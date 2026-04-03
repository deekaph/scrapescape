// --- State ---
let downloads = [];
let playlists = [];
let ws = null;
let reconnectTimer = null;

// --- DOM refs ---
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

const urlInput = $("#urlInput");
const addBtn = $("#addBtn");
const importBtn = $("#importBtn");
const concurrencySlider = $("#concurrencySlider");
const concurrencyValue = $("#concurrencyValue");
const perSiteSlider = $("#perSiteSlider");
const perSiteValue = $("#perSiteValue");
const moveToDir = $("#moveToDir");
const moveToDirBtn = $("#moveToDirBtn");
const browseDirBtn = $("#browseDirBtn");
const startBtn = $("#startBtn");
const pauseBtn = $("#pauseBtn");
const connStatus = $("#connStatus");

const clearCompletedBtn = $("#clearCompletedBtn");

const activeList = $("#activeList");
const queueList = $("#queueList");
const completedList = $("#completedList");
const failedList = $("#failedList");
const playlistList = $("#playlistList");

const activeCount = $("#activeCount");
const queueCount = $("#queueCount");
const completedCount = $("#completedCount");
const failedCount = $("#failedCount");
const playlistCount = $("#playlistCount");

const importModal = $("#importModal");
const importPreview = $("#importPreview");
const importConfirmBtn = $("#importConfirmBtn");
const importCancelBtn = $("#importCancelBtn");

// --- Init ---
document.addEventListener("DOMContentLoaded", () => {
    loadSettings();
    loadDownloads();
    loadPlaylists();
    loadLogs();
    updateDiskUsage();
    connectWebSocket();
    bindEvents();
});

// --- API helpers ---
async function api(path, opts = {}) {
    try {
        const res = await fetch(path, {
            headers: { "Content-Type": "application/json" },
            ...opts,
        });
        if (!res.ok) {
            const text = await res.text();
            console.error(`API ${path} failed (${res.status}):`, text);
            return { error: `Server error ${res.status}` };
        }
        return await res.json();
    } catch (e) {
        console.error(`API ${path} exception:`, e);
        return { error: e.message };
    }
}

// --- Load data ---
async function loadDownloads() {
    downloads = await api("/api/downloads");
    renderDownloads();
}

async function loadPlaylists() {
    playlists = await api("/api/playlists");
    renderPlaylists();
}

async function loadSettings() {
    const settings = await api("/api/settings");
    concurrencySlider.value = settings.max_concurrent;
    concurrencyValue.textContent = settings.max_concurrent;
    perSiteSlider.value = settings.max_per_site || 2;
    perSiteValue.textContent = settings.max_per_site || 2;
    moveToDir.value = settings.move_to_dir || "";
}

// --- WebSocket ---
function connectWebSocket() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(`${proto}//${location.host}/ws`);

    ws.onopen = () => {
        connStatus.classList.add("connected");
        connStatus.title = "Connected";
        if (reconnectTimer) clearInterval(reconnectTimer);
        reconnectTimer = null;
    };

    ws.onclose = () => {
        connStatus.classList.remove("connected");
        connStatus.title = "Disconnected";
        if (!reconnectTimer) {
            reconnectTimer = setInterval(connectWebSocket, 3000);
        }
    };

    ws.onmessage = (e) => {
        const msg = JSON.parse(e.data);
        handleWsMessage(msg);
    };
}

function handleWsMessage(msg) {
    switch (msg.type) {
        case "progress":
            updateProgress(msg);
            break;
        case "status_change":
            updateStatusChange(msg);
            break;
        case "queue_update":
            loadDownloads();
            break;
        case "playlist_update":
            loadPlaylists();
            break;
        case "log":
            appendLog(msg.message);
            break;
        case "disk_full":
            toast("Disk 90%+ full — downloads auto-paused!", "error");
            updateDiskUsage();
            break;
    }
}

