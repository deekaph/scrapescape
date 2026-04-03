import asyncio
import collections
import os
import logging

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import database as db
from .downloader import DownloadManager
from .bookmarks import parse_chrome_bookmarks, get_domain_summary, filter_bookmarks

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("scrapescape")

# --- Log capture ---
# Ring buffer of recent log lines, broadcast to UI via WebSocket
log_buffer: collections.deque[str] = collections.deque(maxlen=500)
_ws_manager_ref = None  # set after ConnectionManager is created
_event_loop = None  # set on startup


class WebSocketLogHandler(logging.Handler):
    def emit(self, record):
        try:
            msg = self.format(record)
            log_buffer.append(msg)
            if _ws_manager_ref and _event_loop and _event_loop.is_running():
                _event_loop.call_soon_threadsafe(
                    lambda m=msg: _event_loop.create_task(
                        _ws_manager_ref.broadcast({"type": "log", "message": m})
                    ),
                )
        except Exception:
            pass


ws_log_handler = WebSocketLogHandler()
ws_log_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S"))

# Attach to root logger + specific loggers that might not propagate
logging.getLogger().addHandler(ws_log_handler)
for _logger_name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
    logging.getLogger(_logger_name).addHandler(ws_log_handler)

app = FastAPI(title="ScrapeScape")

# --- WebSocket connection manager ---

class ConnectionManager:
    def __init__(self):
        self.connections: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.connections.append(ws)

    def disconnect(self, ws: WebSocket):
        self.connections.remove(ws)

    async def broadcast(self, message: dict):
        dead = []
        for conn in self.connections:
            try:
                await conn.send_json(message)
            except Exception:
                dead.append(conn)
        for conn in dead:
            if conn in self.connections:
                self.connections.remove(conn)


ws_manager = ConnectionManager()
_ws_manager_ref = ws_manager
dl_manager: DownloadManager | None = None


# --- App lifecycle ---

@app.on_event("startup")
async def startup():
    global dl_manager, _event_loop
    _event_loop = asyncio.get_event_loop()
    db.init_db()
    dl_manager = DownloadManager(ws_manager.broadcast)
    await dl_manager.start()
    logger.info("ScrapeScape started on http://127.0.0.1:8888")


@app.on_event("shutdown")
async def shutdown():
    if dl_manager:
        await dl_manager.stop()


# --- Static files ---

static_dir = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
async def index():
    return FileResponse(os.path.join(static_dir, "index.html"))


@app.get("/api/logs")
async def get_logs():
    return {"logs": list(log_buffer)}


# --- WebSocket ---

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws_manager.connect(ws)
    try:
        while True:
            await ws.receive_text()  # keep alive
    except WebSocketDisconnect:
        ws_manager.disconnect(ws)


# --- Request models ---

class AddUrlRequest(BaseModel):
    url: str

class ConcurrencyRequest(BaseModel):
    max: int

class PerSiteRequest(BaseModel):
    max: int

class ImportConfirmRequest(BaseModel):
    urls: list[str]

class RenameRequest(BaseModel):
    title: str

class MoveToRequest(BaseModel):
    directory: str

class OwnFolderRequest(BaseModel):
    enabled: bool

class CancelRequest(BaseModel):
    delete_partial: bool = False

class QueueAllRequest(BaseModel):
    min_duration: int = 0  # minimum duration in seconds

class ReleaseRequest(BaseModel):
    count: int = 0  # 0 means release all



# --- Download API routes ---

@app.get("/api/downloads")
async def get_downloads():
    return db.get_all()


@app.get("/api/downloads/{status}")
async def get_downloads_by_status(status: str):
    if status not in ("queued", "downloading", "completed", "failed", "pending", "paused"):
        return {"error": "Invalid status"}
    return db.get_by_status(status)


@app.post("/api/add")
async def add_url(req: AddUrlRequest):
    url = req.url.strip()
    if not url:
        return {"error": "URL is required"}
    result = db.add_url(url, status="queued")
    if result["added"] and dl_manager:
        dl_manager.notify_queue()
    await ws_manager.broadcast({"type": "queue_update"})
    return result


@app.post("/api/import-bookmarks")
async def import_bookmarks_preview():
    bookmarks_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "bookmarks.txt"
    )
    if not os.path.exists(bookmarks_path):
        return {"error": "bookmarks.txt not found"}

    try:
        bookmarks = parse_chrome_bookmarks(bookmarks_path)
    except ValueError as e:
        return {"error": str(e)}

    summary = get_domain_summary(bookmarks)

    domain_counts = {domain: len(urls) for domain, urls in summary.items()}
    return {
        "total": len(bookmarks),
        "domains": domain_counts,
    }


