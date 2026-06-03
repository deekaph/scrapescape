import asyncio
import os
import re
import logging
from datetime import datetime, timezone
from typing import Callable, Awaitable

import yt_dlp

from . import database as db

logger = logging.getLogger("scrapescape.music")

PROJECT_ROOT = os.path.dirname(os.path.dirname(__file__))
COOKIES_FILE = os.path.join(PROJECT_ROOT, "cookies.txt")
MUSIC_DIR = os.path.join(PROJECT_ROOT, "music")

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\[0[;0-9]*m")


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


class _YtdlpLogger:
    def debug(self, msg):
        msg = _strip_ansi(msg)
        if msg.startswith("[download]") or msg.startswith("[info]"):
            logger.info(msg)

    def info(self, msg):
        logger.info(_strip_ansi(msg))

    def warning(self, msg):
        logger.warning(_strip_ansi(msg))

    def error(self, msg):
        logger.error(_strip_ansi(msg))


def _safe_filename(name: str) -> str:
    """Sanitize a string for use as a file/directory name."""
    name = re.sub(r'[<>:"/\\|?*]', '_', name)
    name = name.strip('. ')
    return name or 'Unknown'


def _primary_artist(artist: str) -> str:
    """Extract just the first/primary artist from a comma-separated or & list.

    e.g. 'Alice, Bob, Bob' -> 'Alice'
    """
    # Split on comma, ampersand, "feat.", "ft."
    parts = re.split(r'\s*[,&]\s*|\s+feat\.?\s+|\s+ft\.?\s+', artist, flags=re.IGNORECASE)
    parts = [p.strip() for p in parts if p.strip()]
    return parts[0] if parts else artist


def _dedup_artist(artist: str) -> str:
    """Clean up a comma-separated artist string.

    - Removes P-line / copyright entries (ALL CAPS names)
    - Removes duplicate names (case-insensitive)

    e.g. 'Alice Smith, Bob Jones, ALICE SMITH' -> 'Alice Smith, Bob Jones'
    """
    parts = [p.strip() for p in artist.split(',') if p.strip()]
    seen = set()
    deduped = []
    for p in parts:
        # Skip ALL CAPS entries — these are P-line/copyright, not artist credits
        if p == p.upper() and len(p) > 1 and any(c.isalpha() for c in p):
            continue
        key = p.lower()
        if key not in seen:
            seen.add(key)
            deduped.append(p)
    return ', '.join(deduped) if deduped else artist


def _base_ydl_opts():
    opts = {
        "quiet": False,
        "no_warnings": False,
        "logger": _YtdlpLogger(),
        "noprogress": True,
        "js_runtimes": {"node": {}, "deno": {}},
        "remote_components": {"ejs:github": {}},
    }
    if os.path.isfile(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
    return opts


_RATE_LIMIT_PATTERNS = [
    "Sign in to confirm you're not a bot",
    "confirm you're not a bot",
    "HTTP Error 429",
    "Too Many Requests",
]


class MusicManager:
    def __init__(self, broadcast: Callable[..., Awaitable]):
        self.broadcast = broadcast
        self.max_concurrent = 3
        self._semaphore = asyncio.Semaphore(3)
        self._running = False
        self._paused = True  # Start paused — user must explicitly start
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_task: asyncio.Task | None = None
        self._active_downloads: dict[int, asyncio.Task] = {}
        self._cancel_flags: dict[int, bool] = {}
        self._consecutive_failures = 0
        self._download_delay = 3  # seconds between starting downloads

    async def start(self):
        """Initialize the manager and start the process loop (but stay paused)."""
        if self._running:
            return
        self._running = True
        self._loop = asyncio.get_event_loop()
        # Reset any stuck downloads from previous crash
        for dl in db.music_get_by_status("downloading"):
            db.music_update_status(dl["id"], "queued")
        self._loop_task = asyncio.create_task(self._process_loop())
        logger.info("Music download manager initialized (paused — click Start to begin)")

    async def resume(self):
        """Unpause the download queue."""
        self._paused = False
        logger.info("Music downloads resumed")

    async def pause(self):
        """Pause the download queue (active downloads finish, no new ones start)."""
        self._paused = True
        logger.info("Music downloads paused")
        await self.broadcast({"type": "music_queue_update"})

    async def stop(self):
        self._running = False
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass

    def set_concurrency(self, n: int):
        n = max(1, min(10, n))
        self.max_concurrent = n
        self._semaphore = asyncio.Semaphore(n)
        db.set_setting("music_concurrent", str(n))

    def notify_queue(self):
        """Wake up the process loop when new items are added."""
        pass  # The loop polls, so this is a no-op for now

    async def _process_loop(self):
        while self._running:
            try:
                if self._paused:
                    await asyncio.sleep(1)
                    continue
                queued = db.music_get_by_status("queued")
                for dl in queued:
                    if not self._running or self._paused:
                        break
                    if dl["id"] in self._active_downloads:
                        continue
                    task = asyncio.create_task(self._download_track(dl))
                    self._active_downloads[dl["id"]] = task
                    # Delay between starting downloads to avoid rate limiting
                    await asyncio.sleep(self._download_delay)
                await asyncio.sleep(2)
                # Clean up finished tasks
                done_ids = [did for did, t in self._active_downloads.items() if t.done()]
                for did in done_ids:
                    del self._active_downloads[did]
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Music process loop error: {e}")
                await asyncio.sleep(5)

    async def _download_track(self, dl: dict):
        download_id = dl["id"]
        url = dl["url"]

        async with self._semaphore:
            if not self._running:
                return
            self._cancel_flags[download_id] = False

            db.music_update_status(download_id, "downloading")
            await self.broadcast({
                "type": "music_status_change",
                "id": download_id,
                "status": "downloading",
            })

            try:
                result = await asyncio.get_event_loop().run_in_executor(
                    None, self._do_download, dl
                )

                if self._cancel_flags.get(download_id):
                    return

                db.music_update_status(
                    download_id, "completed",
                    filename=result.get("filename", ""),
                    title=result.get("title", dl["title"]),
                    artist=result.get("artist", dl["artist"]),
                    album=result.get("album", dl["album"]),
                    track_number=result.get("track_number", dl["track_number"]),
                    completed_at=datetime.now(timezone.utc).isoformat(),
                    progress=100.0,
                )
                await self.broadcast({
                    "type": "music_status_change",
                    "id": download_id,
                    "status": "completed",
                    "title": result.get("title", dl["title"]),
                    "artist": result.get("artist", dl["artist"]),
                    "album": result.get("album", dl["album"]),
                })
                logger.info(f"Music download complete: {result.get('artist', '')} - {result.get('title', '')}")
                self._consecutive_failures = 0

            except Exception as e:
                error_msg = _strip_ansi(str(e))
                logger.error(f"Music download failed: {url} — {error_msg}")

                # Detect rate limiting
                is_rate_limited = any(p in error_msg for p in _RATE_LIMIT_PATTERNS)
                if is_rate_limited:
                    self._consecutive_failures += 1
                    # Re-queue instead of marking failed — it's not a real failure
                    db.music_update_status(
                        download_id, "queued",
                        progress=0.0, error_message="",
                    )
                    if self._consecutive_failures >= 2:
                        # Auto-pause and notify
                        self._paused = True
                        logger.warning("Rate limited by YouTube — auto-paused. Switch VPN and click Start.")
                        await self.broadcast({
                            "type": "music_rate_limited",
                            "message": "Rate limited by YouTube. Switch VPN location and click Start to resume.",
                        })
                        await self.broadcast({"type": "music_queue_update"})
                        self._consecutive_failures = 0
                else:
                    self._consecutive_failures = 0
                    db.music_update_status(
                        download_id, "failed",
                        error_message=error_msg,
                        completed_at=datetime.now(timezone.utc).isoformat(),
                    )
                    await self.broadcast({
                        "type": "music_status_change",
                        "id": download_id,
                        "status": "failed",
                        "error": error_msg,
                    })
            finally:
                self._cancel_flags.pop(download_id, None)

    def _do_download(self, dl: dict) -> dict:
        """Run the actual yt-dlp download in a thread. Returns metadata dict."""
        download_id = dl["id"]
        url = dl["url"]
        audio_format = dl.get("audio_format", "mp3") or "mp3"

        # Strip playlist params from watch URLs to avoid yt-dlp treating single videos as playlists
        # But DON'T strip from playlist URLs (music.youtube.com/playlist?list=) — those ARE the content
        from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
        parsed = urlparse(url)
        is_playlist_url = parsed.path.rstrip("/").endswith("/playlist")
        if not is_playlist_url:
            params = parse_qs(parsed.query)
            params.pop("list", None)
            params.pop("index", None)
            clean_query = urlencode(params, doseq=True)
            url = urlunparse(parsed._replace(query=clean_query))

        # Determine base directory
        base_dir = db.get_setting("music_base_dir")
        if not base_dir or not os.path.isdir(base_dir):
            base_dir = MUSIC_DIR
        os.makedirs(base_dir, exist_ok=True)

        # First, extract info to get metadata
        info_opts = _base_ydl_opts()
        info_opts["extract_flat"] = False
        info_opts["skip_download"] = True
        if not is_playlist_url:
            info_opts["noplaylist"] = True

        with yt_dlp.YoutubeDL(info_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if not info:
            raise Exception("Could not extract track info")

        # Handle playlist — if this URL is a playlist/album, return info for re-queueing
        if info.get("_type") == "playlist" or "entries" in info:
            return self._handle_album(info, dl, audio_format)

        # Detect DJ mix: single video 45+ minutes
        duration = info.get("duration") or 0
        if duration >= 2700:  # 45 minutes
            return self._handle_dj_mix(info, dl, base_dir, audio_format, download_id)

        # Extract metadata
        artist = dl["artist"] or info.get("artist") or info.get("creator") or info.get("uploader") or info.get("channel") or "Unknown Artist"
        album = dl["album"] or info.get("album") or "Unknown Album"
        track_number = dl["track_number"] or info.get("track_number") or 0
        title = dl["title"] or info.get("track") or info.get("title") or "Unknown Track"

        # Clean up track number
        if isinstance(track_number, str):
            try:
                track_number = int(track_number)
            except ValueError:
                track_number = 0

        # Deduplicate artist names and extract primary for directory
        artist = _dedup_artist(artist)
        # Use album_artist for directory if set (keeps all album tracks together),
        # otherwise fall back to primary artist from track metadata
        stored_album_artist = dl.get("album_artist", "").strip()
        if stored_album_artist:
            dir_artist = _primary_artist(_dedup_artist(stored_album_artist))
        else:
            dir_artist = _primary_artist(artist)

        # Build output path
        if dl.get("one_hit_wonder"):
            # One Hit Wonder: save flat in base music directory
            output_dir = base_dir
        else:
            # Normal: BaseDir/PrimaryArtist/Album/
            artist_dir = _safe_filename(dir_artist)
            album_dir = _safe_filename(album)
            output_dir = os.path.join(base_dir, artist_dir, album_dir)
        os.makedirs(output_dir, exist_ok=True)

        # Filename: Artist - Album - TrackNumber - Title.ext
        safe_artist = _safe_filename(artist)
        safe_album = _safe_filename(album)
        safe_title = _safe_filename(title)
        if track_number > 0:
            filename_template = f"{safe_artist} - {safe_album} - {track_number:02d} - {safe_title}.%(ext)s"
        else:
            filename_template = f"{safe_artist} - {safe_album} - {safe_title}.%(ext)s"

        output_template = os.path.join(output_dir, filename_template)

        # Progress callback (throttled to 1 broadcast/sec to prevent browser memory leaks)
        _last_music_broadcast = [0.0]

        def progress_hook(d):
            if self._cancel_flags.get(download_id):
                raise Exception("Download cancelled")
            if d["status"] == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                downloaded = d.get("downloaded_bytes", 0)
                if total > 0:
                    pct = round((downloaded / total) * 100, 1)
                else:
                    pct = 0.0
                speed = _strip_ansi(d.get("_speed_str", "").strip())
                eta = _strip_ansi(d.get("_eta_str", "").strip())
                filesize = _strip_ansi(d.get("_total_bytes_str", "").strip() or d.get("_total_bytes_estimate_str", "").strip())
                db.music_update_progress(download_id, pct, speed, eta, filesize)
                # Throttle WS broadcasts to max 1/sec
                import time as _time
                now = _time.monotonic()
                if now - _last_music_broadcast[0] < 1.0:
                    return
                _last_music_broadcast[0] = now
                try:
                    if self._loop and self._loop.is_running():
                        asyncio.run_coroutine_threadsafe(
                            self.broadcast({
                                "type": "music_progress",
                                "id": download_id,
                                "progress": pct,
                                "speed": speed,
                                "eta": eta,
                                "filesize": filesize,
                            }),
                            self._loop,
                        )
                except Exception:
                    pass

        # Download options
        dl_opts = _base_ydl_opts()
        dl_opts.update({
            "format": "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best",
            "outtmpl": output_template,
            "noplaylist": True,
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": audio_format,
                "preferredquality": "0",  # best quality
            }, {
                "key": "FFmpegMetadata",
            }, {
                "key": "EmbedThumbnail",
            }],
            "writethumbnail": True,
            "progress_hooks": [progress_hook],
        })

        with yt_dlp.YoutubeDL(dl_opts) as ydl:
            ydl.download([url])

        # Find the actual output file
        expected_file = output_template.replace("%(ext)s", audio_format)
        if not os.path.isfile(expected_file):
            # Try to find it
            import glob as g
            base = output_template.rsplit(".%(ext)s", 1)[0]
            matches = g.glob(f"{base}.*")
            matches = [m for m in matches if not m.endswith(('.webp', '.jpg', '.png', '.part', '.ytdl'))]
            if matches:
                expected_file = matches[0]

        return {
            "filename": expected_file if os.path.isfile(expected_file) else "",
            "artist": artist,
            "album": album,
            "track_number": track_number,
            "title": title,
        }

    def _handle_dj_mix(self, info: dict, dl: dict, base_dir: str,
                       audio_format: str, download_id: int) -> dict:
        """Handle a long video (45+ min) as a DJ mix."""
        channel = info.get("uploader") or info.get("channel") or "Unknown"
        title = info.get("title") or "Unknown Mix"
        description = info.get("description") or ""
        upload_date = info.get("upload_date") or ""  # YYYYMMDD format
        url = dl["url"]

        # Format the upload date nicely
        if upload_date and len(upload_date) == 8:
            formatted_date = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:8]}"
        else:
            formatted_date = upload_date

        # Build filename: Channel - Title
        safe_name = _safe_filename(f"{channel} - {title}")

        # Output to DJ Mixes subdirectory
        mix_dir = os.path.join(base_dir, "DJ Mixes")
        os.makedirs(mix_dir, exist_ok=True)

        output_template = os.path.join(mix_dir, f"{safe_name}.%(ext)s")

        # Write the description to a .txt file
        txt_path = os.path.join(mix_dir, f"{safe_name}.txt")
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(f"URL: {url}\n")
            f.write(f"Published: {formatted_date}\n")
            f.write(f"Channel: {channel}\n")
            f.write(f"Title: {title}\n")
            f.write(f"\n{'=' * 60}\n\n")
            f.write(description)

        logger.info(f"DJ mix detected ({info.get('duration', 0) // 60}min): {channel} - {title}")

        # Progress callback (throttled to 1 broadcast/sec)
        _last_mix_broadcast = [0.0]

        def progress_hook(d):
            if self._cancel_flags.get(download_id):
                raise Exception("Download cancelled")
            if d["status"] == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                downloaded = d.get("downloaded_bytes", 0)
                pct = round((downloaded / total) * 100, 1) if total > 0 else 0.0
                speed = _strip_ansi(d.get("_speed_str", "").strip())
                eta = _strip_ansi(d.get("_eta_str", "").strip())
                filesize = _strip_ansi(d.get("_total_bytes_str", "").strip() or d.get("_total_bytes_estimate_str", "").strip())
                db.music_update_progress(download_id, pct, speed, eta, filesize)
                import time as _time
                now = _time.monotonic()
                if now - _last_mix_broadcast[0] < 1.0:
                    return
                _last_mix_broadcast[0] = now
                try:
                    if self._loop and self._loop.is_running():
                        asyncio.run_coroutine_threadsafe(
                            self.broadcast({
                                "type": "music_progress",
                                "id": download_id,
                                "progress": pct,
                                "speed": speed,
                                "eta": eta,
                                "filesize": filesize,
                            }),
                            self._loop,
                        )
                except Exception:
                    pass

        # Download
        dl_opts = _base_ydl_opts()
        dl_opts.update({
            "format": "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best",
            "outtmpl": output_template,
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": audio_format,
                "preferredquality": "0",
            }, {
                "key": "FFmpegMetadata",
            }, {
                "key": "EmbedThumbnail",
            }],
            "writethumbnail": True,
            "progress_hooks": [progress_hook],
        })

        with yt_dlp.YoutubeDL(dl_opts) as ydl:
            ydl.download([url])

        # Find actual output file
        expected_file = output_template.replace("%(ext)s", audio_format)
        if not os.path.isfile(expected_file):
            import glob as g
            base = output_template.rsplit(".%(ext)s", 1)[0]
            matches = g.glob(f"{base}.*")
            matches = [m for m in matches if not m.endswith(('.webp', '.jpg', '.png', '.part', '.ytdl', '.txt'))]
            if matches:
                expected_file = matches[0]

        return {
            "filename": expected_file if os.path.isfile(expected_file) else "",
            "artist": channel,
            "album": "DJ Mixes",
            "track_number": 0,
            "title": title,
        }

    def _handle_album(self, info: dict, dl: dict, audio_format: str) -> dict:
        """When a URL turns out to be a playlist/album, extract tracks and queue them."""
        entries = info.get("entries", [])
        if not entries:
            raise Exception("Playlist/album has no entries")

        # Resolve lazy entries
        resolved = []
        for entry in entries:
            if entry is None:
                continue
            resolved.append(entry)

        album_title = info.get("title") or dl.get("album") or "Unknown Album"
        # Strip "Album - " prefix YouTube Music adds
        if album_title.startswith("Album - "):
            album_title = album_title[8:]
        # Prefer the artist we explicitly set (from discography scan) over yt-dlp metadata
        album_artist = dl.get("artist") or info.get("uploader") or info.get("channel") or "Unknown Artist"
        # Clean up "Topic" suffix YouTube adds
        if album_artist.endswith(" - Topic"):
            album_artist = album_artist[:-8]

        tracks = []
        for i, entry in enumerate(resolved):
            track_url = entry.get("url") or entry.get("webpage_url")
            if not track_url:
                continue
            track_artist = entry.get("artist") or entry.get("creator") or album_artist
            track_artist = _dedup_artist(track_artist)
            tracks.append({
                "url": track_url,
                "artist": track_artist,
                "album": entry.get("album") or album_title,
                "track_number": entry.get("track_number") or (i + 1),
                "title": entry.get("track") or entry.get("title") or f"Track {i + 1}",
            })

        if tracks:
            result = db.music_add_album(tracks, audio_format=audio_format, force=True,
                                        album_artist=_dedup_artist(album_artist))
            logger.info(f"Album detected: '{album_title}' by '{album_artist}' — queued {result['added']} tracks")

        return {
            "filename": "",
            "artist": album_artist,
            "album": album_title,
            "track_number": 0,
            "title": f"Album: {album_title} ({len(tracks)} tracks queued)",
        }

    async def cancel_one(self, download_id: int):
        self._cancel_flags[download_id] = True
        if download_id in self._active_downloads:
            task = self._active_downloads[download_id]
            task.cancel()
        db.music_update_status(download_id, "failed", error_message="Cancelled by user")
        await self.broadcast({"type": "music_status_change", "id": download_id, "status": "failed", "error": "Cancelled"})

    async def retry_one(self, download_id: int):
        db.music_reset_to_queued(download_id)
        await self.broadcast({"type": "music_queue_update"})