function updateProgress(msg) {
    const dl = downloads.find((d) => d.id === msg.id);
    if (dl) {
        dl.progress = msg.progress;
        dl.speed = msg.speed;
        dl.eta = msg.eta;
        dl.filesize = msg.filesize;
    }

    const card = document.getElementById(`dl-${msg.id}`);
    if (!card) return;

    const fill = card.querySelector(".progress-fill");
    const pctText = card.querySelector(".dl-progress-text");
    const speedEl = card.querySelector(".dl-speed");
    const sizeEl = card.querySelector(".dl-filesize");
    const etaEl = card.querySelector(".dl-eta");

    if (fill) fill.style.width = `${msg.progress}%`;
    if (pctText) pctText.textContent = `${msg.progress.toFixed(1)}%`;
    if (speedEl) speedEl.textContent = msg.speed || "";
    if (sizeEl) sizeEl.textContent = msg.filesize || "";

    // Calculate ETA from start time and progress
    if (etaEl) {
        if (msg.eta) {
            etaEl.textContent = `ETA: ${msg.eta}`;
        } else if (dl && dl._started_at && msg.progress > 0) {
            const elapsed = (Date.now() - dl._started_at) / 1000;
            const totalEstimate = elapsed / (msg.progress / 100);
            const remaining = Math.max(0, totalEstimate - elapsed);
            etaEl.textContent = `ETA: ${formatEta(remaining)}`;
        } else {
            etaEl.textContent = "";
        }
    }
}

function formatEta(seconds) {
    if (seconds <= 0) return "done";
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = Math.floor(seconds % 60);
    if (h > 0) return `${h}h ${m}m`;
    if (m > 0) return `${m}m ${s}s`;
    return `${s}s`;
}

function updateStatusChange(msg) {
    const dl = downloads.find((d) => d.id === msg.id);
    if (dl) {
        dl.status = msg.status;
        if (msg.title) dl.title = msg.title;
        if (msg.filename) dl.filename = msg.filename;
        if (msg.error) dl.error_message = msg.error;
        if (msg.started_at) dl._started_at = new Date(msg.started_at).getTime();
        // If transitioning to downloading and no start time yet, set it now
        if (msg.status === "downloading" && !dl._started_at) {
            dl._started_at = Date.now();
        }
    }
    renderDownloads();
}

// --- Render downloads ---
function renderDownloads() {
    const active = downloads.filter((d) => d.status === "downloading");
    const paused = downloads.filter((d) => d.status === "paused");
    const queued = downloads.filter((d) => d.status === "queued");
    const pending = downloads.filter((d) => d.status === "pending");
    const completed = downloads.filter((d) => d.status === "completed");
    const failed = downloads.filter((d) => d.status === "failed");

    activeCount.textContent = active.length + paused.length;
    queueCount.textContent = queued.length + pending.length;
    completedCount.textContent = completed.length;
    failedCount.textContent = failed.length;

    let activeHtml = active.map(renderActiveItem).join("");
    if (paused.length) {
        activeHtml += `<div class="paused-controls">
            <span class="paused-banner">PAUSED (${paused.length} downloads)</span>
            <button class="btn btn-success btn-small" onclick="resumeAll()">Resume All</button>
            <button class="btn btn-danger btn-small" onclick="cancelAllRequeue()">Delete Partials &amp; Requeue All</button>
        </div>`;
        activeHtml += paused.map(renderPausedItem).join("");
    }
    activeList.innerHTML = activeHtml
        || '<div class="empty-state">No active downloads</div>';

    let queueHtml = "";
    if (queued.length || pending.length) {
        // Released (queued) items first
        queueHtml += queued.map(renderQueueItem).join("");
        // Then pending items with release controls
        if (pending.length) {
            queueHtml += `<div class="release-controls">
                <span class="pending-banner">${pending.length} pending — awaiting release</span>
                <select id="releaseSelect" class="select-input">
                    ${[50,100,150,200,250,300,350,400,450,500].filter(n => n <= pending.length + 49).map(n => `<option value="${n}">Release ${n}</option>`).join("")}
                </select>
                <button class="btn btn-primary btn-small" onclick="releaseNext()">Release</button>
                <button class="btn btn-success btn-small" onclick="releaseAllPending()">Release All</button>
            </div>`;
            queueHtml += pending.map(renderPendingItem).join("");
        }
    } else {
        queueHtml = '<div class="empty-state">Queue is empty</div>';
    }
    queueList.innerHTML = queueHtml;

    const sortedCompleted = completed.sort((a, b) => {
        const ta = a.completed_at || a.added_at || "";
        const tb = b.completed_at || b.added_at || "";
        return tb.localeCompare(ta);
    });
    completedList.innerHTML = sortedCompleted.length
        ? sortedCompleted.map(renderCompletedItem).join("")
        : '<div class="empty-state">No completed downloads yet</div>';

    const sortedFailed = failed.sort((a, b) => {
        const ta = a.completed_at || a.added_at || "";
        const tb = b.completed_at || b.added_at || "";
        return tb.localeCompare(ta);
    });
    failedList.innerHTML = sortedFailed.length
        ? sortedFailed.map(renderFailedItem).join("")
        : '<div class="empty-state">No failed downloads</div>';
}

