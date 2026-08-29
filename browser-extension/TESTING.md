# Manual test matrix

Automated where it makes sense (`node lib.test.mjs`, plus the backend curl in
the README). The rest is browser behaviour — walk these by hand in Brave.

## Queue submission

Feedback is on the icon **badge** + tooltip (hover the icon after each action).

| Case | Action | Expected badge/tooltip |
|---|---|---|
| Active `http://` tab | left-click icon | green `1`, tooltip `Added: 1` |
| Active `https://` tab | left-click icon | green `1` |
| Active `brave://…` tab | left-click icon | red `×`, `No web page to add` |
| 10 tabs to the right | right-click → Add tabs to the right | green `10`, queue order matches tab order |
| No tabs to the right | right-click → Add tabs to the right | red `×`, `No web page to add` |
| All tabs | right-click → Add all tabs | web tabs added; internal ones in `Skipped` |
| Duplicate URLs across tabs | Add all tabs | duplicates collapsed (one queue row), in `Skipped` |
| Re-submit already-queued | any | amber count, tooltip `Already queued: N` |
| Scrapescape stopped | any | red `!`, `Scrapescape unavailable: …` (nothing added) |
| Server adds some, dupes others | Add all tabs | tooltip shows `Added` vs `Already queued` from the response |

Confirm rows land in Scrapescape's queue in the browser tab order, and that
tabs are **not** closed, navigated, or modified by any of the above. Also check
the right-click **Lazy background tabs** checkbox reflects and flips the setting,
and stays in sync with the Options page.

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
