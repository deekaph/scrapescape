# ScrapeScape

For backing up videos embedded in web pages.

## What is it

v0.2 was a pair of bash scripts. v1.0 is a complete rewrite — a local web app with a proper UI, queue management, and a lot more under the hood.

In short, I got tired of manually viewing page sources and hunting for video URLs. ScrapeScape handles the whole pipeline: paste a URL, it figures out where the video is, downloads it, and keeps track of what you've already grabbed so you don't end up with duplicates.

It uses yt-dlp as the primary download engine, which supports thousands of sites out of the box. For sites that yt-dlp doesn't support natively, there's a fallback scraper that parses the page source for video URLs, and if the site uses Cloudflare protection, it can spin up a headless browser to get past that too.

## Features

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

## Dependencies

- Python 3.10+
- ffmpeg (recommended, for merging video/audio streams)

Python packages (installed via pip):

- fastapi + uvicorn — web server
- yt-dlp — video extraction and downloading
- curl_cffi — browser TLS fingerprint impersonation for sites that block bots
- playwright — headless browser fallback for Cloudflare-protected streams (optional but recommended)

## Setup

```bash
pip install -r requirements.txt

# Only needed if you want the headless browser fallback
playwright install chromium

python run.py
```

Then open **http://127.0.0.1:8888** in your browser.

## Cookie Support

Some sites require authentication. Export your cookies from Firefox using the [cookies.txt extension](https://addons.mozilla.org/en-US/firefox/addon/cookies-txt/), save the file as `cookies.txt` in the project root, and ScrapeScape will use them automatically.

## Bookmark Import

Export your Chrome bookmarks (chrome://bookmarks > three dots > Export) and save as `bookmarks.txt` in the project root. The import UI lets you filter by domain and select which ones to queue up.

## Queue Batching

When you import a big playlist or a pile of bookmarks, items enter the queue as "pending" rather than starting immediately. Use the release controls to send them through in batches of 50-500 at a time, or release all at once. The queue pauses between batches and waits for your input.

## Legacy Scripts (v0.2)

The original bash scripts are still in the `scripts/` directory if you want something simpler. They use wget/curl and work on any Linux box without Python.

## Privacy

Everything runs locally. Download history is stored in `scrapescape.db`, files go to `downloads/` (or wherever you configure). Nothing is sent to any external service. Of course, be aware that your traffic and origin is always observable by upstream providers unless you take the necessary precautions.