function truncateUrl(url, max = 80) {
    return url.length > max ? url.slice(0, max) + "..." : url;
}

function renderActiveItem(dl) {
    const hasProgress = dl.progress > 0;
    const title = dl.title || (hasProgress ? "Downloading..." : "Extracting info...");
    const site = extractSite(dl.url);
    const startedStr = dl._started_at ? `Started: ${new Date(dl._started_at).toLocaleTimeString()}` : "";
    return `
        <div class="download-item" id="dl-${dl.id}">
            <div class="dl-info">
                <div class="dl-title">${site ? `<span class="dl-site">${escHtml(site)}</span> ` : ""}<span class="dl-title-text" onclick="editTitle(${dl.id}, this)" title="Click to rename">${escHtml(title)}</span></div>
                <div class="dl-url">${escHtml(truncateUrl(dl.url))}</div>
                <div class="dl-meta">
                    <span class="dl-speed">${dl.speed || ""}</span>
                    <span class="dl-eta">${dl.eta ? "ETA: " + dl.eta : ""}</span>
                    <span class="dl-filesize">${dl.filesize || ""}</span>
                    ${startedStr ? `<span class="dl-started">${startedStr}</span>` : ""}
                </div>
            </div>
            <div class="progress-bar">
                <div class="progress-fill" style="width: ${dl.progress || 0}%"></div>
            </div>
            <div class="dl-progress-text">${(dl.progress || 0).toFixed(1)}%</div>
            <div class="dl-actions">
                <button class="btn btn-warning btn-small" onclick="pauseOne(${dl.id})">Pause</button>
            </div>
        </div>`;
}

function renderPausedItem(dl) {
    const site = extractSite(dl.url);
    return `
        <div class="download-item paused" id="dl-${dl.id}">
            <div class="dl-info">
                <div class="dl-title">${site ? `<span class="dl-site">${escHtml(site)}</span> ` : ""}${escHtml(dl.title || "Paused")}</div>
                <div class="dl-url">${escHtml(truncateUrl(dl.url))}</div>
                <div class="dl-meta">
                    <span class="dl-paused-label">PAUSED</span>
                    <span class="dl-filesize">${dl.filesize || ""}</span>
                </div>
            </div>
            <div class="progress-bar">
                <div class="progress-fill paused-fill" style="width: ${dl.progress || 0}%"></div>
            </div>
            <div class="dl-progress-text">${(dl.progress || 0).toFixed(1)}%</div>
            <div class="dl-actions">
                <button class="btn btn-success btn-small" onclick="resumeOne(${dl.id})">Resume</button>
                <button class="btn btn-danger btn-small" onclick="cancelOneRequeue(${dl.id})">Delete &amp; Requeue</button>
            </div>
        </div>`;
}

function renderQueueItem(dl) {
    const site = extractSite(dl.url);
    return `
        <div class="download-item released-item" id="dl-${dl.id}">
            <div class="dl-info">
                <div class="dl-url">${site ? `<span class="dl-site">${escHtml(site)}</span> ` : ""}${escHtml(truncateUrl(dl.url))}</div>
            </div>
            <div class="dl-actions">
                <button class="btn btn-success btn-small" onclick="startNow(${dl.id})">Start Now</button>
                <button class="btn btn-danger btn-small" onclick="removeFromQueue(${dl.id})">Remove</button>
            </div>
        </div>`;
}

