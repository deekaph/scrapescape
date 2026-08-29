// Offline self-check for lib.js. Run: node lib.test.mjs
import { isSubmittableUrl, dedupePreserveOrder, prepareSubmission } from "./lib.js";

let failures = 0;
function eq(got, want, label) {
  const g = JSON.stringify(got);
  const w = JSON.stringify(want);
  if (g !== w) {
    failures += 1;
    console.error(`FAIL ${label}\n  got:  ${g}\n  want: ${w}`);
  }
}

// isSubmittableUrl
eq(isSubmittableUrl("http://a.test/x"), true, "http ok");
eq(isSubmittableUrl("https://a.test/x"), true, "https ok");
eq(isSubmittableUrl("HTTPS://A.test"), true, "case-insensitive");
eq(isSubmittableUrl("brave://settings"), false, "brave:// skipped");
eq(isSubmittableUrl("chrome://newtab"), false, "chrome:// skipped");
eq(isSubmittableUrl("chrome-extension://abc/x"), false, "extension skipped");
eq(isSubmittableUrl("about:blank"), false, "about: skipped");
eq(isSubmittableUrl("devtools://x"), false, "devtools skipped");
eq(isSubmittableUrl("view-source:http://x"), false, "view-source skipped");
eq(isSubmittableUrl("file:///x"), false, "file skipped");
eq(isSubmittableUrl(""), false, "empty skipped");
eq(isSubmittableUrl(undefined), false, "undefined skipped");

// dedupePreserveOrder
eq(dedupePreserveOrder(["a", "b", "a", "c", "b"]), ["a", "b", "c"], "dedupe keeps order");

// prepareSubmission — a window of tabs. Active is index 2.
const tabs = [
  { index: 0, url: "https://one.test", active: false },
  { index: 1, url: "brave://settings", active: false },
  { index: 2, url: "https://active.test", active: true },
  { index: 3, url: "https://three.test", active: false },
  { index: 4, url: "https://one.test", active: false }, // dup of index 0
  { index: 5, url: "", pendingUrl: "https://pending.test", active: false }, // uses pendingUrl
  { index: 6, url: "about:blank", active: false },
];

eq(prepareSubmission(tabs, "current").urls, ["https://active.test"], "current = active only");

const right = prepareSubmission(tabs, "right");
eq(right.urls, ["https://three.test", "https://one.test", "https://pending.test"], "right preserves order, uses pendingUrl");
eq(right.skipped, 1, "right skipped about:blank");
eq(right.duplicates, 0, "right no dups among chosen");

const all = prepareSubmission(tabs, "all");
eq(all.urls, ["https://one.test", "https://active.test", "https://three.test", "https://pending.test"], "all: ordered + deduped");
eq(all.skipped, 2, "all skipped brave:// and about:blank");
eq(all.duplicates, 1, "all: one duplicate collapsed");

// Unsorted input still yields tab-order output.
const shuffled = [tabs[3], tabs[0], tabs[2], tabs[1]];
eq(prepareSubmission(shuffled, "all").urls, ["https://one.test", "https://active.test", "https://three.test"], "sorts by index");

if (failures) {
  console.error(`\n${failures} test(s) failed`);
  process.exit(1);
}
console.log("lib.js: all tests passed");