def extract_mix_playlist(url: str) -> dict:
    """Extract a YouTube playlist and return the list of videos with metadata.

    Returns {title, url, mixes: [{url, title, channel, duration}]}
    """
    opts = _base_ydl_opts()
    opts["extract_flat"] = "in_playlist"
    opts["skip_download"] = True

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if not info:
        raise Exception("Could not extract playlist")

    playlist_title = info.get("title") or "Mix Playlist"

    entries = info.get("entries", [])
    mixes = []
    seen_urls = set()

    for entry in entries:
        if entry is None:
            continue

        entry_url = entry.get("url") or entry.get("webpage_url") or ""
        if not entry_url:
            continue

        # Build full URL if needed
        if not entry_url.startswith("http"):
            entry_url = f"https://www.youtube.com/watch?v={entry_url}"

        if entry_url in seen_urls:
            continue
        seen_urls.add(entry_url)

        mixes.append({
            "url": entry_url,
            "title": entry.get("title") or "Unknown",
            "channel": entry.get("uploader") or entry.get("channel") or "",
            "duration": entry.get("duration") or 0,
        })

    logger.info(f"Mix playlist '{playlist_title}': found {len(mixes)} videos")

    return {
        "title": playlist_title,
        "url": url,
        "mixes": mixes,
    }