function renderPendingItem(dl) {
    const site = extractSite(dl.url);
    return `
        <div class="download-item pending-item" id="dl-${dl.id}">
            <div class="dl-info">
                <div class="dl-url">${site ? `<span class="dl-site">${escHtml(site)}</span> ` : ""}${escHtml(truncateUrl(dl.url))}</div>
            </div>
            <div class="dl-actions">
                <button class="btn btn-danger btn-small" onclick="removeFromQueue(${dl.id})">Remove</button>
            </div>
        </div>`;
}

function extractSite(url) {
    try {
        const host = new URL(url).hostname.replace("www.", "");
        return host.split(".")[0];
    } catch { return ""; }
}

function extractDir(filepath) {
    if (!filepath) return "";
    // Get directory portion, handle both / and \
    const sep = filepath.includes("\\") ? "\\" : "/";
    const parts = filepath.split(sep);
    parts.pop(); // remove filename
    return parts.join(sep);
}

function renderCompletedItem(dl) {
    const ts = dl.completed_at ? formatTimestamp(dl.completed_at) : "";
    const site = extractSite(dl.url);
    const dir = extractDir(dl.filename);
    return `
        <div class="download-item" id="dl-${dl.id}">
            <div class="dl-info">
                <div class="dl-title">${site ? `<span class="dl-site">${escHtml(site)}</span> ` : ""}${escHtml(dl.title || "Unknown")}</div>
                ${dir ? `<div class="dl-filepath">${escHtml(dir)}</div>` : ""}
                ${ts ? `<div class="dl-timestamp">Completed: ${ts}</div>` : ""}
            </div>
        </div>`;
}

function renderFailedItem(dl) {
    const ts = dl.completed_at ? formatTimestamp(dl.completed_at) : "";
    const encodedUrl = encodeURIComponent(dl.url);
    return `
        <div class="download-item" id="dl-${dl.id}">
            <div class="dl-info">
                <div class="dl-url">${escHtml(truncateUrl(dl.url))}</div>
                <div class="dl-error">${escHtml(dl.error_message || "Unknown error")}</div>
                ${ts ? `<div class="dl-timestamp">${ts}</div>` : ""}
            </div>
            <div class="dl-actions">
                <button class="btn btn-open btn-small" onclick="window.open(decodeURIComponent('${encodedUrl}'), '_blank')" title="Open in browser">Open</button>
                <button class="btn btn-primary btn-small" onclick="retryDownload(${dl.id})">Retry</button>
                <button class="btn btn-danger btn-small" onclick="removeFromQueue(${dl.id})">Delete</button>
            </div>
        </div>`;
}

function formatTimestamp(ts) {
    try {
        const d = new Date(ts);
        if (isNaN(d)) return ts;
        return d.toLocaleString();
    } catch {
        return ts;
    }
}

// --- Render playlists ---
function renderPlaylists() {
    playlistCount.textContent = playlists.length;

    if (!playlists.length) {
        playlistList.innerHTML = '<div class="empty-state">No playlists detected yet</div>';
        return;
    }

    playlistList.innerHTML = playlists.map((pl) => {
        const entries = pl.entries || [];
        const encodedUrl = encodeURIComponent(pl.url);
        const ownFolder = pl.own_folder ? "checked" : "";
        return `
            <div class="playlist-item" id="pl-${pl.id}">
                <div class="playlist-top-row">
                    <div class="playlist-info">
                        <div class="playlist-title" title="${escHtml(pl.url)}">${escHtml(pl.title || pl.url)}</div>
                        <div class="playlist-url">${escHtml(pl.url)}</div>
                        <div class="playlist-meta">${entries.length} videos found</div>
                    </div>
                    <div class="playlist-actions">
                        <button class="btn btn-open btn-small" onclick="window.open(decodeURIComponent('${encodedUrl}'), '_blank')" title="Open playlist in browser">Open</button>
                        <button class="btn btn-copy btn-small" onclick="copyToClipboard(decodeURIComponent('${encodedUrl}'))" title="Copy playlist URL">Copy URL</button>
                        <button class="btn btn-queue btn-small" onclick="downloadAllPlaylist(${pl.id})" title="Queue all videos for download">Download All</button>
                        <button class="btn btn-danger btn-small" onclick="deletePlaylist(${pl.id})" title="Remove playlist">X</button>
                    </div>
                </div>
                <div class="playlist-options-row">
                    <label class="playlist-checkbox-label">
                        <input type="checkbox" ${ownFolder} onchange="toggleOwnFolder(${pl.id}, this.checked)">
                        <span>Save to own folder</span>
                    </label>
                    <div class="playlist-duration-filter">
                        <span>Min duration:</span>
                        <input type="text" class="duration-input" id="pl-min-dur-${pl.id}" placeholder="0:00" value="">
                        <button class="btn btn-queue btn-small" onclick="downloadFilteredPlaylist(${pl.id})" title="Download videos longer than min duration">Download Filtered</button>
                    </div>
                </div>
                <div class="playlist-expand">
                    <button class="btn btn-secondary btn-small playlist-toggle-btn" onclick="togglePlaylistEntries(${pl.id})">
                        <span id="pl-toggle-text-${pl.id}">Show Videos</span>
                    </button>
                </div>
                <div class="playlist-entries" id="pl-entries-${pl.id}">
                    ${entries.map((entry) => renderPlaylistEntry(pl.id, entry)).join("")}
                </div>
            </div>`;
    }).join("");
}

