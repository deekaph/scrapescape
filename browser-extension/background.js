// Scrapescape Companion — service worker (MV3, ES module).
//
// Two jobs:
//   1. Submit URLs to Scrapescape's /api/queue (toolbar click / right-click menu).
//   2. Lazy background tabs: when a background tab is created, discard it so it
//      does not keep its page loaded until the user actually views it.
//
// It sends URLs to Scrapescape ONLY when you explicitly click the icon or a
// menu item. Tab create/activate events are observed locally and never
// transmitted anywhere.

import { isSubmittableUrl, prepareSubmission } from "./lib.js";

const DEFAULTS = { serverUrl: "http://127.0.0.1:8888", lazyTabs: false };

async function getSettings() {
  return chrome.storage.local.get(DEFAULTS);
}

function apiBase(serverUrl) {
  return String(serverUrl || DEFAULTS.serverUrl).replace(/\/+$/, "");
}

// --- Scrapescape queue submission -------------------------------------------

async function submitUrls(urls, source = "browser-extension") {
  const { serverUrl } = await getSettings();
  const res = await fetch(`${apiBase(serverUrl)}/api/queue`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      // Static header the server requires. A web page can't set this on a
      // cross-origin request without a preflight the server won't answer, so
      // only this extension (privileged host access) can reach the endpoint.
      "X-Scrapescape-Extension": "1",
    },
    body: JSON.stringify({ urls, source }),
  });
  if (!res.ok) throw new Error(`Scrapescape returned HTTP ${res.status}`);
  return res.json();
}

async function queueCount() {
  const { serverUrl } = await getSettings();
  const res = await fetch(`${apiBase(serverUrl)}/api/downloads`);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const items = await res.json();
  return items.filter((d) => ["queued", "pending", "downloading"].includes(d.status)).length;
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  (async () => {
    try {
      if (msg && msg.type === "submit") {
        sendResponse({ ok: true, result: await submitUrls(msg.urls, msg.source) });
      } else if (msg && msg.type === "queueCount") {
        sendResponse({ ok: true, count: await queueCount() });
      } else {
        sendResponse({ ok: false, error: "unknown message" });
      }
    } catch (e) {
      sendResponse({ ok: false, error: String((e && e.message) || e) });
    }
  })();
  return true; // keep the message channel open for the async response
});

// --- Toolbar action + context menu ------------------------------------------
//
// MV3 gives one toolbar button per extension. Left-click submits the current
// tab; right-click opens a native menu with the bulk actions and the lazy
// toggle. Feedback is shown on the icon badge (no popup).

let badgeTimer = null;
async function flashBadge(text, color, title) {
  try {
    await chrome.action.setBadgeBackgroundColor({ color });
    await chrome.action.setBadgeText({ text });
    await chrome.action.setTitle({ title: `Scrapescape — ${title}` });
    if (badgeTimer) clearTimeout(badgeTimer);
    badgeTimer = setTimeout(async () => {
      try {
        await chrome.action.setBadgeText({ text: "" });
        await chrome.action.setTitle({ title: "Scrapescape — click to add current tab (right-click for more)" });
      } catch (_) {}
    }, 4000);
  } catch (_) {}
}

async function gatherAndSubmit(mode) {
  const tabs = await chrome.tabs.query({ currentWindow: true });
  const prep = prepareSubmission(tabs, mode);
  const clientSkipped = prep.skipped + prep.duplicates;

  if (prep.urls.length === 0) {
    flashBadge("×", "#e94560", clientSkipped ? `Nothing to add (${clientSkipped} skipped)` : "No web page to add");
    return;
  }
  try {
    const r = await submitUrls(prep.urls);
    const skipped = clientSkipped + (r.invalid || 0);
    const parts = [`Added: ${r.added}`];
    if (r.already_queued) parts.push(`Already queued: ${r.already_queued}`);
    if (skipped) parts.push(`Skipped: ${skipped}`);
    // Green when something new was added, amber when everything was a dupe.
    flashBadge(String(r.added), r.added ? "#4ecca3" : "#ffc107", parts.join(" · "));
  } catch (e) {
    flashBadge("!", "#e94560", `Scrapescape unavailable: ${(e && e.message) || e}`);
  }
}

chrome.action.onClicked.addListener(() => gatherAndSubmit("current"));

async function buildMenus() {
  const { lazyTabs } = await getSettings();
  await new Promise((resolve) => chrome.contextMenus.removeAll(resolve));
  const item = (props) => chrome.contextMenus.create({ contexts: ["action"], ...props });
  item({ id: "right", title: "Add tabs to the right" });
  item({ id: "all", title: "Add all tabs" });
  item({ id: "sep", type: "separator" });
  item({ id: "lazy", title: "Lazy background tabs", type: "checkbox", checked: !!lazyTabs });
  item({ id: "open", title: "Open Scrapescape" });
  item({ id: "options", title: "Options" });
}

chrome.contextMenus.onClicked.addListener(async (info) => {
  if (info.menuItemId === "right") gatherAndSubmit("right");
  else if (info.menuItemId === "all") gatherAndSubmit("all");
  else if (info.menuItemId === "lazy") await chrome.storage.local.set({ lazyTabs: info.checked });
  else if (info.menuItemId === "open") {
    const { serverUrl } = await getSettings();
    chrome.tabs.create({ url: serverUrl });
  } else if (info.menuItemId === "options") {
    chrome.runtime.openOptionsPage();
  }
});

