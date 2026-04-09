import asyncio
import glob
import hashlib
import os
import re
import shutil
import logging
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlparse, urljoin

import yt_dlp

from . import database as db

logger = logging.getLogger("scrapescape.downloader")

import time as _time

PROJECT_ROOT = os.path.dirname(os.path.dirname(__file__))
COOKIES_FILE = os.path.join(PROJECT_ROOT, "cookies.txt")
DOWNLOAD_DIR = os.path.join(PROJECT_ROOT, "downloads")

# Cancellation flags for long-running downloads (keyed by URL)
_cancel_flag: dict[str, bool] = {}


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


class _YtdlpLogger:
    """Routes yt-dlp output through Python logging so it appears in the UI."""
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


def _base_ydl_opts():
    opts = {
        "quiet": False,
        "no_warnings": False,
        "continuedl": True,
        "logger": _YtdlpLogger(),
        "noprogress": True,
        "js_runtimes": {"node": {}, "deno": {}},
        "remote_components": {"ejs:github": {}},
    }
    if os.path.isfile(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
    return opts


_GENERIC_TITLES = {"videos", "playlist", "uploads", "all videos", "featured", "newest", "most viewed", ""}


def _best_playlist_title(info: dict, url: str) -> str:
    """Pick the best human-readable title for a playlist, falling back to URL parsing."""
    # Try metadata fields, skip generic ones
    for key in ("title", "playlist_title", "uploader", "channel"):
        val = (info.get(key) or "").strip()
        if val and val.lower() not in _GENERIC_TITLES:
            return val

    # Extract a meaningful name from the URL path
    # Handles patterns like /model/asianvixen4u/videos, /channels/foo, /pornstar/bar, /users/baz
    try:
        path = urlparse(url).path.strip("/")
        parts = [p for p in path.split("/") if p]
        # Walk the path segments, skip generic trailing ones like "videos"
        meaningful = [p for p in parts if p.lower() not in _GENERIC_TITLES]
        if meaningful:
            # Prefer the last meaningful segment (usually the username/channel)
            return meaningful[-1]
    except Exception:
        pass

    return url


_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

# Patterns to find video URLs in page source — ordered by specificity
_VIDEO_PATTERNS = [
    # HLS manifests in JS: often assigned as strings in player config
    re.compile(r'["\']([^"\']*\.m3u8(?:\?[^"\']*)?)["\']', re.IGNORECASE),
    # Direct video URLs in attributes
    re.compile(r'(?:src|href|file|url|source)\s*[:=]\s*["\']([^"\']+\.(?:mp4|webm|mkv)(?:\?[^"\']*)?)["\']', re.IGNORECASE),
    # Bare URLs ending in video extensions
    re.compile(r'(https?://[^\s"\'<>]+\.(?:mp4|m3u8|webm|mkv)(?:\?[^\s"\'<>]*)?)', re.IGNORECASE),
]


def _fetch_page(url: str, cookies_file: str | None = None) -> tuple[str, any]:
    """Fetch a page and return (html, opener_or_None)."""
    headers = {"User-Agent": _USER_AGENT}
    req = urllib.request.Request(url, headers=headers)
    opener = None

    if cookies_file and os.path.isfile(cookies_file):
        import http.cookiejar
        cj = http.cookiejar.MozillaCookieJar(cookies_file)
        cj.load(ignore_discard=True, ignore_expires=True)
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
        html = opener.open(req, timeout=30).read().decode("utf-8", errors="replace")
    else:
        html = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", errors="replace")

    return html, opener


def _try_player_api(url: str, page_content: str, cookies_file: str | None = None) -> str | None:
    """Try to find and call JS player APIs that return video stream URLs."""
    import base64, json as _json

    # Pattern: page has film_id and a player endpoint (common on video sites)
    film_id_match = re.search(r'(?:film_id|video_id|content_id)\s*[:=]\s*["\']?(\d+)', page_content)
    player_url_match = re.search(r'(https?://[^"\']+)/ajax/player', page_content)

    # Also check external script URLs referenced in the page
    if not player_url_match:
        asset_match = re.search(r'var\s+web_link_player\s*=\s*["\']([^"\']+)["\']', page_content)
        if asset_match:
            player_url_match = type('Match', (), {'group': lambda self, n=1: asset_match.group(1)})()

    if film_id_match and player_url_match:
        fid = film_id_match.group(1)
        base_url = player_url_match.group(1)
        api_url = f"{base_url}/ajax/player"
        logger.info("Fallback: found player API at %s with film_id=%s", api_url, fid)

        try:
            data = f"player=1&id={fid}".encode()
            parsed = urlparse(url)
            req = urllib.request.Request(api_url, data=data, headers={
                "User-Agent": _USER_AGENT,
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": url,
                "Origin": f"{parsed.scheme}://{parsed.hostname}",
            })
            resp = urllib.request.urlopen(req, timeout=15).read().decode()
            j = _json.loads(resp)

            if j.get("msg") == "ok":
                if j.get("url_m3u8"):
                    logger.info("Fallback: player API returned m3u8 URL")
                    return j["url_m3u8"]
                elif j.get("source_m3u8"):
                    # base64-encoded m3u8 content — decode and find URLs inside
                    decoded = base64.b64decode(j["source_m3u8"]).decode("utf-8", errors="replace")
                    m3u8_urls = re.findall(r"(https?://[^\s]+)", decoded)
                    if m3u8_urls:
                        logger.info("Fallback: player API returned base64 m3u8 with %d URLs", len(m3u8_urls))
                        return m3u8_urls[0]
                elif j.get("url"):
                    return j["url"]
        except Exception as e:
            logger.warning("Fallback: player API call failed — %s", str(e))

    return None


def _fallback_scrape(url: str, output_dir: str, cookies_file: str | None = None, progress_callback=None) -> dict:
    """Scrape a page for video URLs and download the best one found."""
    logger.info("yt-dlp unsupported — trying fallback scrape for %s", url)

    try:
        logger.info("Fallback: fetching page...")
        page_content, opener = _fetch_page(url, cookies_file)
        logger.info("Fallback: got %d bytes of HTML", len(page_content))
    except Exception as e:
        return {"success": False, "error": f"Fallback: failed to fetch page — {_strip_ansi(str(e))}"}

    # Extract page title
    title_match = re.search(r"<title[^>]*>([^<]+)</title>", page_content, re.IGNORECASE)
    page_title = title_match.group(1).strip() if title_match else "Unknown"
    page_title = re.sub(r'[<>:"/\\|?*]', '_', page_title)[:100]
    logger.info("Fallback: page title = %s", page_title)

    # Find all video URLs in the full page source (HTML + inline JS)
    video_urls = set()
    for pattern in _VIDEO_PATTERNS:
        for match in pattern.finditer(page_content):
            found_url = match.group(1)
            # Skip blob URLs, data URIs, and obvious non-video
            if found_url.startswith("blob:") or found_url.startswith("data:"):
                continue
            if found_url.startswith("//"):
                found_url = "https:" + found_url
            elif found_url.startswith("/"):
                found_url = urljoin(url, found_url)
            elif not found_url.startswith("http"):
                found_url = urljoin(url, found_url)
            video_urls.add(found_url)

    # Secondary: check for JS player API patterns (ajax/player endpoints)
    if not video_urls:
        api_result = _try_player_api(url, page_content, cookies_file)
        if api_result:
            video_urls.add(api_result)

    logger.info("Fallback: found %d video URLs", len(video_urls))
    for vu in video_urls:
        logger.info("  -> %s", vu[:150])

    if not video_urls:
        return {"success": False, "error": "Unsupported site — fallback scrape found no video URLs in page source"}

    # Prefer m3u8 (full video stream), then mp4, skip thumbnails/previews
    def score_url(u):
        s = 0
        ul = u.lower()
        if ".m3u8" in ul:
            s += 200  # HLS streams are usually the full video
        elif ".mp4" in ul:
            s += 100
        if "preview" in ul or "thumb" in ul or "trailer" in ul or "poster" in ul:
            s -= 500
        if "master" in ul:
            s += 50  # master.m3u8 is the multi-quality manifest
        if "1080" in u:
            s += 30
        elif "720" in u:
            s += 20
        elif "480" in u:
            s += 10
        return s

    best_url = max(video_urls, key=score_url)
    logger.info("Fallback found %d video URLs, best: %s", len(video_urls), best_url[:150])

    # If we found an m3u8, hand it back to yt-dlp — it can download HLS streams directly
    if ".m3u8" in best_url.lower():
        logger.info("Fallback: found HLS stream, downloading via yt-dlp")
        return _download_m3u8_via_ytdlp(best_url, url, page_title, output_dir, cookies_file, progress_callback=progress_callback)

    # Direct download for mp4/webm
    site = urlparse(url).hostname or "unknown"
    site = site.replace("www.", "").split(".")[0]
    url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
    filename = f"[{site}] {page_title} [{url_hash}].mp4"
    filepath = os.path.join(output_dir, filename)

    try:
        logger.info("Fallback: starting direct download of %s", best_url[:120])
        headers = {"User-Agent": _USER_AGENT, "Referer": url}
        dl_req = urllib.request.Request(best_url, headers=headers)
        if opener:
            response = opener.open(dl_req, timeout=300)
        else:
            response = urllib.request.urlopen(dl_req, timeout=300)

        total_size = int(response.headers.get("Content-Length", 0))
        logger.info("Fallback: Content-Length = %s", f"{total_size // (1024*1024)}MB" if total_size else "unknown")
        downloaded = 0
        last_log_mb = 0

        with open(filepath, "wb") as f:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                dl_mb = downloaded // (1024 * 1024)

                if progress_callback and total_size > 0:
                    pct = (downloaded / total_size) * 100
                    progress_callback(pct, "", f"{dl_mb}MB / {total_size // (1024*1024)}MB")
                elif progress_callback:
                    progress_callback(0, "", f"{dl_mb}MB downloaded")

                # Log every 10MB
                if dl_mb >= last_log_mb + 10:
                    last_log_mb = dl_mb
                    if total_size:
                        logger.info("Fallback: %dMB / %dMB (%.1f%%)", dl_mb, total_size // (1024*1024), (downloaded / total_size) * 100)
                    else:
                        logger.info("Fallback: %dMB downloaded...", dl_mb)
    except Exception as e:
        return {"success": False, "error": f"Fallback download failed — {_strip_ansi(str(e))}"}

    filesize = os.path.getsize(filepath)
    if filesize < 10000:
        os.remove(filepath)
        return {"success": False, "error": f"Fallback download too small ({filesize} bytes) — likely not a video"}

    return {
        "success": True,
        "title": page_title,
        "filename": os.path.basename(filepath),
        "filepath": filepath,
        "filesize": "",
    }


def _download_m3u8_via_ytdlp(m3u8_url: str, page_url: str, title: str, output_dir: str, cookies_file: str | None, progress_callback=None) -> dict:
    """Use yt-dlp to download an HLS stream from a direct m3u8 URL."""
    site = urlparse(page_url).hostname or "unknown"
    site = site.replace("www.", "").split(".")[0]
    url_hash = hashlib.md5(page_url.encode()).hexdigest()[:8]

    parsed = urlparse(page_url)
    origin = f"{parsed.scheme}://{parsed.hostname}"

    outtmpl = os.path.join(output_dir, f"[{site}] {title} [{url_hash}].%(ext)s")
    opts = {
        "outtmpl": outtmpl,
        "quiet": False,
        "logger": _YtdlpLogger(),
        "noprogress": True,
        "continuedl": True,
        "merge_output_format": "mp4",
        "http_headers": {
            "Referer": page_url,
            "Origin": origin,
            "User-Agent": _USER_AGENT,
        },
        # yt-dlp specific flags for HLS
        "referer": page_url,
        "concurrent_fragment_downloads": 4,
    }
    if cookies_file and os.path.isfile(cookies_file):
        opts["cookiefile"] = cookies_file

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(m3u8_url, download=True)
            if info:
                filename = ydl.prepare_filename(info)
                base, _ = os.path.splitext(filename)
                for ext in (".mp4", ".mkv", ".webm", ".ts"):
                    if os.path.exists(base + ext):
                        filename = base + ext
                        break
                return {
                    "success": True,
                    "title": title,
                    "filename": os.path.basename(filename),
                    "filepath": filename,
                    "filesize": "",
                }
    except Exception as e:
        err = _strip_ansi(str(e))
        if "redirect" in err.lower() or "302" in err:
            logger.info("HLS redirect loop — trying headless browser fallback")
            return _download_via_browser(m3u8_url, page_url, title, output_dir, progress_callback=progress_callback)
        return {"success": False, "error": f"Fallback HLS download failed — {err}"}


def _download_via_browser(m3u8_url: str, page_url: str, title: str, output_dir: str, progress_callback=None) -> dict:
    """Last resort: use a headless browser to visit the page, intercept the HLS stream,
    and download using the browser's authenticated session."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"success": False, "error": "Cloudflare-protected stream — install playwright: pip install playwright && playwright install chromium"}

    site = urlparse(page_url).hostname or "unknown"
    site = site.replace("www.", "").split(".")[0]
    url_hash = hashlib.md5(page_url.encode()).hexdigest()[:8]

    logger.info("Browser fallback: launching headless Chromium for %s", page_url)

    captured_m3u8 = {"url": None, "segments": []}

    def handle_response(response):
        url = response.url
        if ".m3u8" in url:
            logger.info("Browser: intercepted m3u8 at %s", url[:120])
            captured_m3u8["url"] = url
            try:
                body = response.text()
                # Extract .ts segment URLs from the m3u8
                for line in body.splitlines():
                    line = line.strip()
                    if line and not line.startswith("#"):
                        if line.startswith("http"):
                            captured_m3u8["segments"].append(line)
                        else:
                            # Relative URL
                            base = url.rsplit("/", 1)[0]
                            captured_m3u8["segments"].append(f"{base}/{line}")
            except Exception:
                pass
        elif ".ts" in url and url.startswith("http"):
            captured_m3u8["segments"].append(url)

    try:
        with sync_playwright() as p:
            logger.info("Browser: launching Chromium...")
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(user_agent=_USER_AGENT)
            page = context.new_page()
            page.on("response", handle_response)

            logger.info("Browser: navigating to %s", page_url)
            page.goto(page_url, wait_until="domcontentloaded", timeout=30000)
            logger.info("Browser: page loaded, waiting for Cloudflare...")
            page.wait_for_timeout(5000)

            # Click play/age-gate buttons
            for selector in ["button.cf_18y", ".playaction", ".vjs-big-play-button", "[onclick*='cf18y']"]:
                try:
                    el = page.query_selector(selector)
                    if el:
                        logger.info("Browser: clicking %s", selector)
                        el.click()
                        page.wait_for_timeout(2000)
                except Exception:
                    pass

            # Wait for video stream to be intercepted
            logger.info("Browser: waiting for video stream to load...")
            for i in range(12):  # up to 60 seconds
                page.wait_for_timeout(5000)
                if captured_m3u8["url"] or len(captured_m3u8["segments"]) > 0:
                    logger.info("Browser: stream captured after %ds", (i + 1) * 5)
                    break
                logger.info("Browser: still waiting... (%ds)", (i + 1) * 5)

            # Give it a moment to capture more segments
            if captured_m3u8["segments"]:
                page.wait_for_timeout(3000)

            browser.close()

        if not captured_m3u8["url"] and not captured_m3u8["segments"]:
            return {"success": False, "error": "Browser fallback: no video stream intercepted — site may require login"}

        # If we captured an m3u8 URL that works from the browser context,
        # save the segments list and download them
        if captured_m3u8["segments"]:
            total_segs = len(captured_m3u8["segments"])
            logger.info("Browser: captured %d segments, downloading...", total_segs)
            filepath = os.path.join(output_dir, f"[{site}] {title} [{url_hash}].ts")
            total_bytes = 0
            consecutive_fails = 0
            max_consecutive_fails = 10

            with open(filepath, "wb") as f:
                for i, seg_url in enumerate(captured_m3u8["segments"]):
                    # Check for cancellation
                    if _cancel_flag.get(page_url):
                        logger.info("Browser: download cancelled by user at segment %d/%d", i, total_segs)
                        _cancel_flag.pop(page_url, None)
                        return {"success": False, "error": "Download cancelled"}

                    # Retry with backoff
                    data = None
                    for attempt in range(4):
                        try:
                            req = urllib.request.Request(seg_url, headers={"User-Agent": _USER_AGENT, "Referer": page_url})
                            data = urllib.request.urlopen(req, timeout=30).read()
                            consecutive_fails = 0
                            break
                        except urllib.request.HTTPError as e:
                            if e.code == 429:
                                wait = (attempt + 1) * 5
                                if attempt == 0:
                                    logger.warning("Browser: rate limited at segment %d, waiting %ds...", i, wait)
                                _time.sleep(wait)
                            else:
                                logger.warning("Browser: segment %d HTTP %d on attempt %d", i, e.code, attempt + 1)
                                break
                        except Exception as e:
                            logger.warning("Browser: segment %d error: %s", i, str(e))
                            break

                    if data:
                        f.write(data)
                        f.flush()
                        total_bytes += len(data)
                    else:
                        consecutive_fails += 1
                        if consecutive_fails >= max_consecutive_fails:
                            logger.error("Browser: %d consecutive segment failures, aborting", max_consecutive_fails)
                            break

                    pct = ((i + 1) / total_segs) * 100
                    size_str = f"{total_bytes // (1024*1024)}MB ({i+1}/{total_segs} segments)"
                    if progress_callback:
                        progress_callback(pct, "", size_str)
                    if (i + 1) % 50 == 0:
                        logger.info("Browser: %d/%d segments (%.1f%%, %dMB)", i + 1, total_segs, pct, total_bytes // (1024*1024))

                    # Small delay to avoid rate limiting
                    _time.sleep(0.1)

            filesize = os.path.getsize(filepath)
            if filesize < 10000:
                os.remove(filepath)
                return {"success": False, "error": "Browser fallback: downloaded file too small"}

            if consecutive_fails >= max_consecutive_fails:
                return {"success": False, "error": f"Browser fallback: too many segment failures — got {total_bytes // (1024*1024)}MB before aborting"}

            return {
                "success": True,
                "title": title,
                "filename": os.path.basename(filepath),
                "filepath": filepath,
                "filesize": f"{filesize // (1024*1024)}MB",
            }

        return {"success": False, "error": "Browser fallback: captured m3u8 but no segments found"}

    except Exception as e:
        return {"success": False, "error": f"Browser fallback failed — {_strip_ansi(str(e))}"}


def _get_domain(url: str) -> str:
    try:
        host = urlparse(url).hostname or "unknown"
        return host.replace("www.", "")
    except Exception:
        return "unknown"


class DownloadManager:
    def __init__(self, broadcast_fn):
        self.broadcast = broadcast_fn
        self.max_concurrent = int(db.get_setting("max_concurrent") or 3)
        self.max_per_site = int(db.get_setting("max_per_site") or self.max_concurrent)
        self.semaphore = asyncio.Semaphore(self.max_concurrent)
        self.active_tasks: dict[int, asyncio.Task] = {}
        self.active_domains: dict[str, int] = {}  # domain -> count of active downloads
        self._individually_paused: set[int] = set()  # download IDs paused individually
        self._running = False
        self._paused = False
        self._loop = None
        self._queue_event = asyncio.Event()

    async def start(self):
        if self._running:
            if self._paused:
                await self.resume_all()
            return
        self._running = True
        self._paused = False
        self._loop = asyncio.get_event_loop()
        # Reset any downloads stuck in 'downloading' from a crash
        for dl in db.get_by_status("downloading"):
            db.update_status(dl["id"], "paused")
        asyncio.create_task(self._process_loop())
        logger.info("DownloadManager started (max_concurrent=%d)", self.max_concurrent)

    async def pause(self):
        """Pause all active downloads immediately."""
        self._paused = True
        # Signal browser fallback downloads to stop
        for dl in db.get_by_status("downloading"):
            _cancel_flag[dl["url"]] = True
        for task_id, task in list(self.active_tasks.items()):
            task.cancel()
        if self.active_tasks:
            await asyncio.sleep(0.5)
        self.active_tasks.clear()
        logger.info("DownloadManager paused")

    async def resume_all(self):
        """Resume all paused downloads. They go back to queued and the loop picks them up."""
        for dl in db.get_by_status("paused"):
            db.update_status(dl["id"], "queued")
        self._paused = False
        self.notify_queue()
        await self.broadcast({"type": "queue_update"})
        logger.info("DownloadManager resumed all")

    async def resume_one(self, download_id: int):
        """Resume a single paused download, releasing the held semaphore slot."""
        db.update_status(download_id, "queued")
        # Release the semaphore slot that was held during individual pause
        self.semaphore.release()
        if not db.get_by_status("paused"):
            self._paused = False
        self.notify_queue()
        await self.broadcast({"type": "queue_update"})

    async def cancel_all(self, delete_partial: bool):
        """Cancel all paused downloads — delete partials and requeue."""
        paused = db.get_by_status("paused")
        for dl in paused:
            if delete_partial:
                self._delete_partial_files(dl)
            db.reset_to_queued(dl["id"])
        # If nothing paused left, unpause so queue can process
        if not db.get_by_status("paused"):
            self._paused = False
        self.notify_queue()
        await self.broadcast({"type": "queue_update"})
        logger.info("Cancelled all paused (delete_partial=%s)", delete_partial)

    async def cancel_one(self, download_id: int, delete_partial: bool):
        """Cancel a single paused download — delete partial and requeue, release held slot."""
        dl = next((d for d in db.get_by_status("paused") if d["id"] == download_id), None)
        if dl:
            if delete_partial:
                self._delete_partial_files(dl)
            db.reset_to_queued(download_id)
            self.semaphore.release()
        if not db.get_by_status("paused"):
            self._paused = False
        self.notify_queue()
        await self.broadcast({"type": "queue_update"})

    def _delete_partial_files(self, item):
        """Remove .part files and any incomplete downloads for this item."""
        subfolder = item.get("subfolder", "")
        output_dir = os.path.join(DOWNLOAD_DIR, subfolder) if subfolder else DOWNLOAD_DIR
        if not os.path.isdir(output_dir):
            return
        # yt-dlp partial files end in .part, .ytdl, or temp patterns
        for pattern in ("*.part", "*.ytdl", "*.temp"):
            for f in glob.glob(os.path.join(output_dir, pattern)):
                try:
                    os.remove(f)
                    logger.info("Deleted partial file: %s", f)
                except OSError:
                    pass

    def set_concurrency(self, n: int):
        self.max_concurrent = max(1, min(n, 10))
        self.semaphore = asyncio.Semaphore(self.max_concurrent)
        db.set_setting("max_concurrent", str(self.max_concurrent))
        logger.info("Concurrency set to %d", self.max_concurrent)

    def set_per_site(self, n: int):
        self.max_per_site = max(1, min(n, 10))
        db.set_setting("max_per_site", str(self.max_per_site))
        logger.info("Per-site concurrency set to %d", self.max_per_site)

    def notify_queue(self):
        self._queue_event.set()

    @property
    def is_paused(self):
        return self._paused

    async def start_now(self, download_id: int):
        """Start a specific download immediately, bypassing the concurrency limit."""
        # Check both queued and pending items
        items = [d for d in db.get_by_status("queued") + db.get_by_status("pending") if d["id"] == download_id]
        if not items:
            return
        item = items[0]
        if item["id"] in self.active_tasks:
            return
        # Launch directly without acquiring the semaphore
        task = asyncio.create_task(self._download_wrapper(item, use_semaphore=False))
        self.active_tasks[item["id"]] = task

    def _check_disk_space(self) -> bool:
        """Return True if disk is below 90% full, False if we should pause."""
        target = db.get_setting("move_to_dir")
        if not target or not os.path.isdir(target):
            target = DOWNLOAD_DIR
        try:
            usage = shutil.disk_usage(target)
            pct = (usage.used / usage.total) * 100
            if pct >= 90:
                logger.warning("Disk %.1f%% full — auto-pausing downloads", pct)
                return False
        except Exception:
            pass
        return True

    async def _process_loop(self):
        while self._running:
            if not self._paused:
                # Check disk space before processing queue
                if not self._check_disk_space():
                    self._paused = True
                    await self.broadcast({"type": "disk_full"})
                    await self.broadcast({"type": "queue_update"})
                    continue

                queued = db.get_by_status("queued")
                # Sort queue to prefer sites with fewer active downloads (diversity)
                queued.sort(key=lambda item: self.active_domains.get(_get_domain(item["url"]), 0))

                for item in queued:
                    if not self._running or self._paused:
                        break
                    if item["id"] in self.active_tasks:
                        continue
                    # Check per-site limit
                    domain = _get_domain(item["url"])
                    if self.active_domains.get(domain, 0) >= self.max_per_site:
                        continue  # Skip this one, try next from a different site
                    await self.semaphore.acquire()
                    if not self._running or self._paused:
                        self.semaphore.release()
                        break
                    self.active_domains[domain] = self.active_domains.get(domain, 0) + 1
                    task = asyncio.create_task(self._download_wrapper(item, domain=domain))
                    self.active_tasks[item["id"]] = task

            self._queue_event.clear()
            try:
                await asyncio.wait_for(self._queue_event.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                pass

    async def pause_one(self, download_id: int):
        """Pause a single active download without releasing its queue slot."""
        self._individually_paused.add(download_id)
        task = self.active_tasks.get(download_id)
        if task:
            dl = next((d for d in db.get_by_status("downloading") if d["id"] == download_id), None)
            if dl:
                _cancel_flag[dl["url"]] = True
            task.cancel()
        else:
            db.update_status(download_id, "paused")
            await self.broadcast({"type": "queue_update"})

    async def _download_wrapper(self, item, use_semaphore=True, domain=None):
        download_id = item["id"]
        _skip_semaphore_release = False
        if domain is None:
            domain = _get_domain(item["url"])
        try:
            start_time = datetime.now(timezone.utc).isoformat()
            db.update_status(download_id, "downloading")
            await self.broadcast({
                "type": "status_change",
                "id": download_id,
                "status": "downloading",
                "started_at": start_time,
            })

            result = await asyncio.to_thread(self._do_download, item)

            if result.get("is_playlist"):
                db.update_status(
                    download_id, "failed",
                    error_message="Playlist detected — moved to Playlists panel",
                )
                db.add_playlist(item["url"], result.get("title", ""), result.get("entries", []))
                await self.broadcast({"type": "playlist_update"})
                await self.broadcast({
                    "type": "status_change",
                    "id": download_id,
                    "status": "failed",
                    "error": "Playlist detected — moved to Playlists panel",
                })
            elif result["success"]:
                now = datetime.now(timezone.utc).isoformat()
                saved_path = result.get("filepath", "")

                # Check if user set a custom title while downloading
                current_db = next((d for d in db.get_by_status("downloading") if d["id"] == download_id), None)
                custom_title = None
                if current_db and current_db.get("title") and current_db["title"] != result.get("title", ""):
                    custom_title = current_db["title"]

                # Rename file if custom title was set
                if custom_title and saved_path and os.path.exists(saved_path):
                    safe_title = re.sub(r'[<>:"/\\|?*]', '_', custom_title)[:150]
                    ext = os.path.splitext(saved_path)[1]
                    new_name = f"{safe_title}{ext}"
                    new_path = os.path.join(os.path.dirname(saved_path), new_name)
                    try:
                        os.rename(saved_path, new_path)
                        saved_path = new_path
                        logger.info("Renamed to custom title: %s", new_name)
                    except Exception as e:
                        logger.warning("Failed to rename: %s", e)

                final_title = custom_title or result.get("title", "")
                db.update_status(
                    download_id,
                    "completed",
                    title=final_title,
                    filename=saved_path,
                    filesize=result.get("filesize", ""),
                    progress=100.0,
                    completed_at=now,
                )
                move_dir = db.get_setting("move_to_dir")
                if move_dir and os.path.isdir(move_dir) and saved_path:
                    try:
                        # Preserve subfolder structure
                        subfolder = item.get("subfolder", "")
                        if subfolder:
                            dest_dir = os.path.join(move_dir, subfolder)
                            os.makedirs(dest_dir, exist_ok=True)
                        else:
                            dest_dir = move_dir
                        dest = os.path.join(dest_dir, os.path.basename(saved_path))
                        shutil.move(saved_path, dest)
                        saved_path = dest
                        db.update_status(download_id, "completed", filename=dest)
                        logger.info("Moved %s -> %s", result["filepath"], dest)
                    except Exception as e:
                        logger.warning("Failed to move file: %s", e)

                await self.broadcast({
                    "type": "status_change",
                    "id": download_id,
                    "status": "completed",
                    "title": final_title,
                    "filename": saved_path,
                })
            else:
                now = datetime.now(timezone.utc).isoformat()
                db.update_status(
                    download_id, "failed",
                    error_message=result.get("error", "Unknown error"),
                    completed_at=now,
                )
                await self.broadcast({
                    "type": "status_change",
                    "id": download_id,
                    "status": "failed",
                    "error": result.get("error", "Unknown error"),
                })
        except asyncio.CancelledError:
            was_individual = download_id in self._individually_paused
            self._individually_paused.discard(download_id)
            db.update_status(download_id, "paused")
            await self.broadcast({
                "type": "status_change", "id": download_id, "status": "paused"
            })
            if was_individual:
                _skip_semaphore_release = True
        except Exception as e:
            clean_err = _strip_ansi(str(e))
            logger.exception("Unexpected error downloading %s", item["url"])
            db.update_status(download_id, "failed", error_message=clean_err)
            await self.broadcast({
                "type": "status_change",
                "id": download_id,
                "status": "failed",
                "error": clean_err,
            })
        finally:
            self.active_tasks.pop(download_id, None)
            if domain and domain in self.active_domains:
                self.active_domains[domain] = max(0, self.active_domains[domain] - 1)
                if self.active_domains[domain] == 0:
                    del self.active_domains[domain]
            if use_semaphore and not _skip_semaphore_release:
                self.semaphore.release()

    def _do_download(self, item):
        download_id = item["id"]
        url = item["url"]
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)

        # First, check if this is a playlist by extracting info without downloading
        try:
            check_opts = {**_base_ydl_opts(), "extract_flat": "in_playlist"}
            with yt_dlp.YoutubeDL(check_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if info and info.get("_type") == "playlist":
                    entries = []
                    for entry in info.get("entries", []):
                        if entry:
                            video_url = entry.get("url", "")
                            if video_url and not video_url.startswith("http"):
                                video_url = entry.get("webpage_url", video_url)
                            entries.append({
                                "url": video_url,
                                "title": entry.get("title", "Unknown"),
                                "duration": entry.get("duration") or 0,
                            })
                    playlist_title = _best_playlist_title(info, url)

                    # Check for pagination — try fetching additional pages
                    page_num = 2
                    base_url = url.rstrip("/")
                    # Strip existing page number from URL if present
                    base_url = re.sub(r'/\d+/?$', '', base_url)
                    while True:
                        page_url = f"{base_url}/{page_num}/"
                        try:
                            logger.info("Checking playlist page %d: %s", page_num, page_url)
                            page_info = ydl.extract_info(page_url, download=False)
                            if not page_info or page_info.get("_type") != "playlist":
                                break
                            page_entries = list(page_info.get("entries", []))
                            if not page_entries:
                                break
                            new_count = 0
                            seen_urls = {e["url"] for e in entries}
                            for entry in page_entries:
                                if entry:
                                    video_url = entry.get("url", "")
                                    if video_url and not video_url.startswith("http"):
                                        video_url = entry.get("webpage_url", video_url)
                                    if video_url not in seen_urls:
                                        entries.append({
                                            "url": video_url,
                                            "title": entry.get("title", "Unknown"),
                                            "duration": entry.get("duration") or 0,
                                        })
                                        seen_urls.add(video_url)
                                        new_count += 1
                            logger.info("Page %d: %d new entries (total: %d)", page_num, new_count, len(entries))
                            if new_count == 0:
                                break
                            page_num += 1
                        except Exception:
                            break

                    return {
                        "success": False,
                        "is_playlist": True,
                        "title": playlist_title,
                        "entries": entries,
                    }
        except Exception as e:
            err = _strip_ansi(str(e))
            if "403" in err or "Forbidden" in err:
                logger.warning("Got 403 for %s — cookies may be expired", url)
                return {
                    "success": False,
                    "error": f"403 Forbidden — re-export cookies.txt from your browser after visiting {_get_domain(url)}",
                }
            # Otherwise just proceed to normal download attempt

        # Check if this video is already downloaded by extracting info and checking the file
        try:
            subfolder = item.get("subfolder", "")
            check_dir = os.path.join(DOWNLOAD_DIR, subfolder) if subfolder else DOWNLOAD_DIR
            check_template = os.path.join(check_dir, "[%(extractor)s] %(title).100s [%(id)s].%(ext)s")
            check_ydl_opts = {**_base_ydl_opts(), "outtmpl": check_template, "noplaylist": True}
            with yt_dlp.YoutubeDL(check_ydl_opts) as ydl:
                check_info = ydl.extract_info(url, download=False)
                if check_info:
                    expected = ydl.prepare_filename(check_info)
                    base, _ = os.path.splitext(expected)
                    for ext in (".mp4", ".mkv", ".webm"):
                        if os.path.exists(base + ext):
                            logger.info("Already downloaded, skipping: %s", base + ext)
                            return {
                                "success": True,
                                "title": check_info.get("title", "Unknown"),
                                "filename": os.path.basename(base + ext),
                                "filepath": base + ext,
                                "filesize": "",
                            }
        except Exception:
            pass  # Couldn't check, proceed with download

        result = {"success": False}
        _title_sent = [False]  # track whether we've sent the title yet

        def progress_hook(d):
            if d["status"] == "downloading":
                # Send title on first progress update (extraction is done)
                if not _title_sent[0]:
                    title = d.get("info_dict", {}).get("title", "Downloading...")
                    _title_sent[0] = True
                    db.update_status(download_id, "downloading", title=title)
                    if self._loop and self._loop.is_running():
                        asyncio.run_coroutine_threadsafe(
                            self.broadcast({
                                "type": "status_change",
                                "id": download_id,
                                "status": "downloading",
                                "title": title,
                            }),
                            self._loop,
                        )

                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                downloaded = d.get("downloaded_bytes", 0)
                pct = (downloaded / total) * 100 if total > 0 else 0
                speed = _strip_ansi(d.get("_speed_str", "").strip())
                eta = _strip_ansi(d.get("_eta_str", "").strip())
                filesize = _strip_ansi(d.get("_total_bytes_str", "").strip() or d.get("_total_bytes_estimate_str", "").strip())

                db.update_progress(download_id, pct, speed, eta, filesize)

                if self._loop and self._loop.is_running():
                    asyncio.run_coroutine_threadsafe(
                        self.broadcast({
                            "type": "progress",
                            "id": download_id,
                            "progress": round(pct, 1),
                            "speed": speed,
                            "eta": eta,
                            "filesize": filesize,
                        }),
                        self._loop,
                    )

        subfolder = item.get("subfolder", "")
        if subfolder:
            output_dir = os.path.join(DOWNLOAD_DIR, subfolder)
        else:
            output_dir = DOWNLOAD_DIR
        os.makedirs(output_dir, exist_ok=True)

        ydl_opts = {
            **_base_ydl_opts(),
            "outtmpl": os.path.join(output_dir, "[%(extractor)s] %(title).100s [%(id)s].%(ext)s"),
            "progress_hooks": [progress_hook],
            "concurrent_fragment_downloads": 4,
            "noplaylist": True,
            "merge_output_format": "mp4",
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if info:
                    filename = ydl.prepare_filename(info)
                    base, _ = os.path.splitext(filename)
                    for ext in (".mp4", ".mkv", ".webm"):
                        if os.path.exists(base + ext):
                            filename = base + ext
                            break
                    result = {
                        "success": True,
                        "title": info.get("title", "Unknown"),
                        "filename": os.path.basename(filename),
                        "filepath": filename,
                        "filesize": info.get("filesize_approx_str", ""),
                    }
        except Exception as e:
            err = _strip_ansi(str(e))
            if "Unsupported URL" in err or "No video formats found" in err:
                # Fallback: scrape the page directly for video URLs
                cookies = COOKIES_FILE if os.path.isfile(COOKIES_FILE) else None

                def fallback_progress(pct, speed, size_str):
                    db.update_progress(download_id, pct, speed, "", size_str)
                    if self._loop and self._loop.is_running():
                        asyncio.run_coroutine_threadsafe(
                            self.broadcast({
                                "type": "progress",
                                "id": download_id,
                                "progress": round(pct, 1),
                                "speed": speed,
                                "eta": "",
                                "filesize": size_str,
                            }),
                            self._loop,
                        )

                result = _fallback_scrape(url, output_dir, cookies, progress_callback=fallback_progress)
            else:
                result = {"success": False, "error": err}

        return result