function renderPlaylistEntry(playlistId, entry) {
    const safeUrl = escHtml(entry.url);
    const safeTitle = escHtml(entry.title || entry.url);
    const dur = formatDuration(entry.duration || 0);
    const encodedEntryUrl = encodeURIComponent(entry.url);
    return `
        <div class="playlist-entry">
            <div class="playlist-entry-info">
                <div class="playlist-entry-title" title="${safeTitle}">${safeTitle}</div>
                <div class="playlist-entry-meta">
                    <span class="playlist-entry-duration">${dur}</span>
                    <span class="playlist-entry-url" title="${safeUrl}">${safeUrl}</span>
                </div>
            </div>
            <div class="playlist-entry-actions">
                <button class="btn btn-open btn-small" onclick="window.open(decodeURIComponent('${encodedEntryUrl}'), '_blank')" title="Open in browser">Open</button>
                <button class="btn btn-copy btn-small" onclick="copyToClipboard(decodeURIComponent('${encodedEntryUrl}'))" title="Copy URL">Copy</button>
                <button class="btn btn-queue btn-small" onclick="queueSingleEntry('${encodedEntryUrl}')" title="Add to download queue">Queue</button>
            </div>
        </div>`;
}

function formatDuration(seconds) {
    if (!seconds || seconds <= 0) return "--:--";
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = Math.floor(seconds % 60);
    if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
    return `${m}:${String(s).padStart(2, "0")}`;
}

function parseDuration(str) {
    // Parse "M:SS" or "H:MM:SS" into seconds
    if (!str || !str.trim()) return 0;
    const parts = str.trim().split(":").map(Number);
    if (parts.some(isNaN)) return 0;
    if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2];
    if (parts.length === 2) return parts[0] * 60 + parts[1];
    if (parts.length === 1) return parts[0];
    return 0;
}

function escHtml(str) {
    if (!str) return "";
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
}

// --- Playlist actions ---
function togglePlaylistEntries(id) {
    const entries = document.getElementById(`pl-entries-${id}`);
    const text = document.getElementById(`pl-toggle-text-${id}`);
    if (entries) {
        const isOpen = entries.classList.toggle("open");
        if (text) text.textContent = isOpen ? "Hide Videos" : "Show Videos";
    }
}

async function deletePlaylist(id) {
    await api(`/api/playlists/${id}`, { method: "DELETE" });
}

async function toggleOwnFolder(id, checked) {
    await api(`/api/playlists/${id}/own-folder`, {
        method: "POST",
        body: JSON.stringify({ enabled: checked }),
    });
}

async function downloadAllPlaylist(id) {
    const result = await api(`/api/playlists/${id}/queue-all`, { method: "POST" });
    if (result.error) {
        toast(result.error, "error");
    } else {
        toast(`Queued ${result.added} of ${result.total} videos (${result.skipped} duplicates skipped)`, "success");
    }
}