@app.post("/api/import-bookmarks/confirm")
async def import_bookmarks_confirm(req: ImportConfirmRequest):
    if req.urls:
        result = db.add_urls_bulk(req.urls)
    else:
        result = {"added": 0, "skipped": 0}
    if result["added"] > 0 and dl_manager:
        dl_manager.notify_queue()
    await ws_manager.broadcast({"type": "queue_update"})
    return result


@app.post("/api/import-bookmarks/filter")
async def import_bookmarks_filter(req: ImportConfirmRequest):
    """Get filtered bookmark URLs by domain list. req.urls contains domain names."""
    bookmarks_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "bookmarks.txt"
    )
    bookmarks = parse_chrome_bookmarks(bookmarks_path)
    filtered = filter_bookmarks(bookmarks, req.urls)
    return {"urls": [b["url"] for b in filtered], "count": len(filtered)}


@app.post("/api/concurrency")
async def set_concurrency(req: ConcurrencyRequest):
    if dl_manager:
        dl_manager.set_concurrency(req.max)
    return {"max": dl_manager.max_concurrent if dl_manager else req.max}


@app.post("/api/per-site")
async def set_per_site(req: PerSiteRequest):
    if dl_manager:
        dl_manager.set_per_site(req.max)
    return {"max": dl_manager.max_per_site if dl_manager else req.max}


@app.post("/api/retry/{download_id}")
async def retry_download(download_id: int):
    db.reset_to_queued(download_id)
    if dl_manager:
        dl_manager.notify_queue()
    await ws_manager.broadcast({"type": "queue_update"})
    return {"ok": True}


@app.delete("/api/queue/{download_id}")
async def delete_from_queue(download_id: int):
    db.delete_download(download_id)
    await ws_manager.broadcast({"type": "queue_update"})
    return {"ok": True}


@app.post("/api/start-now/{download_id}")
async def start_now(download_id: int):
    if dl_manager:
        await dl_manager.start_now(download_id)
    return {"ok": True}


@app.post("/api/rename/{download_id}")
async def rename_download(download_id: int, req: RenameRequest):
    title = req.title.strip()
    if not title:
        return {"error": "Title is required"}
    db.update_status(download_id, "downloading", title=title)
    return {"ok": True}


@app.post("/api/clear-completed")
async def clear_completed():
    count = db.clear_completed()
    await ws_manager.broadcast({"type": "queue_update"})
    return {"ok": True, "cleared": count}


@app.get("/api/pending-count")
async def pending_count():
    return {"count": db.get_pending_count()}


@app.post("/api/release")
async def release_downloads(req: ReleaseRequest):
    if req.count <= 0:
        released = db.release_all()
    else:
        released = db.release_next(req.count)
    if released > 0 and dl_manager:
        dl_manager.notify_queue()
    await ws_manager.broadcast({"type": "queue_update"})
    return {"ok": True, "released": released}


@app.post("/api/hold-queue")
async def hold_queue():
    """Move all queued items back to pending."""
    with db.get_db() as conn:
        cursor = conn.execute(
            "UPDATE downloads SET status = 'pending' WHERE status = 'queued'"
        )
        count = cursor.rowcount
    await ws_manager.broadcast({"type": "queue_update"})
    return {"ok": True, "held": count}


@app.get("/api/downloads-folder-count")
async def downloads_folder_count():
    """Count files in the downloads folder for reference."""
    download_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "downloads")
    if not os.path.isdir(download_dir):
        return {"count": 0}
    count = 0
    for root, dirs, files in os.walk(download_dir):
        for f in files:
            if not f.endswith((".part", ".ytdl", ".temp")):
                count += 1
    return {"count": count}


@app.post("/api/start")
async def start_downloads():
    if dl_manager:
        await dl_manager.start()
    return {"ok": True}


@app.post("/api/pause")
async def pause_downloads():
    if dl_manager:
        await dl_manager.pause()
    await ws_manager.broadcast({"type": "queue_update"})
    return {"ok": True}


@app.post("/api/pause/{download_id}")
async def pause_one_download(download_id: int):
    if dl_manager:
        await dl_manager.pause_one(download_id)
    return {"ok": True}


