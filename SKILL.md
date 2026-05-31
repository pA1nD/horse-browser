---
name: horse-browser
description: A dedicated, persistent browser (CDP on :9223) plus a tab-grouper extension that drops each agent's tabs into its own per-session group and opens them without stealing OS focus. Use whenever you drive a browser via browser-harness — opening tabs, navigating, scraping, rendering, screenshots.
---

# horse-browser

**Rule:** when this skill is active, open tabs with `bh_open(url)` not `new_tab(url)`. If `bh_open` is undefined in your browser-harness call, install it from the [recipe](#extension) below *before* opening any tab. End-of-task: prune your group with `bh_list()` and `cdp("Target.closeTarget", ...)`.

**Never reach for bare `goto_url`.** It navigates whichever tab is currently focused in the browser — disastrous for other agents (it hijacks their work) and humans (it clobbers the tab they're reading). Open with `bh_open(url)`; re-navigate within your own session-grouped tab via `bh_switch_tab(tid)` first, then `goto_url(url)`. The only legitimate place for bare `goto_url` is *inside* `bh_open` itself.

## Starting the browser

Before opening any tab, ensure the browser is up. It's idempotent — just run:

```bash
horse-browser                            # launches the dedicated browser (own profile +
                                         # tab grouper) if it's down; no-op if already up.
                                         # Blocks until CDP :9223 is ready.
export BU_CDP_URL=http://127.0.0.1:9223  # point browser-harness at it
```

**Never launch a browser yourself** — don't `open` Chrome/Brave, don't spawn your own
Chromium. Only `horse-browser`. It runs a *dedicated* browser (Chrome for Testing, on its
own profile) that won't collide with the user's daily browser; improvising would.

If `horse-browser` isn't on your PATH, the one-time setup hasn't been run — tell the user
to run the repo's `./install.sh` (fetches the browser, registers the launcher). Don't
attempt setup yourself.

## Extension

Gives each Claude session its own coloured tab group (label = last 4 chars of `CLAUDE_CODE_SESSION_ID`; subagents inherit it and share the group). Keeps RAM and tab-strip clutter from bleeding across parallel sessions, and lets you reason about "my tabs" as a real set. `chrome.tabGroups` is extension-only — no CDP equivalent — which is why an extension exists at all.

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

**`bh_open` / `bh_list` / `bh_switch_tab` are pre-installed.** `install.sh` writes them into browser-harness's `agent-workspace/agent_helpers.py` (which auto-loads on every call), so they're available immediately — just call `bh_open(url)`.

If `bh_open` is somehow undefined (a browser-harness checkout that never ran our `install.sh`), re-run `horse-browser`'s `install.sh` to install them — don't hand-roll your own; the focus-safe behaviour is subtle. You pass CDP `targetId`s only — the extension bridges to chrome `tabId`s internally.

### Why this avoids stealing macOS focus

Two ingredients:

1. **`Target.createTarget(background=True)` + `chrome.tabs.update({active:true})` instead of `Target.activateTarget`.** The latter calls `[NSApp activate]` on macOS and pulls the browser over whatever app you're in. `chrome.tabs.update` is documented to "not affect whether the window is focused" — it only changes which tab is visible inside the browser.
2. **`Emulation.setFocusEmulationEnabled` per attached session.** Makes the renderer treat the page as always focused — `document.hasFocus()` returns true, `requestAnimationFrame` runs at full rate, focus/blur events fire as if user-driven. The OS app focus state is independent of this; the emulation just stops sites and Chromium internals from misbehaving because the tab "looks" backgrounded.

Native popovers (autofill, password save, translate) are *not* gated by focus emulation — they're triggered in the browser process per-form-field. If you see typing-time focus theft, those are the likely culprit; disable them at the profile level rather than chasing them through CDP.

## Tab discipline

Regularly (e.g. before opening many tabs), glance at `bh_list()` and close stale ones via raw CDP. Close the rest when a task ends. Chrome removes empty groups, so a disciplined session leaves zero clutter.
