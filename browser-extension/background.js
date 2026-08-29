// Scrapescape Companion — service worker (MV3, ES module).
//
// Two jobs:
//   1. Submit URLs to Scrapescape's /api/queue on request from the popup.
//   2. Lazy background tabs: when a background tab is created, discard it so it
//      does not keep its page loaded until the user actually views it.
//
// It sends URLs to Scrapescape ONLY in response to an explicit "submit" message
// from the popup. Tab create/activate events are observed locally and never
// transmitted anywhere.

import { isSubmittableUrl } from "./lib.js";

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

async function tryDiscard(tabId) {
  if (!pending.has(tabId)) return;
  let tab;
  try {
    tab = await chrome.tabs.get(tabId);
  } catch (_) {
    pending.delete(tabId);
    await persist();
    return;
  }
  // Never discard the active or a pinned tab; stop tracking those.
  if (!tab || tab.active || tab.pinned) {
    pending.delete(tabId);
    await persist();
    return;
  }
  if (tab.discarded) return; // already unloaded — keep tracking until viewed
  try {
    const discarded = await chrome.tabs.discard(tabId);
    // discard() can assign a new tab id — move our tracking across.
    if (discarded && discarded.id !== tabId) {
      pending.delete(tabId);
      if (!discarded.active) pending.add(discarded.id);
      await persist();
    }
  } catch (_) {
    // Not discardable yet (navigation not far enough along). onUpdated retries.
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
  tryDiscard(tab.id);
});

chrome.tabs.onUpdated.addListener(async (tabId, changeInfo, tab) => {
  await hydrate();
  if (!pending.has(tabId)) return;
  const { lazyTabs } = await getSettings();
  if (!lazyTabs) {
    pending.delete(tabId);
    await persist();
    return;
  }
  if (tab.active) {
    pending.delete(tabId);
    await persist();
    return;
  }
  if (tab.discarded) return;
  // Once the tab has a load status it's usually discardable — retry.
  if (changeInfo.status === "loading" || changeInfo.status === "complete") {
    tryDiscard(tabId);
  }
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
});

chrome.runtime.onInstalled.addListener(async () => {
  const cur = await chrome.storage.local.get(DEFAULTS);
  await chrome.storage.local.set({ ...DEFAULTS, ...cur }); // ensure keys exist, keep user values
});
