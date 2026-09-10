// Pure, side-effect-free helpers shared by the popup and the service worker.
// Kept dependency-free and testable with `node lib.test.mjs`.

// Only real web pages are submittable. Everything else (brave://, chrome://,
// chrome-extension://, about:, devtools://, view-source:, file://, empty/newtab)
// is intentionally skipped — matching what Scrapescape can actually fetch.
export function isSubmittableUrl(url) {
  if (!url) return false;
  const u = String(url).trim().toLowerCase();
  return u.startsWith("http://") || u.startsWith("https://");
}

// Remove duplicate URLs while preserving first-seen order.
export function dedupePreserveOrder(urls) {
  const seen = new Set();
  const out = [];
  for (const u of urls) {
    if (!seen.has(u)) {
      seen.add(u);
      out.push(u);
    }
  }
  return out;
}

// Given the current window's tabs, pick the ones for a mode and return an ordered,
// deduped list of submittable URLs plus counts of what was dropped.
//   mode: "current" | "right" | "all"
//   tabs: [{ url, pendingUrl, index, active }]
// Returns: { urls, skipped, duplicates }
export function prepareSubmission(tabs, mode) {
  const sorted = [...tabs].sort((a, b) => a.index - b.index);
  const active = sorted.find((t) => t.active);
  const activeIndex = active ? active.index : -1;

  let chosen;
  if (mode === "current") {
    chosen = active ? [active] : [];
  } else if (mode === "right") {
    chosen = sorted.filter((t) => t.index > activeIndex);
  } else {
    chosen = sorted;
  }

  const web = [];
  let skipped = 0;
  for (const t of chosen) {
    const url = (t.url || t.pendingUrl || "").trim();
    if (isSubmittableUrl(url)) web.push(url);
    else skipped += 1;
  }

  const urls = dedupePreserveOrder(web);
  return { urls, skipped, duplicates: web.length - urls.length };
}