async function downloadFilteredPlaylist(id) {
    const input = document.getElementById(`pl-min-dur-${id}`);
    const minSec = parseDuration(input ? input.value : "");
    if (minSec <= 0) {
        toast("Enter a minimum duration like 5:00 or 1:30", "error");
        return;
    }
    const result = await api(`/api/playlists/${id}/queue-all`, {
        method: "POST",
        body: JSON.stringify({ min_duration: minSec }),
    });
    if (result.error) {
        toast(result.error, "error");
    } else {
        toast(`Queued ${result.added} of ${result.filtered} videos over ${input.value} (${result.skipped} dupes, ${result.total} total)`, "success");
    }
}

async function queueSingleEntry(encodedUrl) {
    const url = decodeURIComponent(encodedUrl);
    const result = await api("/api/add", {
        method: "POST",
        body: JSON.stringify({ url }),
    });
    if (result.added) {
        toast("Added to download queue", "success");
    } else if (result.error) {
        toast(result.error, "error");
    } else {
        toast("Already in queue/history", "error");
    }
}

function copyToClipboard(text) {
    navigator.clipboard.writeText(text).then(() => {
        toast("URL copied to clipboard", "success");
    }).catch(() => {
        // Fallback
        const ta = document.createElement("textarea");
        ta.value = text;
        document.body.appendChild(ta);
        ta.select();
        document.execCommand("copy");
        ta.remove();
        toast("URL copied to clipboard", "success");
    });
}

// --- Download actions ---
async function addUrl() {
    const url = urlInput.value.trim();
    if (!url) return;
    const result = await api("/api/add", {
        method: "POST",
        body: JSON.stringify({ url }),
    });
    if (result.added) {
        toast("URL added to queue", "success");
    } else if (result.error) {
        toast(result.error, "error");
    } else {
        toast("URL already in queue/history", "error");
    }
    urlInput.value = "";
    urlInput.focus();
}

function editTitle(id, el) {
    const dl = downloads.find((d) => d.id === id);
    const current = (dl && dl.title && dl.title !== "Downloading..." && dl.title !== "Extracting info...") ? dl.title : el.textContent;

    // Hide the span, insert an input next to it
    el.style.display = "none";
    const input = document.createElement("input");
    input.type = "text";
    input.value = current;
    input.className = "dl-title-edit";
    el.parentNode.appendChild(input);
    input.focus();
    input.select();

    function finish(save) {
        if (save) {
            const newTitle = input.value.trim();
            if (newTitle) {
                api(`/api/rename/${id}`, {
                    method: "POST",
                    body: JSON.stringify({ title: newTitle }),
                });
                if (dl) dl.title = newTitle;
                el.textContent = newTitle;
            }
        }
        input.remove();
        el.style.display = "";
    }

    input.addEventListener("blur", () => finish(true));
    input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") { e.preventDefault(); input.blur(); }
        if (e.key === "Escape") { finish(false); }
    });
}

async function startNow(id) {
    await api(`/api/start-now/${id}`, { method: "POST" });
}

async function removeFromQueue(id) {
    await api(`/api/queue/${id}`, { method: "DELETE" });
}

async function retryDownload(id) {
    await api(`/api/retry/${id}`, { method: "POST" });
    toast("Download re-queued", "success");
}

// --- Release queue ---
async function releaseNext() {
    const sel = document.getElementById("releaseSelect");
    const count = sel ? parseInt(sel.value) : 50;
    const result = await api("/api/release", {
        method: "POST",
        body: JSON.stringify({ count }),
    });
    if (result.released > 0) {
        toast(`Released ${result.released} downloads to queue`, "success");
    } else {
        toast("Nothing to release", "success");
    }
}

async function holdQueue() {
    const result = await api("/api/hold-queue", { method: "POST" });
    if (result.held > 0) {
        toast(`Held ${result.held} items — use Release to resume`, "success");
    } else {
        toast("Nothing in queue to hold", "success");
    }
}

async function releaseAllPending() {
    const result = await api("/api/release", {
        method: "POST",
        body: JSON.stringify({ count: 0 }),
    });
    if (result.released > 0) {
        toast(`Released all ${result.released} downloads to queue`, "success");
    } else {
        toast("Nothing to release", "success");
    }
}

