// Scrapescape Companion — popup. Gathers tab URLs, hands them to the service
// worker for submission, and shows a compact result. No network calls here;
// the service worker owns the fetch (and the host permission for it).

import { prepareSubmission } from "./lib.js";

const $ = (id) => document.getElementById(id);
const statusEl = $("status");

function setStatus(text, kind = "") {
  statusEl.textContent = text;
  statusEl.className = "status" + (kind ? " " + kind : "");
}

async function currentWindowTabs() {
  return chrome.tabs.query({ currentWindow: true });
}

async function submit(mode, buttons) {
  const tabs = await currentWindowTabs();
  const prep = prepareSubmission(tabs, mode);
  const clientSkipped = prep.skipped + prep.duplicates;

  if (prep.urls.length === 0) {
    setStatus(clientSkipped ? `Nothing to add (${clientSkipped} skipped)` : "No submittable tabs", "err");
    return;
  }

  buttons.forEach((b) => (b.disabled = true));
  setStatus(`Sending ${prep.urls.length}…`);

  const resp = await chrome.runtime.sendMessage({ type: "submit", urls: prep.urls, source: "browser-extension" });

  buttons.forEach((b) => (b.disabled = false));

  if (!resp || !resp.ok) {
    setStatus(`Scrapescape unavailable: ${(resp && resp.error) || "no response"}`, "err");
    return;
  }

  const r = resp.result;
  const skipped = clientSkipped + (r.invalid || 0);
  const parts = [`Added: ${r.added}`];
  if (r.already_queued) parts.push(`Already queued: ${r.already_queued}`);
  if (skipped) parts.push(`Skipped: ${skipped}`);
  setStatus(parts.join(" · "), "ok");
  refreshCount();
}

async function refreshCount() {
  const resp = await chrome.runtime.sendMessage({ type: "queueCount" });
  $("qcount").textContent = resp && resp.ok ? `queue: ${resp.count}` : "";
}

async function init() {
  const { serverUrl, lazyTabs } = await chrome.storage.local.get({
    serverUrl: "http://127.0.0.1:8888",
    lazyTabs: false,
  });

  const toggle = $("lazyToggle");
  const stateLabel = $("lazyState");
  const paintToggle = (on) => {
    stateLabel.textContent = on ? "ON" : "OFF";
    stateLabel.classList.toggle("on", on);
  };
  toggle.checked = lazyTabs;
  paintToggle(lazyTabs);
  toggle.addEventListener("change", async () => {
    await chrome.storage.local.set({ lazyTabs: toggle.checked });
    paintToggle(toggle.checked);
    setStatus(`Lazy background tabs: ${toggle.checked ? "ON" : "OFF"}`);
  });

  const bCurrent = $("addCurrent");
  const bRight = $("addRight");
  const bAll = $("addAll");
  const all = [bCurrent, bRight, bAll];
  bCurrent.addEventListener("click", () => submit("current", all));
  bRight.addEventListener("click", () => submit("right", all));
  bAll.addEventListener("click", () => submit("all", all));

  $("openServer").addEventListener("click", (e) => {
    e.preventDefault();
    chrome.tabs.create({ url: serverUrl });
  });
  $("openOptions").addEventListener("click", (e) => {
    e.preventDefault();
    chrome.runtime.openOptionsPage();
  });

  refreshCount();
}

init();
