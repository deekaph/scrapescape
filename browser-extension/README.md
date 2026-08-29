# Scrapescape Companion (Brave / Chromium extension)

A first-party Manifest V3 extension for Scrapescape. Two jobs:

1. **Send tabs to the Scrapescape queue** — the current tab, every tab to the
   right of it, or every tab in the window.
2. **Lazy background tabs** — when you Ctrl/middle-click a pile of links, keep
   those background tabs **unloaded** until you actually view them, instead of
   letting Brave load all of them at once (tens of GB of RAM on video sites).

It is deliberately its own extension so no unknown third party gets access to
your browsing. It has no analytics, no remote code, and phones home to nothing.
The **only** data ever sent anywhere is the list of URLs you explicitly submit
with the three "Add …" buttons, and it goes only to your configured Scrapescape
server.

---

## Permissions and why each is needed

| Permission | Why |
|---|---|
| `tabs` | Read tab URLs/titles to submit them, and observe tab **create / activate** and call `tabs.discard()` for lazy tabs. This grants tab metadata (URL/title) without any access to page **content**. |
| `storage` | Save your settings (server URL, lazy-tabs toggle) and a small ephemeral "opened-in-background, not yet viewed" set (`storage.session`, cleared on browser restart). |
| `host_permissions`: `http://127.0.0.1/*`, `http://localhost/*` | Let the service worker POST to the default local Scrapescape server. Match patterns cover all ports. |
| `optional_host_permissions`: `http://*/*`, `https://*/*` | Only used if you point the extension at a **non-local** server; the Options page then requests permission for **exactly that one host** at save time. Nothing is granted until you do that. |

**Not requested (on purpose):** `<all_urls>` host access, `scripting`,
`activeTab`, `webNavigation`, content scripts. The extension never reads or
injects into page content.

---

## Install (unpacked) in Brave

1. Go to `brave://extensions`.
2. Turn on **Developer mode** (top-right).
3. Click **Load unpacked** and select this `browser-extension/` folder.
4. Pin the Scrapescape icon if you like.

Chromium/Chrome are identical via `chrome://extensions`.

## Configure the server

Default is `http://127.0.0.1:8888` (pre-authorized). To change it, open the
extension's **Options** (popup → *Options*, or the card on `brave://extensions`):

- Enter the server URL and click **Save & Test**.
- For a non-local host you'll get a one-time permission prompt scoped to that
  host only. Save & Test then confirms it can reach the queue.

No credentials are stored; Scrapescape stays auth-free and local. The queue
endpoint is protected from random websites by requiring a custom request header
(see *Security* below), not by a password.

---

## How lazy background tabs work

Toggle it in the popup or Options (default **OFF** until you enable it).

When ON, for every tab that is created **in the background** (Ctrl/middle-click)
with an `http(s)` URL, the service worker calls `chrome.tabs.discard()` on it —
**as soon as the tab's navigation commits a real URL** (not before; discarding an
uncommitted tab would leave it with nothing to reload and it would hang at
"Loading"). Each tab is discarded exactly once and then forgotten, so it is never
re-discarded in a loop. The tab stays in the tab strip showing its title/URL, but
its page is unloaded. When you click the tab, Chromium reloads it from its URL.
Once you've viewed a tab it is forgotten permanently, so switching away later
never re-discards it.

Left alone: the active tab, pinned tabs, internal pages (`brave://`, `about:`,
new-tab), and tabs restored on browser start (a short grace window after startup
avoids fighting Brave's own lazy session restore).

### Honest Chromium limitation

**Manifest V3 cannot stop a background tab's first navigation from beginning.**
`tabs.onCreated` fires *after* the navigation is already queued; there is no API
to intercept it. So the real sequence is:

```
tab created  →  navigation begins  →  extension discards it immediately  →  (on click) loads
```

This is **not** "the URL is retained and never fetched." A small initial request
may start before the discard cancels it. In practice discarding right away stops
the page from fully loading, which is what recovers the RAM — but the extension
does not and cannot pretend it prevents the first navigation outright. Verify the
effect with `brave://discards` (see below).

---

## Security

- The queue endpoint `POST /api/queue` **requires the header
  `X-Scrapescape-Extension: 1`.** A web page cannot set a custom header on a
  cross-origin request without a CORS preflight, which the local server never
  answers — so arbitrary sites can't reach the endpoint. The extension is
  privileged (host permission) and sets the header directly. This keeps
  Scrapescape's local no-auth model while not exposing the queue to the web.
- Tab create/activate events are handled locally and **never transmitted**.

---

## Development / testing

- **Shared logic self-check (Node):** `node lib.test.mjs` — covers URL
  filtering, order preservation, and dedupe.
- **Backend endpoint** (from the repo root, server running):
  ```bash
  curl -s -X POST http://127.0.0.1:8888/api/queue \
    -H 'Content-Type: application/json' -H 'X-Scrapescape-Extension: 1' \
    -d '{"urls":["https://example.com/a","https://example.com/b"]}'
  # -> {"added":2,"already_queued":0,"invalid":0,"total_received":2}
  ```
  Without the header the server returns `403`.
- **Icons** are generated by `icons/generate_icons.py` (stdlib only).
- **Manual matrix:** see [TESTING.md](TESTING.md).

After editing files, hit the reload ⟳ on the extension card in
`brave://extensions`.
