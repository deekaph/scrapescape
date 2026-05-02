#!/usr/bin/env python3
"""
One-off script to rename files that got URL-derived titles like:
    [SpankBang] t7d00u [t7d00u].mp4

Looks up each file's URL in the database, re-extracts the title from the
site (without re-downloading), and renames the file.

Usage:
    python rename_existing.py              # dry run (shows what would be renamed)
    python rename_existing.py --apply      # actually rename files
"""

import os
import re
import sys
import sqlite3
import time

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(PROJECT_ROOT, "downloads")
DB_PATH = os.path.join(PROJECT_ROOT, "scrapescape.db")
COOKIES_FILE = os.path.join(PROJECT_ROOT, "cookies.txt")

DRY_RUN = "--apply" not in sys.argv


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def is_url_derived_title(title: str) -> bool:
    """Check if a title looks like it was derived from a URL rather than page metadata."""
    t = title.strip()
    if not t:
        return True
    # Short alphanumeric hash-like IDs (e.g. t7d00u, abc123)
    if re.match(r'^[a-zA-Z0-9_-]{3,12}$', t) and not re.search(r'[A-Z].*[a-z]|[a-z].*[A-Z].*[a-z]', t):
        return True
    # Contains :// or looks like a URL path
    if "://" in t or (t.count("/") >= 2 and " " not in t):
        return True
    return False


def find_files_to_rename():
    """Scan download dir for files with URL-derived titles and look up their URLs in the DB."""
    # Pattern: [Extractor] title [id].ext
    pattern = re.compile(r'^\[([^\]]+)\]\s+(.+?)\s+\[([^\]]+)\]\.(mp4|mkv|webm)$')

    conn = get_db()
    to_rename = []

    for root, dirs, files in os.walk(DOWNLOAD_DIR):
        for fname in files:
            m = pattern.match(fname)
            if not m:
                continue
            extractor, title, vid_id, ext = m.groups()
            if not is_url_derived_title(title):
                continue

            # Look up URL in database by filename
            filepath = os.path.join(root, fname)
            row = conn.execute(
                "SELECT url, title FROM downloads WHERE filename LIKE ? OR filename LIKE ?",
                (f"%{fname}%", f"%{vid_id}%")
            ).fetchone()

            if row:
                to_rename.append({
                    "filepath": filepath,
                    "filename": fname,
                    "extractor": extractor,
                    "vid_id": vid_id,
                    "ext": ext,
                    "url": row["url"],
                    "db_title": row["title"],
                })

    conn.close()
    return to_rename


def extract_title(url: str) -> str | None:
    """Use yt-dlp to extract just the title without downloading."""
    import yt_dlp

    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
    }
    if os.path.isfile(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info:
                title = info.get("title", "")
                if title and not is_url_derived_title(title):
                    return title
    except Exception as e:
        print(f"  ⚠ Failed to extract: {e}")
    return None


def main():
    if DRY_RUN:
        print("=== DRY RUN (use --apply to actually rename) ===\n")
    else:
        print("=== APPLYING RENAMES ===\n")

    files = find_files_to_rename()
    print(f"Found {len(files)} files with URL-derived titles\n")

    if not files:
        return

    renamed = 0
    failed = 0
    skipped = 0

    for i, f in enumerate(files, 1):
        print(f"[{i}/{len(files)}] {f['filename']}")
        print(f"  URL: {f['url']}")

        # Try to get the real title
        title = extract_title(f["url"])

        if not title:
            print(f"  ⚠ Could not extract a better title, skipping")
            skipped += 1
            # Rate limit: don't hammer the site
            time.sleep(1)
            continue

        safe_title = re.sub(r'[<>:"/\\|?*]', '_', title)[:100]
        new_name = f"[{f['extractor']}] {safe_title} [{f['vid_id']}].{f['ext']}"
        new_path = os.path.join(os.path.dirname(f["filepath"]), new_name)

        if os.path.exists(new_path):
            print(f"  ⚠ Target already exists: {new_name}")
            skipped += 1
            continue

        print(f"  → {new_name}")

        if not DRY_RUN:
            try:
                os.rename(f["filepath"], new_path)
                # Update DB too
                conn = get_db()
                conn.execute(
                    "UPDATE downloads SET title = ?, filename = ? WHERE url = ?",
                    (title, new_path, f["url"])
                )
                conn.commit()
                conn.close()
                renamed += 1
            except Exception as e:
                print(f"  ✗ Rename failed: {e}")
                failed += 1
        else:
            renamed += 1

        # Rate limit: 1 lookup per second to avoid bans
        time.sleep(1)

    print(f"\n{'Would rename' if DRY_RUN else 'Renamed'}: {renamed}")
    print(f"Skipped: {skipped}")
    if failed:
        print(f"Failed: {failed}")


if __name__ == "__main__":
    main()