// Keep the menu checkbox in sync when the toggle changes elsewhere (Options page).
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes.lazyTabs) {
    try {
      chrome.contextMenus.update("lazy", { checked: !!changes.lazyTabs.newValue });
    } catch (_) {}
  }
});

// --- Lazy background tabs ----------------------------------------------------
//
// Chromium MV3 has no API to stop a background tab's initial navigation from
// beginning — onCreated fires after navigation is already queued. The closest
// reliable behaviour is: tab created -> navigation begins -> discard it
// immediately, which cancels the in-flight load and unloads the tab. On
// activation Chromium reloads it from its URL.
//
// We track only tabs opened in the background that have NOT yet been viewed.
// The moment the user activates such a tab we forget it permanently, so
// switching away later never re-discards it (no loop).

const SESSION_KEY = "pendingLazyTabs";
let pending = new Set();     // tab ids: opened in background, never viewed
let hydrated = false;
let startupAt = 0;           // set on browser start, to avoid fighting session restore
const RESTORE_GRACE_MS = 2500;

async function hydrate() {
  if (hydrated) return;
  try {
    const s = await chrome.storage.session.get(SESSION_KEY);
    if (Array.isArray(s[SESSION_KEY])) pending = new Set(s[SESSION_KEY]);
  } catch (_) {}
  hydrated = true;
}

async function persist() {
  try {
    await chrome.storage.session.set({ [SESSION_KEY]: [...pending] });
  } catch (_) {}
}

// Discard a still-pending background tab ONCE, but only after its navigation has
// committed a real URL. Discarding before the URL commits (when only pendingUrl
// exists) leaves the tab with nothing to reload — it hangs at "Loading" forever,
// even on activation. On success we drop it from `pending` so it is never
// touched again (no re-discard loop); Chromium reloads it when the user selects it.
async function maybeDiscard(tab) {
  if (!tab || !pending.has(tab.id)) return;
  if (tab.active || tab.pinned) {
    // Viewed or pinned before we could discard — leave it alone for good.
    pending.delete(tab.id);
    await persist();
    return;
  }
  if (tab.discarded) {
    // Already unloaded (e.g. Brave did it) — nothing left to do.
    pending.delete(tab.id);
    await persist();
    return;
  }
  // Need a committed http(s) URL, not just pendingUrl.
  if (!isSubmittableUrl(tab.url)) {
    // If it committed to a non-web URL, stop tracking it.
    if (tab.url) {
      pending.delete(tab.id);
      await persist();
    }
    return; // otherwise wait for the URL to commit (a later onUpdated)
  }
  try {
    await chrome.tabs.discard(tab.id);
    pending.delete(tab.id); // done — success means it's unloaded with its URL retained
    await persist();
  } catch (_) {
    // Momentarily not discardable; a later onUpdated will retry while it stays pending.
  }
}

chrome.tabs.onCreated.addListener(async (tab) => {
  await hydrate();
  const { lazyTabs } = await getSettings();
  if (!lazyTabs) return;
  // Don't touch tabs appearing during session restore — Brave restores them lazily.
  if (startupAt && Date.now() - startupAt < RESTORE_GRACE_MS) return;
  if (tab.active || tab.pinned || tab.discarded) return;
  const url = tab.pendingUrl || tab.url || "";
  if (!isSubmittableUrl(url)) return; // leave internal / newtab / empty pages alone
  pending.add(tab.id);
  await persist();
  // Only discard now if the URL is ALREADY committed; otherwise wait for onUpdated.
  if (isSubmittableUrl(tab.url)) maybeDiscard(tab);
});

chrome.tabs.onUpdated.addListener(async (tabId, _changeInfo, tab) => {
  await hydrate();
  if (!pending.has(tabId)) return;
  const { lazyTabs } = await getSettings();
  if (!lazyTabs) {
    pending.delete(tabId);
    await persist();
    return;
  }
  // Discard as soon as the real URL is committed (earliest reliable point).
  maybeDiscard(tab);
});

chrome.tabs.onActivated.addListener(async ({ tabId }) => {
  await hydrate();
  if (pending.delete(tabId)) await persist(); // viewed -> stop managing forever
});

chrome.tabs.onRemoved.addListener(async (tabId) => {
  await hydrate();
  if (pending.delete(tabId)) await persist();
});

chrome.tabs.onReplaced.addListener(async (addedTabId, removedTabId) => {
  await hydrate();
  if (pending.delete(removedTabId)) {
    pending.add(addedTabId);
    await persist();
  }
});

chrome.runtime.onStartup.addListener(async () => {
  startupAt = Date.now();
  pending = new Set();
  hydrated = true;
  await persist();
  await buildMenus();
});

chrome.runtime.onInstalled.addListener(async () => {
  const cur = await chrome.storage.local.get(DEFAULTS);
  await chrome.storage.local.set({ ...DEFAULTS, ...cur }); // ensure keys exist, keep user values
  await buildMenus();
});
