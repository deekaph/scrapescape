import sqlite3
import json
import os
from contextlib import contextmanager

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scrapescape.db")


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _migrate_add_column(conn, table: str, column: str, definition: str):
    """Add a column to a table if it doesn't exist."""
    cols = [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS downloads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT UNIQUE NOT NULL,
                title TEXT,
                filename TEXT,
                status TEXT DEFAULT 'pending',
                progress REAL DEFAULT 0.0,
                speed TEXT,
                filesize TEXT,
                eta TEXT,
                error_message TEXT,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP,
                source TEXT DEFAULT 'manual',
                subfolder TEXT DEFAULT '',
                cleared INTEGER DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS playlists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT UNIQUE NOT NULL,
                title TEXT,
                entries TEXT DEFAULT '[]',
                own_folder INTEGER DEFAULT 1,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS music_downloads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT UNIQUE NOT NULL,
                artist TEXT DEFAULT '',
                album TEXT DEFAULT '',
                track_number INTEGER DEFAULT 0,
                title TEXT DEFAULT '',
                filename TEXT,
                status TEXT DEFAULT 'queued',
                progress REAL DEFAULT 0.0,
                speed TEXT,
                filesize TEXT,
                eta TEXT,
                error_message TEXT,
                audio_format TEXT DEFAULT 'mp3',
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP,
                cleared INTEGER DEFAULT 0
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS music_mix_playlists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT UNIQUE NOT NULL,
                name TEXT DEFAULT '',
                mixes TEXT DEFAULT '[]',
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS music_artists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT UNIQUE NOT NULL,
                name TEXT DEFAULT '',
                releases TEXT DEFAULT '[]',
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Migrations — safely add columns that may not exist on older DBs
        _migrate_add_column(conn, "downloads", "subfolder", "TEXT DEFAULT ''")
        _migrate_add_column(conn, "downloads", "cleared", "INTEGER DEFAULT 0")
        _migrate_add_column(conn, "playlists", "own_folder", "INTEGER DEFAULT 0")
        _migrate_add_column(conn, "music_downloads", "one_hit_wonder", "INTEGER DEFAULT 0")
        _migrate_add_column(conn, "music_downloads", "album_artist", "TEXT DEFAULT ''")

        # Default settings
        conn.execute("""
            INSERT OR IGNORE INTO settings (key, value) VALUES ('move_to_dir', '')
        """)
        conn.execute("""
            INSERT OR IGNORE INTO settings (key, value) VALUES ('max_concurrent', '3')
        """)
        conn.execute("""
            INSERT OR IGNORE INTO settings (key, value) VALUES ('max_per_site', '2')
        """)
        conn.execute("""
            INSERT OR IGNORE INTO settings (key, value) VALUES ('music_base_dir', '')
        """)
        conn.execute("""
            INSERT OR IGNORE INTO settings (key, value) VALUES ('music_audio_format', 'opus')
        """)
        conn.execute("""
            INSERT OR IGNORE INTO settings (key, value) VALUES ('music_concurrent', '3')
        """)


# --- Downloads ---

def add_url(url: str, source: str = "manual", subfolder: str = "", status: str = "pending") -> dict:
    with get_db() as conn:
        try:
            cursor = conn.execute(
                "INSERT INTO downloads (url, source, subfolder, status) VALUES (?, ?, ?, ?)",
                (url, source, subfolder, status),
            )
            row = conn.execute(
                "SELECT * FROM downloads WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
            return {"added": True, "download": dict(row)}
        except sqlite3.IntegrityError:
            row = conn.execute(
                "SELECT * FROM downloads WHERE url = ?", (url,)
            ).fetchone()
            d = dict(row)
            # Allow re-queueing failed items
            if d["status"] == "failed":
                conn.execute(
                    "UPDATE downloads SET status = 'queued', progress = 0.0, error_message = '', subfolder = ? WHERE url = ?",
                    (subfolder or d.get("subfolder", ""), url),
                )
                row = conn.execute("SELECT * FROM downloads WHERE url = ?", (url,)).fetchone()
                return {"added": True, "download": dict(row)}
            return {"added": False, "download": d}


def add_urls_bulk(urls: list[str], source: str = "bookmarks", subfolder: str = "") -> dict:
    added = 0
    skipped = 0
    with get_db() as conn:
        for url in urls:
            try:
                conn.execute(
                    "INSERT INTO downloads (url, source, subfolder) VALUES (?, ?, ?)",
                    (url, source, subfolder),
                )
                added += 1
            except sqlite3.IntegrityError:
                # Re-queue failed items instead of skipping
                row = conn.execute(
                    "SELECT status FROM downloads WHERE url = ?", (url,)
                ).fetchone()
                if row and row["status"] == "failed":
                    conn.execute(
                        "UPDATE downloads SET status = 'queued', progress = 0.0, error_message = '', subfolder = COALESCE(NULLIF(?, ''), subfolder) WHERE url = ?",
                        (subfolder, url),
                    )
                    added += 1
                else:
                    skipped += 1
    return {"added": added, "skipped": skipped}


def get_by_status(status: str) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM downloads WHERE status = ? ORDER BY added_at DESC",
            (status,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_all() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM downloads WHERE cleared = 0 ORDER BY added_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_pending_count() -> int:
    with get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM downloads WHERE status = 'pending' AND cleared = 0"
        ).fetchone()
        return row["cnt"]


def release_next(count: int) -> int:
    """Move the next N pending items to queued status. Returns how many were released."""
    with get_db() as conn:
        cursor = conn.execute(
            "UPDATE downloads SET status = 'queued' WHERE id IN "
            "(SELECT id FROM downloads WHERE status = 'pending' AND cleared = 0 ORDER BY added_at ASC LIMIT ?)",
            (count,),
        )
        return cursor.rowcount


def release_all() -> int:
    """Move all pending items to queued."""
    with get_db() as conn:
        cursor = conn.execute(
            "UPDATE downloads SET status = 'queued' WHERE status = 'pending' AND cleared = 0"
        )
        return cursor.rowcount


def clear_completed() -> int:
    """Mark completed downloads as cleared. They stay in DB for duplicate detection."""
    with get_db() as conn:
        cursor = conn.execute(
            "UPDATE downloads SET cleared = 1 WHERE status = 'completed' AND cleared = 0"
        )
        return cursor.rowcount


def mark_url_completed(url: str, title: str = "", filename: str = ""):
    """Insert or update a URL as completed+cleared. Used when recovering history from disk."""
    with get_db() as conn:
        try:
            conn.execute(
                "INSERT INTO downloads (url, title, filename, status, progress, cleared) VALUES (?, ?, ?, 'completed', 100.0, 1)",
                (url, title, filename),
            )
        except sqlite3.IntegrityError:
            # Already exists — update to completed if it was queued/failed
            conn.execute(
                "UPDATE downloads SET status = 'completed', progress = 100.0, cleared = 1, title = COALESCE(NULLIF(?, ''), title), filename = COALESCE(NULLIF(?, ''), filename) WHERE url = ? AND status NOT IN ('completed', 'downloading')",
                (title, filename, url),
            )


def update_status(download_id: int, status: str, **kwargs):
    with get_db() as conn:
        fields = ["status = ?"]
        values = [status]
        for key, val in kwargs.items():
            fields.append(f"{key} = ?")
            values.append(val)
        values.append(download_id)
        conn.execute(
            f"UPDATE downloads SET {', '.join(fields)} WHERE id = ?",
            values,
        )


def update_progress(download_id: int, progress: float, speed: str = "", eta: str = "", filesize: str = ""):
    with get_db() as conn:
        conn.execute(
            "UPDATE downloads SET progress = ?, speed = ?, eta = ?, filesize = ? WHERE id = ?",
            (progress, speed, eta, filesize, download_id),
        )


def reset_to_queued(download_id: int):
    with get_db() as conn:
        conn.execute(
            "UPDATE downloads SET status = 'queued', progress = 0.0, speed = '', eta = '', error_message = '' WHERE id = ?",
            (download_id,),
        )


def delete_download(download_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM downloads WHERE id = ?", (download_id,))


# --- Playlists ---

def add_playlist(url: str, title: str, entries: list[dict]) -> dict:
    with get_db() as conn:
        entries_json = json.dumps(entries)
        try:
            conn.execute(
                "INSERT INTO playlists (url, title, entries) VALUES (?, ?, ?)",
                (url, title, entries_json),
            )
            return {"added": True}
        except sqlite3.IntegrityError:
            # Update entries if playlist already exists
            conn.execute(
                "UPDATE playlists SET title = ?, entries = ? WHERE url = ?",
                (title, entries_json, url),
            )
            return {"added": False, "updated": True}


def get_playlists() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM playlists ORDER BY added_at DESC"
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["entries"] = json.loads(d["entries"])
            d["own_folder"] = bool(d.get("own_folder", 0))
            result.append(d)
        return result


def get_playlist_by_id(playlist_id: int) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM playlists WHERE id = ?", (playlist_id,)
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["entries"] = json.loads(d["entries"])
        d["own_folder"] = bool(d.get("own_folder", 0))
        return d


def set_playlist_own_folder(playlist_id: int, enabled: bool):
    with get_db() as conn:
        conn.execute(
            "UPDATE playlists SET own_folder = ? WHERE id = ?",
            (1 if enabled else 0, playlist_id),
        )


def delete_playlist(playlist_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM playlists WHERE id = ?", (playlist_id,))


# --- Settings ---

def get_setting(key: str) -> str:
    with get_db() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else ""


def set_setting(key: str, value: str):
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, value),
        )


# --- Music Downloads ---

def music_add_url(url: str, artist: str = "", album: str = "", track_number: int = 0,
                  title: str = "", audio_format: str = "mp3", one_hit_wonder: bool = False) -> dict:
    with get_db() as conn:
        try:
            cursor = conn.execute(
                "INSERT INTO music_downloads (url, artist, album, track_number, title, audio_format, one_hit_wonder) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (url, artist, album, track_number, title, audio_format, 1 if one_hit_wonder else 0),
            )
            row = conn.execute(
                "SELECT * FROM music_downloads WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
            return {"added": True, "download": dict(row)}
        except sqlite3.IntegrityError:
            row = conn.execute(
                "SELECT * FROM music_downloads WHERE url = ?", (url,)
            ).fetchone()
            d = dict(row)
            if d["status"] == "failed":
                conn.execute(
                    "UPDATE music_downloads SET status = 'queued', progress = 0.0, error_message = '' WHERE url = ?",
                    (url,),
                )
                row = conn.execute("SELECT * FROM music_downloads WHERE url = ?", (url,)).fetchone()
                return {"added": True, "download": dict(row)}
            return {"added": False, "download": d}


def music_add_album(urls_with_meta: list[dict], audio_format: str = "mp3",
                    force: bool = False, album_artist: str = "") -> dict:
    """Add multiple tracks. Each dict: {url, artist, album, track_number, title}.
    album_artist is the top-level artist who owns the album (used for directory).
    If force=True, re-queue completed/failed tracks (for redownloading an album)."""
    added = 0
    skipped = 0
    with get_db() as conn:
        for item in urls_with_meta:
            try:
                conn.execute(
                    "INSERT INTO music_downloads (url, artist, album, track_number, title, audio_format, album_artist) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (item["url"], item.get("artist", ""), item.get("album", ""),
                     item.get("track_number", 0), item.get("title", ""), audio_format, album_artist),
                )
                added += 1
            except sqlite3.IntegrityError:
                row = conn.execute(
                    "SELECT status FROM music_downloads WHERE url = ?", (item["url"],)
                ).fetchone()
                if row and row["status"] in ("failed", "completed") and (force or row["status"] == "failed"):
                    conn.execute(
                        "UPDATE music_downloads SET status = 'queued', progress = 0.0, error_message = '', "
                        "cleared = 0, artist = ?, album = ?, track_number = ?, title = ?, album_artist = ? WHERE url = ?",
                        (item.get("artist", ""), item.get("album", ""),
                         item.get("track_number", 0), item.get("title", ""), album_artist, item["url"]),
                    )
                    added += 1
                else:
                    skipped += 1
    return {"added": added, "skipped": skipped}


def music_get_all() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM music_downloads WHERE cleared = 0 ORDER BY added_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def music_get_by_status(status: str) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM music_downloads WHERE status = ? ORDER BY artist, album, track_number",
            (status,),
        ).fetchall()
        return [dict(r) for r in rows]


def music_update_status(download_id: int, status: str, **kwargs):
    with get_db() as conn:
        fields = ["status = ?"]
        values = [status]
        for key, val in kwargs.items():
            fields.append(f"{key} = ?")
            values.append(val)
        values.append(download_id)
        conn.execute(
            f"UPDATE music_downloads SET {', '.join(fields)} WHERE id = ?",
            values,
        )


def music_update_progress(download_id: int, progress: float, speed: str = "", eta: str = "", filesize: str = ""):
    with get_db() as conn:
        conn.execute(
            "UPDATE music_downloads SET progress = ?, speed = ?, eta = ?, filesize = ? WHERE id = ?",
            (progress, speed, eta, filesize, download_id),
        )


def music_delete(download_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM music_downloads WHERE id = ?", (download_id,))


def music_clear_completed() -> int:
    with get_db() as conn:
        cursor = conn.execute(
            "UPDATE music_downloads SET cleared = 1 WHERE status = 'completed' AND cleared = 0"
        )
        return cursor.rowcount


def music_reset_to_queued(download_id: int):
    with get_db() as conn:
        conn.execute(
            "UPDATE music_downloads SET status = 'queued', progress = 0.0, speed = '', eta = '', error_message = '' WHERE id = ?",
            (download_id,),
        )


def music_clear_queue() -> int:
    """Delete all queued (not yet downloading) music items."""
    with get_db() as conn:
        cursor = conn.execute(
            "DELETE FROM music_downloads WHERE status = 'queued'"
        )
        return cursor.rowcount


def music_clear_failed() -> int:
    """Delete all failed music items."""
    with get_db() as conn:
        cursor = conn.execute(
            "DELETE FROM music_downloads WHERE status = 'failed'"
        )
        return cursor.rowcount


def music_retry_all_failed() -> int:
    """Reset all failed music items back to queued."""
    with get_db() as conn:
        cursor = conn.execute(
            "UPDATE music_downloads SET status = 'queued', progress = 0.0, speed = '', eta = '', error_message = '' WHERE status = 'failed'"
        )
        return cursor.rowcount


# --- Music Artists ---

def music_artist_save(url: str, name: str, releases: list[dict]) -> dict:
    """Save or update an artist's discovered releases."""
    with get_db() as conn:
        releases_json = json.dumps(releases)
        try:
            cursor = conn.execute(
                "INSERT INTO music_artists (url, name, releases) VALUES (?, ?, ?)",
                (url, name, releases_json),
            )
            row = conn.execute(
                "SELECT * FROM music_artists WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
            d = dict(row)
            d["releases"] = json.loads(d["releases"])
            return d
        except sqlite3.IntegrityError:
            conn.execute(
                "UPDATE music_artists SET name = ?, releases = ? WHERE url = ?",
                (name, releases_json, url),
            )
            row = conn.execute(
                "SELECT * FROM music_artists WHERE url = ?", (url,)
            ).fetchone()
            d = dict(row)
            d["releases"] = json.loads(d["releases"])
            return d


def music_artist_get_all() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM music_artists ORDER BY added_at DESC"
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["releases"] = json.loads(d["releases"])
            result.append(d)
        return result


def music_artist_delete(artist_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM music_artists WHERE id = ?", (artist_id,))


# --- Mix Playlists ---

def music_mix_save(url: str, name: str, mixes: list[dict]) -> dict:
    """Save or update a scanned mix playlist."""
    with get_db() as conn:
        mixes_json = json.dumps(mixes)
        try:
            cursor = conn.execute(
                "INSERT INTO music_mix_playlists (url, name, mixes) VALUES (?, ?, ?)",
                (url, name, mixes_json),
            )
            row = conn.execute(
                "SELECT * FROM music_mix_playlists WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
            d = dict(row)
            d["mixes"] = json.loads(d["mixes"])
            return d
        except sqlite3.IntegrityError:
            conn.execute(
                "UPDATE music_mix_playlists SET name = ?, mixes = ? WHERE url = ?",
                (name, mixes_json, url),
            )
            row = conn.execute(
                "SELECT * FROM music_mix_playlists WHERE url = ?", (url,)
            ).fetchone()
            d = dict(row)
            d["mixes"] = json.loads(d["mixes"])
            return d


def music_mix_get_all() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM music_mix_playlists ORDER BY added_at DESC"
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["mixes"] = json.loads(d["mixes"])
            result.append(d)
        return result


def music_mix_delete(mix_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM music_mix_playlists WHERE id = ?", (mix_id,))
