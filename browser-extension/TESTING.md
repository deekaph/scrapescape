# Manual test matrix

Automated where it makes sense (`node lib.test.mjs`, plus the backend curl in
the README). The rest is browser behaviour — walk these by hand in Brave.

## Queue submission

| Case | Expected |
|---|---|
| Active `http://` tab → Add Current Tab | `Added: 1` |
| Active `https://` tab → Add Current Tab | `Added: 1` |
| Active `brave://…` tab → Add Current Tab | `No submittable tabs` / `Nothing to add` |
| 10 tabs to the right → Add Tabs to Right | `Added: 10`, queue order matches tab order |
| No tabs to the right → Add Tabs to Right | `No submittable tabs` |
| Add All Tabs | all web tabs added, internal ones counted as Skipped |
| Duplicate URLs across tabs | duplicates collapsed (one queue row), shown as Skipped |
| Re-submit already-queued URLs | `Already queued: N` |
| Scrapescape stopped | `Scrapescape unavailable: …` (nothing marked as added) |
| Server rejects/does-not-add some | counts reflect `added` vs `already_queued` from the response |

Confirm rows land in Scrapescape's queue in the browser tab order, and that
tabs are **not** closed, navigated, or modified by any of the above.

## Lazy background tabs (feature ON)

Use `brave://discards` in a second tab to confirm state, and `free -h` for RAM.

| Case | Expected |
|---|---|
| Ctrl-click one link | new tab appears; in `brave://discards` it is **Discarded**, not Loaded |
| Ctrl-click 10 links rapidly | all appear; all become Discarded; RAM stays flat |
| Middle-click a link | same as Ctrl-click |
| Background tab still in tab strip | yes — title/URL visible, page unloaded |
| Select a discarded tab | it loads normally |
| Switch away after viewing it | it stays loaded — **not** re-discarded |
| Foreground-created tab (click a link normally / Ctrl-T then type) | loads normally, never discarded |
| New blank tab (Ctrl-T) | normal, not touched |
| Pinned tab | never discarded |
| Close a pending (never-viewed) tab | no errors; internal state cleaned up |
| Drag a pending tab to another window | tracking still works; still discardable/loads on view |
| Toggle feature OFF | new background tabs load normally again |
| Restart the browser | restored tabs behave normally; no discard loop, no unusable tabs |

## RAM verification (the point of the feature)

```bash
free -h                 # baseline
# Feature OFF: Ctrl-click ~20 video-site links, wait, then:
free -h                 # note the jump
# Feature ON: close them, Ctrl-click the same ~20 links, wait, then:
free -h                 # should stay close to baseline
```

In `brave://discards`, the lazy tabs should show **Discarded** (not
Loaded/hidden). If they show Loaded, the discard isn't taking effect.
