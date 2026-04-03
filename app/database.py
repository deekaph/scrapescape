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

        # Migrations — safely add columns that may not exist on older DBs
        _migrate_add_column(conn, "downloads", "subfolder", "TEXT DEFAULT ''")
        _migrate_add_column(conn, "downloads", "cleared", "INTEGER DEFAULT 0")
        _migrate_add_column(conn, "playlists", "own_folder", "INTEGER DEFAULT 0")

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