// --- Clear completed ---
async function clearCompleted() {
    const result = await api("/api/clear-completed", { method: "POST" });
    if (result.cleared > 0) {
        toast(`Cleared ${result.cleared} completed downloads`, "success");
    } else {
        toast("Nothing to clear", "success");
    }
}

// --- Bookmarks import ---
let bookmarkDomains = {};

async function openImportModal() {
    const data = await api("/api/import-bookmarks", { method: "POST" });
    if (data.error) {
        toast(data.error, "error");
        return;
    }

    bookmarkDomains = data.domains;
    let html = `<p>Found <strong>${data.total}</strong> bookmarks across ${Object.keys(data.domains).length} domains:</p>`;
    html += `<div class="domain-item select-all-row">
        <input type="checkbox" id="domain-select-all" onchange="toggleAllDomains(this.checked)">
        <label for="domain-select-all"><strong>Select All</strong></label>
    </div>`;
    html += '<div class="domain-list">';

    const sorted = Object.entries(data.domains).sort((a, b) => b[1] - a[1]);
    for (const [domain, count] of sorted) {
        html += `
            <div class="domain-item">
                <input type="checkbox" id="domain-${domain}" value="${domain}" class="domain-checkbox">
                <label for="domain-${domain}">${domain}</label>
                <span class="domain-count">${count} URLs</span>
            </div>`;
    }
    html += "</div>";

    importPreview.innerHTML = html;
    importModal.classList.remove("hidden");
}

async function confirmImport() {
    const checked = [...importPreview.querySelectorAll('.domain-checkbox:checked')].map(
        (cb) => cb.value
    );

    if (checked.length === 0) {
        toast("Select at least one domain", "error");
        return;
    }

    const filterResult = await api("/api/import-bookmarks/filter", {
        method: "POST",
        body: JSON.stringify({ urls: checked }),
    });

    if (filterResult.error) {
        toast("Filter failed: " + filterResult.error, "error");
        importModal.classList.add("hidden");
        return;
    }

    if (!filterResult.urls || filterResult.urls.length === 0) {
        toast("No URLs found for selected domains", "error");
        importModal.classList.add("hidden");
        return;
    }

    const result = await api("/api/import-bookmarks/confirm", {
        method: "POST",
        body: JSON.stringify({ urls: filterResult.urls }),
    });

    if (result.error) {
        toast("Import failed: " + result.error, "error");
    } else {
        toast(`Imported ${result.added} URLs (${result.skipped} duplicates skipped)`, "success");
    }
    importModal.classList.add("hidden");
}

function toggleAllDomains(checked) {
    importPreview.querySelectorAll(".domain-checkbox").forEach((cb) => {
        cb.checked = checked;
    });
}

// --- Concurrency ---
async function setConcurrency(val) {
    concurrencyValue.textContent = val;
    await api("/api/concurrency", {
        method: "POST",
        body: JSON.stringify({ max: parseInt(val) }),
    });
}

// --- Per-site concurrency ---
async function setPerSite(val) {
    perSiteValue.textContent = val;
    await api("/api/per-site", {
        method: "POST",
        body: JSON.stringify({ max: parseInt(val) }),
    });
}

// --- Move-to directory ---
async function setMoveToDir() {
    const dir = moveToDir.value.trim();
    const result = await api("/api/settings/move-to", {
        method: "POST",
        body: JSON.stringify({ directory: dir }),
    });
    if (result.error) {
        toast(result.error, "error");
    } else {
        toast(dir ? `Completed downloads will move to: ${dir}` : "Downloads will stay in ./downloads/", "success");
    }
}

async function browseForDir() {
    toast("Opening folder picker...", "success");
    const result = await api("/api/browse-folder", { method: "POST" });
    if (result.path) {
        moveToDir.value = result.path;
        setMoveToDir();
    }
}

// --- Start/Pause/Resume/Cancel ---
async function startDownloads() {
    await api("/api/start", { method: "POST" });
    toast("Download manager started", "success");
}

async function pauseDownloads() {
    await api("/api/pause", { method: "POST" });
    toast("All downloads paused", "success");
}

