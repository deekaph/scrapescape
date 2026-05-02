# ScrapeScape

A local web app for downloading and managing videos and music from the web.

## What is it

v0.2 was a pair of bash scripts. v1.0 was a complete rewrite as a local web app with queue management and a proper UI. v1.1 adds a dedicated **Music tab** for downloading and organizing music with full metadata, artist discography scanning, and DJ mix detection.

In short, I got tired of manually viewing page sources and hunting for media URLs. ScrapeScape handles the whole pipeline: paste a URL, it figures out where the media is, downloads it, and keeps track of what you've already grabbed so you don't end up with duplicates.

It uses yt-dlp as the primary download engine, which supports thousands of sites out of the box. For sites that yt-dlp doesn't support natively, there's a fallback scraper that parses the page source for video URLs, and if the site uses Cloudflare protection, it can spin up a headless browser to get past that too.

## Features

### Video Tab

- **Web UI** — dark themed, real-time progress via WebSocket, runs on localhost
- **Queue management** — add URLs manually, import from Chrome bookmarks, bulk release in batches
- **Concurrent downloads** — adjustable total and per-site limits so one slow site doesn't hog the queue
- **Playlist detection** — automatically detects playlists, extracts video lists, lets you browse/filter/download
- **Duration filtering** — download only videos over a minimum length (handy for skipping previews)
- **Download history** — persistent SQLite database tracks everything you've downloaded, survives restarts, prevents duplicates even after files are moved
- **Pause/resume** — pause all or individual downloads, resume where you left off
- **Move completed files** — auto-move finished downloads to a directory of your choice with a browse picker, preserves subfolder structure
- **Rename while downloading** — click a title in active downloads to change the filename before it saves
- **Disk space monitoring** — shows drive usage, auto-pauses downloads when the target drive hits 90%
- **Fallback scraping** — for unsupported sites, scrapes the page for video URLs, handles HLS streams, and uses a headless browser as a last resort
- **Cookie support** — drop a cookies.txt file in the project root for sites that need authentication
- **Server log panel** — collapsible panel showing real-time server messages right in the UI

### Music Tab

- **Track downloads** — paste a music URL and download audio with embedded metadata (artist, album, track number)
- **Artist discography scanning** — scan an artist page to browse and queue their full catalog (albums, singles, EPs)
- **Mix playlist extraction** — scan a playlist of DJ mixes, browse entries, and queue selected mixes
- **DJ mix detection** — automatically detects long-form mixes (45+ minutes) and saves them to a dedicated `DJ Mixes/` folder with a metadata text file
- **Album handling** — detects album/playlist URLs, extracts all tracks with metadata, and queues them in order
- **Organized file structure** — downloads organized as `Artist/Album/Artist - Album - 01 - Title.ext`, with a "one hit wonder" mode for flat storage
- **Multiple audio formats** — opus, mp3, m4a, flac, wav (requires ffmpeg)
- **Rate limit detection** — auto-pauses when rate limited and notifies you to switch VPN or wait
- **Metadata embedding** — track number, artist, album, and thumbnail embedded in downloaded files
- **Separate queue and controls** — independent concurrency, folder, and format settings from the video tab

## Dependencies

- Python 3.10+
- ffmpeg (required for audio extraction and video/audio stream merging)

Python packages (installed via pip):

- fastapi + uvicorn — web server
- yt-dlp — video and audio extraction
- curl_cffi — browser TLS fingerprint impersonation for sites that block bots
- playwright — headless browser fallback for Cloudflare-protected streams (optional but recommended)
- ytmusicapi — music service API for artist discography extraction

## Setup

```bash
pip install -r requirements.txt

# Only needed if you want the headless browser fallback
playwright install chromium

python run.py
```

Then open **http://127.0.0.1:8888** in your browser, or access it from any device on your LAN via the host machine's IP (e.g. `http://192.168.x.x:8888`). The UI has two tabs at the top: **Video** and **Music**.

## Cookie Support

Some sites require authentication. Export your cookies from your browser using a cookies.txt extension, save the file as `cookies.txt` in the project root, and ScrapeScape will use them automatically.

HOWEVER, be advised that by signing in you are identifying yourself to that service and if they detect scraping behaviour, it will be you that they punish. My personal recommendation is that you set your concurrent downloads low and use a VPN and sip the data rather than engage the firehose.

## Bookmark Import

Export your browser bookmarks and save as `bookmarks.txt` in the project root. The import UI lets you filter by domain and select which ones to queue up.

## Queue Batching

When you import a big playlist or a pile of bookmarks, items enter the queue as "pending" rather than starting immediately. Use the release controls to send them through in batches of 50-500 at a time, or release all at once. The queue pauses between batches and waits for your input.

## Legacy Scripts (v0.2)

The original bash scripts are still in the `scripts/` directory if you want something simpler. They use wget/curl and work on any Linux box without Python.

## Privacy

Everything runs locally. Download history is stored in `scrapescape.db`, video files go to `downloads/`, music files go to `music/` (or wherever you configure each). Nothing is sent to any external service. Of course, be aware that your traffic and origin is always observable by upstream providers unless you take the necessary precautions.