@app.post("/api/resume-all")
async def resume_all():
    if dl_manager:
        await dl_manager.resume_all()
    return {"ok": True}


@app.post("/api/resume/{download_id}")
async def resume_one(download_id: int):
    if dl_manager:
        await dl_manager.resume_one(download_id)
    return {"ok": True}


@app.post("/api/cancel-all")
async def cancel_all(req: CancelRequest):
    if dl_manager:
        await dl_manager.cancel_all(delete_partial=req.delete_partial)
    return {"ok": True}


@app.post("/api/cancel/{download_id}")
async def cancel_one(download_id: int, req: CancelRequest):
    if dl_manager:
        await dl_manager.cancel_one(download_id, delete_partial=req.delete_partial)
    return {"ok": True}


# --- Playlist API routes ---

@app.get("/api/playlists")
async def get_playlists():
    return db.get_playlists()


@app.delete("/api/playlists/{playlist_id}")
async def delete_playlist(playlist_id: int):
    db.delete_playlist(playlist_id)
    await ws_manager.broadcast({"type": "playlist_update"})
    return {"ok": True}


@app.post("/api/playlists/{playlist_id}/own-folder")
async def set_playlist_own_folder(playlist_id: int, req: OwnFolderRequest):
    db.set_playlist_own_folder(playlist_id, req.enabled)
    return {"ok": True}


@app.post("/api/playlists/{playlist_id}/queue-all")
async def queue_all_playlist(playlist_id: int, req: QueueAllRequest = QueueAllRequest()):
    """Queue videos in a playlist for download, optionally filtered by minimum duration."""
    pl = db.get_playlist_by_id(playlist_id)
    if not pl:
        return {"error": "Playlist not found"}
    entries = pl["entries"]
    if req.min_duration > 0:
        entries = [e for e in entries if (e.get("duration") or 0) >= req.min_duration]
    urls = [e["url"] for e in entries if e.get("url")]
    subfolder = ""
    if pl.get("own_folder") and pl.get("title"):
        subfolder = "".join(c if c.isalnum() or c in " _-" else "_" for c in pl["title"]).strip()
    result = db.add_urls_bulk(urls, subfolder=subfolder)
    if result["added"] > 0 and dl_manager:
        dl_manager.notify_queue()
    # Remove playlist from list after queueing
    db.delete_playlist(playlist_id)
    await ws_manager.broadcast({"type": "queue_update"})
    await ws_manager.broadcast({"type": "playlist_update"})
    return {**result, "filtered": len(urls), "total": len(pl["entries"])}


# --- Settings API routes ---

@app.get("/api/settings")
async def get_settings():
    return {
        "move_to_dir": db.get_setting("move_to_dir"),
        "max_concurrent": int(db.get_setting("max_concurrent") or 3),
        "max_per_site": int(db.get_setting("max_per_site") or 2),
    }


@app.post("/api/settings/move-to")
async def set_move_to(req: MoveToRequest):
    directory = req.directory.strip()
    if directory and not os.path.isdir(directory):
        return {"error": f"Directory does not exist: {directory}"}
    db.set_setting("move_to_dir", directory)
    return {"ok": True, "move_to_dir": directory}


@app.get("/api/disk-usage")
async def disk_usage():
    """Return disk usage for the downloads directory or move-to directory."""
    import shutil
    target = db.get_setting("move_to_dir")
    if not target or not os.path.isdir(target):
        target = os.path.join(os.path.dirname(os.path.dirname(__file__)), "downloads")
        os.makedirs(target, exist_ok=True)
    try:
        usage = shutil.disk_usage(target)
        pct = (usage.used / usage.total) * 100
        return {
            "total_gb": round(usage.total / (1024**3), 1),
            "used_gb": round(usage.used / (1024**3), 1),
            "free_gb": round(usage.free / (1024**3), 1),
            "percent": round(pct, 1),
            "path": target,
        }
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/browse-folder")
async def browse_folder():
    """Open a native folder picker dialog."""
    import threading

    result = {"path": ""}

    def _pick():
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            folder = filedialog.askdirectory(title="Select folder for completed downloads")
            result["path"] = folder or ""
            root.destroy()
        except Exception:
            result["path"] = ""

    # Run in thread since tkinter blocks
    t = threading.Thread(target=_pick)
    t.start()
    t.join(timeout=60)
    return {"path": result["path"]}