def _extract_artist_id(url: str) -> str:
    """Extract a channel ID or browse ID from a YouTube Music URL."""
    from urllib.parse import urlparse, parse_qs

    parsed = urlparse(url)
    path = parsed.path.strip("/")

    # https://music.youtube.com/channel/UCxxxxxx
    if "/channel/" in url:
        parts = path.split("/")
        for i, p in enumerate(parts):
            if p == "channel" and i + 1 < len(parts):
                return parts[i + 1]

    # https://music.youtube.com/browse/MPADUCxxxxxx
    # MPAD prefix wraps the channel ID — strip it for ytmusicapi
    if "/browse/" in url:
        parts = path.split("/")
        for i, p in enumerate(parts):
            if p == "browse" and i + 1 < len(parts):
                browse_id = parts[i + 1]
                if browse_id.startswith("MPAD"):
                    return browse_id[4:]
                return browse_id

    # Fallback — maybe it's just an ID
    return url


def extract_artist_discography(url: str) -> dict:
    """Extract album/single/EP list from a YouTube Music artist page.

    Uses ytmusicapi for proper structured album/singles data.
    Returns {name, url, releases: [{url, title, year, track_count, type}]}
    Runs synchronously (call from executor).
    """
    from ytmusicapi import YTMusic

    artist_id = _extract_artist_id(url)
    ytmusic = YTMusic()

    logger.info(f"Scanning artist page: {url} (id={artist_id})")

    try:
        artist_info = ytmusic.get_artist(artist_id)
    except Exception as e:
        raise Exception(f"Could not load artist page: {e}")

    artist_name = artist_info.get("name") or "Unknown Artist"
    releases = []

    # Process each release category (albums, singles, etc.)
    for category_key in ("albums", "singles"):
        category = artist_info.get(category_key)
        if not category:
            continue

        results = category.get("results", [])

        # If there's a "browseId" for the category, it means there are more
        # results than shown. Fetch the full list.
        browse_id = category.get("browseId")
        params = category.get("params")
        if browse_id:
            try:
                full_list = ytmusic.get_artist_albums(browse_id, params)
                if full_list:
                    results = full_list
            except Exception as e:
                logger.warning(f"Could not fetch full {category_key} list: {e}")

        for item in results:
            browse_id = item.get("browseId", "")
            title = item.get("title") or "Unknown Release"
            year = item.get("year") or ""

            # Get the playlist ID for this album so yt-dlp can download it
            playlist_id = None
            if browse_id:
                try:
                    album_info = ytmusic.get_album(browse_id)
                    playlist_id = album_info.get("audioPlaylistId")
                    # Use more accurate track count from album info
                    track_count = len(album_info.get("tracks", []))
                except Exception:
                    track_count = 0
            else:
                track_count = 0

            if playlist_id:
                release_url = f"https://music.youtube.com/playlist?list={playlist_id}"
            elif browse_id:
                release_url = f"https://music.youtube.com/browse/{browse_id}"
            else:
                continue

            # Classify type
            if category_key == "singles":
                release_type = "single"
            elif track_count and 2 <= track_count <= 6:
                release_type = "ep"
            else:
                release_type = "album"

            releases.append({
                "url": release_url,
                "title": title,
                "year": year,
                "track_count": track_count,
                "type": release_type,
            })

    logger.info(
        f"Artist '{artist_name}': found {len(releases)} releases — "
        f"{sum(1 for r in releases if r['type'] == 'album')} albums, "
        f"{sum(1 for r in releases if r['type'] == 'ep')} EPs, "
        f"{sum(1 for r in releases if r['type'] == 'single')} singles"
    )

    return {
        "name": artist_name,
        "url": url,
        "releases": releases,
    }
