// Options: configure the Scrapescape server URL and the lazy-tabs default.
// For a non-localhost server we request a narrowly-scoped optional host
// permission for exactly that host (never <all_urls>).

const $ = (id) => document.getElementById(id);
const statusEl = $("status");

function setStatus(text, kind = "") {
  statusEl.textContent = text;
  statusEl.className = "status" + (kind ? " " + kind : "");
}

const DEFAULTS = { serverUrl: "http://127.0.0.1:8888", lazyTabs: false };

function parseUrl(raw) {
  try {
    const u = new URL(raw.trim());
    if (u.protocol !== "http:" && u.protocol !== "https:") return null;
    return u;
  } catch (_) {
    return null;
  }
}

// Match patterns don't take a port, so scope to protocol + host (all ports).
function hostPattern(u) {
  return `${u.protocol}//${u.hostname}/*`;
}
function isLocal(u) {
  return u.hostname === "127.0.0.1" || u.hostname === "localhost";
}

async function save() {
  const raw = $("serverUrl").value.trim() || DEFAULTS.serverUrl;
  const u = parseUrl(raw);
  if (!u) {
    setStatus("Not a valid http(s) URL", "err");
    return;
  }

  // Request permission for a non-local host before we can reach it.
  if (!isLocal(u)) {
    let granted = false;
    try {
      granted = await chrome.permissions.request({ origins: [hostPattern(u)] });
    } catch (e) {
      setStatus(`Permission error: ${e.message}`, "err");
      return;
    }
    if (!granted) {
      setStatus("Permission denied for that host — not saved", "err");
      return;
    }
  }

  await chrome.storage.local.set({
    serverUrl: `${u.protocol}//${u.host}`, // keep the port here (used for fetch URLs)
    lazyTabs: $("lazyTabs").checked,
  });
  setStatus("Saved. Testing connection…");

  const resp = await chrome.runtime.sendMessage({ type: "queueCount" });
  if (resp && resp.ok) setStatus(`Saved. Connected — queue: ${resp.count}`, "ok");
  else setStatus(`Saved, but couldn't reach Scrapescape: ${(resp && resp.error) || "no response"}`, "err");
}

async function init() {
  const cfg = await chrome.storage.local.get(DEFAULTS);
  $("serverUrl").value = cfg.serverUrl;
  $("lazyTabs").checked = cfg.lazyTabs;
  $("save").addEventListener("click", save);
}

init();