async function pauseOne(id) {
    await api(`/api/pause/${id}`, { method: "POST" });
}

async function resumeAll() {
    await api("/api/resume-all", { method: "POST" });
    toast("Resuming all downloads", "success");
}

async function resumeOne(id) {
    await api(`/api/resume/${id}`, { method: "POST" });
}

async function cancelAllRequeue() {
    await api("/api/cancel-all", {
        method: "POST",
        body: JSON.stringify({ delete_partial: true }),
    });
    toast("Partials deleted, all re-queued", "success");
}

async function cancelOneRequeue(id) {
    await api(`/api/cancel/${id}`, {
        method: "POST",
        body: JSON.stringify({ delete_partial: true }),
    });
}

// --- Toast ---
function toast(message, type = "success") {
    const el = document.createElement("div");
    el.className = `toast ${type}`;
    el.textContent = message;
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 3000);
}

// --- Disk usage ---
const diskFill = $("#diskFill");
const diskText = $("#diskText");

async function updateDiskUsage() {
    const data = await api("/api/disk-usage");
    if (data.error) return;

    const pct = data.percent;
    diskFill.style.width = `${pct}%`;
    diskFill.className = "disk-fill" + (pct >= 90 ? " danger" : pct >= 75 ? " warning" : "");
    diskText.textContent = `${data.used_gb}GB / ${data.total_gb}GB (${pct}%) — ${data.free_gb}GB free`;
}

// Poll disk usage every 30 seconds
setInterval(updateDiskUsage, 30000);

// --- Server logs ---
const logOutput = $("#logOutput");
const logToggle = $("#logToggle");
const logBody = $("#logBody");
const MAX_LOG_LINES = 500;

async function loadLogs() {
    const data = await api("/api/logs");
    if (data.logs) {
        for (const line of data.logs) {
            appendLog(line, false);
        }
        scrollLogToBottom();
    }
}

function appendLog(message, autoScroll = true) {
    if (!logOutput) return;
    const line = document.createElement("div");
    line.className = "log-line";
    if (/error/i.test(message)) line.classList.add("log-line-error");
    else if (/warn/i.test(message)) line.classList.add("log-line-warn");
    line.textContent = message;
    logOutput.appendChild(line);

    // Trim old lines
    while (logOutput.children.length > MAX_LOG_LINES) {
        logOutput.removeChild(logOutput.firstChild);
    }

    if (autoScroll) scrollLogToBottom();
}

function scrollLogToBottom() {
    if (logOutput) logOutput.scrollTop = logOutput.scrollHeight;
}

function toggleLogPanel() {
    if (logBody) {
        logBody.classList.toggle("hidden");
        logToggle.classList.toggle("open");
        if (!logBody.classList.contains("hidden")) {
            scrollLogToBottom();
        }
    }
}

// --- Tabs ---
function setupTabs() {
    for (const tab of $$(".tab")) {
        tab.addEventListener("click", () => {
            $$(".tab").forEach((t) => t.classList.remove("active"));
            $$(".tab-content").forEach((t) => t.classList.remove("active"));
            tab.classList.add("active");
            $(`#${tab.dataset.tab}Tab`).classList.add("active");
        });
    }
}

// --- Event binding ---
function bindEvents() {
    addBtn.addEventListener("click", addUrl);
    urlInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") addUrl();
    });
    importBtn.addEventListener("click", openImportModal);
    importConfirmBtn.addEventListener("click", confirmImport);
    importCancelBtn.addEventListener("click", () => importModal.classList.add("hidden"));
    concurrencySlider.addEventListener("input", (e) => setConcurrency(e.target.value));
    perSiteSlider.addEventListener("input", (e) => setPerSite(e.target.value));
    moveToDirBtn.addEventListener("click", setMoveToDir);
    browseDirBtn.addEventListener("click", browseForDir);
    startBtn.addEventListener("click", startDownloads);
    pauseBtn.addEventListener("click", pauseDownloads);
    clearCompletedBtn.addEventListener("click", clearCompleted);
    $("#holdQueueBtn").addEventListener("click", holdQueue);
    setupTabs();

    importModal.addEventListener("click", (e) => {
        if (e.target === importModal) importModal.classList.add("hidden");
    });
}
