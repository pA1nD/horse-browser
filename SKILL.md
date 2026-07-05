---
name: horse-browser
description: A dedicated, persistent CDP browser plus a tab-grouper extension that drops each agent's tabs into its own per-session group and opens them without stealing OS focus. Use whenever you drive a browser via horse-browser (a browser-harness drop-in) — opening tabs, navigating, scraping, rendering, screenshots.
---

# horse-browser

**Rule:** when this skill is active, open tabs with `bh_open(url)` not `new_tab(url)`. If `bh_open` is undefined in your browser-harness call, install it from the [recipe](#extension) below *before* opening any tab. End-of-task: prune your group with `bh_list()` and `cdp("Target.closeTarget", ...)`.

**Never reach for bare `goto_url`.** It navigates whichever tab is currently focused in the browser — disastrous for other agents (it hijacks their work) and humans (it clobbers the tab they're reading). Open with `bh_open(url)`; re-navigate within your own session-grouped tab via `bh_switch_tab(tid)` first, then `goto_url(url)`. The only legitimate place for bare `goto_url` is *inside* `bh_open` itself.

## Driving the browser

`horse-browser` is a drop-in for `browser-harness` that always brings the dedicated
browser up first (launching it if down, **self-healing a frozen GPU after sleep**) and
points the harness at it for you. **Anywhere the browser-harness skill says
`browser-harness <<'PY'`, use `horse-browser <<'PY'` instead.** One command drives it:

```bash
horse-browser <<'PY'
tid = bh_open("https://example.com")   # your own coloured tab group, no focus steal
wait_for_load()
print(page_info())
PY
```

You never set `BU_CDP_URL` or name a port — **horse-browser owns the CDP endpoint** and
wires it up internally (change the port once in its config and the whole fleet follows).
Flags forward too (e.g. `horse-browser --doctor`). To *only* ensure the browser is up
without running a script, call it bare:

```bash
horse-browser            # launch if down, heal if wedged, no-op if healthy; then exit
```

**Never launch a browser yourself** — don't `open` Chrome/Brave, don't spawn your own
Chromium, and don't run `browser-harness` against some other Chrome. Only `horse-browser`.
It runs a *dedicated* browser (Chrome for Testing, own profile) that won't collide with
the user's daily browser; improvising would.

If `horse-browser` isn't on your PATH, the one-time setup hasn't been run — tell the user
to run the repo's `./install.sh` (fetches the browser, registers the launcher). Don't
attempt setup yourself.

## Input — use trusted, real events

Drive clicks and typing with **`click(css)`** and **`type_into(css, text)`** (or
`type_text(text)` into the already-focused field). They fire the same key/mouse events a
real browser generates, so the page's `keyup`/`input`/`mousedown` listeners actually run —
submit buttons enable, autocompletes fire, React/Vue state updates, menus open. **Never
drive a form with `el.value = …` or `el.click()` in `js(...)`**: those fire *no* events, so
the value or click *looks* applied while the page's logic never ran (disabled submit, dead
dropdown, stale state). This is **correctness, not just bot-evasion** — plain sites break
too; the anti-detection win rides along free.

- `click(css)` — trusted mousedown→mouseup→click (+pointer). `click_xy(x, y)` for coords /
  shadow DOM / cross-iframe (CDP input passes through iframes).
- `type_into(css, text, clear=?, enter=?)` — real per-char keys, fast. `type_text(text)`
  types the focused element. `press("Enter"|"Tab"|"Escape"|"Arrow…")` for a named key.
- Escape hatch: `insert_text_fast(text)` dumps via insertText (no key events) — only for a
  plain `<textarea>` with no listeners where speed matters.
- Fast untrusted (`js("el.click()")`) is fine on trivial internal/dev pages, but **always**
  use trusted input on any **login / signup / checkout**, anything behind a **bot vendor**
  (Akamai / PerimeterX / DataDome / Cloudflare / hCaptcha / reCAPTCHA), or **after any
  challenge appeared**.

### Easy challenges: solve them, don't halt

Many "captchas" are just a gesture — **click a checkbox, press-&-hold, slide-to-verify**.
Do them; don't escalate. With a real fingerprint (always-on) plus a trusted click, the easy
ones usually clear. Call **`solve_challenge()`** — it classifies the challenge and solves the
easy kind, or returns `escalate:<why>` for the **perception** kind (identify images, read
distorted text, rotate, audio) — and *only those* go to the operator. The gesture verbs:
`press_hold(css, seconds)` and `drag(css, dx=… / to=(x,y))`.

## Extension

Gives each Claude session — and each subagent within it — its own coloured tab group, automatically; you never manage this. Keeps RAM and tab-strip clutter from bleeding across parallel sessions and parallel subagents, and lets you reason about "my tabs" as a real set. `chrome.tabGroups` is extension-only — no CDP equivalent — which is why an extension exists at all.

The service worker exposes three async functions on `self`:

```js
self.groupTab(targetId: string, label: string) -> Promise<number>
//   Puts the tab into a group titled `label`. Creates the group if missing
//   (colour deterministic from `label`).
//   targetId: CDP target id (uppercase hex string, e.g. "3C39F0B4…").
//   label:    string used as the group title.
//   Returns:  chrome tab id (number).
//   Throws:   Error("no tab for CDP target ...") if targetId has no live tab.

self.activateTab(targetId: string) -> Promise<number>
//   Makes this tab the visible tab in its browser window WITHOUT raising the
//   browser over your current macOS app. Replaces CDP Target.activateTarget, which
//   calls [NSApp activate] and steals focus while the agent works. Returns
//   the chrome tab id.

self.listTabs(label: string) -> Promise<Tab[]>
//   Returns metadata for every tab whose group title equals `label`.
//   Returns []  if no group with that title exists.
//   Tab = {
//     targetId:     string | null,   // CDP target id; null if no live target
//     tabId:        number,          // chrome tab id
//     url:          string,
//     title:        string,
//     lastAccessed: number | null,   // ms since epoch; chrome may omit
//     discarded:    boolean,
//     audible:      boolean,
//     active:       boolean,
//   }
```

**`bh_open` / `bh_list` / `bh_switch_tab` are pre-installed.** `install.sh` writes them to browser-harness's `agent-workspace/horse_helpers.py`, loaded on every call via a small stub it adds once to `agent_helpers.py` (your own additions to that file are never touched), so they're available immediately — just call `bh_open(url)`.

If `bh_open` is somehow undefined (a browser-harness checkout that never ran our `install.sh`), re-run `horse-browser`'s `install.sh` to install them — don't hand-roll your own; the focus-safe behaviour is subtle. You pass CDP `targetId`s only — the extension bridges to chrome `tabId`s internally.

### Why this avoids stealing macOS focus

Two ingredients:

1. **`Target.createTarget(background=True)` + `chrome.tabs.update({active:true})` instead of `Target.activateTarget`.** The latter calls `[NSApp activate]` on macOS and pulls the browser over whatever app you're in. `chrome.tabs.update` is documented to "not affect whether the window is focused" — it only changes which tab is visible inside the browser.
2. **`Emulation.setFocusEmulationEnabled` per attached session.** Makes the renderer treat the page as always focused — `document.hasFocus()` returns true, `requestAnimationFrame` runs at full rate, focus/blur events fire as if user-driven. The OS app focus state is independent of this; the emulation just stops sites and Chromium internals from misbehaving because the tab "looks" backgrounded.

Native popovers (autofill, password save, translate) are *not* gated by focus emulation — they're triggered in the browser process per-form-field. If you see typing-time focus theft, those are the likely culprit; disable them at the profile level rather than chasing them through CDP.

## Tab discipline

Regularly (e.g. before opening many tabs), glance at `bh_list()` and close stale ones via raw CDP. Close the rest when a task ends. Chrome removes empty groups, so a disciplined session leaves zero clutter.
