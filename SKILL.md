---
name: test-brave
description: Project-local Brave (port 9223) plus a tab-grouper extension that puts every browser-harness tab into a per-session group. ALWAYS open tabs with `bh_open(url)` in this project, never raw `new_tab(url)`. If `bh_open` isn't defined, install it from the recipe below BEFORE opening any tab. Use whenever you'd use `browser-harness` in a project that imports this skill — rendering, scraping, navigation, screenshots, anything that opens tabs.
---

# test-brave + tab grouper

**Rule:** in this project, open tabs with `bh_open(url)` not `new_tab(url)`. If `bh_open` is undefined in your browser-harness call, install it from the [recipe](#extension) below *before* opening any tab. End-of-task: prune your group with `bh_list()` and `cdp("Target.closeTarget", ...)`.

**Never just be lazy and use `goto_url`.** It navigates whichever tab is currently focused in Brave — disastrous for other agents (it hijacks their work) and humans (it clobbers the tab they're reading). Open with `bh_open(url)`; re-navigate within your own session-grouped tab via `bh_switch_tab(tid)` first, then `goto_url(url)`. The only legitimate place for bare `goto_url` is *inside* `bh_open` itself.

## Browser

Dedicated Brave profile, separate from the user's daily browser.

- Binary: `/Applications/Brave Browser.app/Contents/MacOS/Brave Browser`
- Profile: `~/.config/test-brave/`
- CDP: `http://127.0.0.1:9223`  →  `export BU_CDP_URL=http://127.0.0.1:9223`

If it's not already running on 9223, launch it (or just run `bin/test-brave`):

```bash
open -na "Brave Browser" --args \
  --remote-debugging-port=9223 \
  --user-data-dir="$HOME/.config/test-brave" \
  --load-extension="$PWD/extension" \
  --no-first-run --no-default-browser-check
```

`open -na` is detached (doesn't block the shell). The profile lock means a duplicate `open` is harmless — the second instance hands off to the first.

## Extension

Gives each Claude session its own coloured tab group (label = last 4 chars of `CLAUDE_CODE_SESSION_ID`; subagents inherit it and share the group). Keeps RAM and tab-strip clutter from bleeding across parallel sessions, and lets you reason about "my tabs" as a real set. `chrome.tabGroups` is extension-only — no CDP equivalent — which is why an extension exists at all.

The service worker exposes three async functions on `self`:

```js
self.bhGroup(targetId: string, label: string) -> Promise<number>
//   Puts the tab into a group titled `label`. Creates the group if missing
//   (colour deterministic from `label`).
//   targetId: CDP target id (uppercase hex string, e.g. "3C39F0B4…").
//   label:    string used as the group title.
//   Returns:  chrome tab id (number).
//   Throws:   Error("no tab for CDP target ...") if targetId has no live tab.

self.bhActivate(targetId: string) -> Promise<number>
//   Makes this tab the visible tab in its Brave window WITHOUT raising Brave
//   over your current macOS app. Replaces CDP Target.activateTarget, which
//   calls [NSApp activate] and steals focus while the agent works. Returns
//   the chrome tab id.

self.bhList(label: string) -> Promise<Tab[]>
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

Reach them over CDP by attaching to the extension's service-worker target and `Runtime.evaluate`. Drop `agent-helpers/bh.py` into `~/Developer/browser-harness/agent-workspace/agent_helpers.py` (auto-loaded on every browser-harness call) and you get `bh_open(url)` everywhere:

```python
import json
from browser_harness.helpers import cdp


def ext_call(fn, *args):
    """Call an extension SW function. Returns the deserialised JS value,
    or None if the extension's service worker isn't registered."""
    sw = next((t["targetId"] for t in cdp("Target.getTargets")["targetInfos"]
               if t.get("type") == "service_worker"
               and t.get("url", "").startswith("chrome-extension://")), None)
    if sw is None:
        return None
    s = cdp("Target.attachToTarget", targetId=sw, flatten=True)["sessionId"]
    a = ", ".join(json.dumps(x) for x in args)
    try:
        return cdp("Runtime.evaluate", session_id=s,
                   expression=f"self.{fn}({a})",
                   awaitPromise=True, returnByValue=True)["result"].get("value")
    finally:
        cdp("Target.detachFromTarget", sessionId=s)
```

Build whatever helpers the project needs on top of `ext_call`. The canonical
trio for this project — `bh_open` opens a tab without raising Brave over your
current macOS app, and tells the renderer to always behave as if focused:

```python
import os
from browser_harness.helpers import cdp, _send, goto_url, wait_for_load


def _label():
    return os.environ.get("CLAUDE_CODE_SESSION_ID", "")[-4:]


def bh_switch_tab(target_id):
    # Drop-in replacement for helpers.switch_tab that does NOT call
    # Target.activateTarget (which fires [NSApp activate] on macOS and yanks
    # Brave over your current app). Tab-strip activation goes through the
    # extension; focus emulation makes the page believe it's foregrounded.
    try: cdp("Runtime.evaluate", expression="if(document.title.startsWith('\U0001F434 '))document.title=document.title.slice(3)")
    except Exception: pass
    sid = cdp("Target.attachToTarget", targetId=target_id, flatten=True)["sessionId"]
    _send({"meta": "set_session", "session_id": sid, "target_id": target_id})
    cdp("Emulation.setFocusEmulationEnabled", enabled=True)
    try: cdp("Runtime.evaluate", expression="if(!document.title.startsWith('\U0001F434'))document.title='\U0001F434 '+document.title")
    except Exception: pass
    ext_call("bhActivate", target_id)
    return sid


def bh_open(url):
    # background=True on createTarget keeps [NSApp activate] from firing.
    tid = cdp("Target.createTarget", url="about:blank", background=True)["targetId"]
    bh_switch_tab(tid)
    if url != "about:blank":
        goto_url(url)
    wait_for_load()
    if _label():
        ext_call("bhGroup", tid, _label())
    return tid


def bh_list():
    return ext_call("bhList", _label()) or [] if _label() else []
```

You pass CDP `targetId`s only — the extension bridges to chrome `tabId`s internally.

### Why this avoids stealing macOS focus

Two ingredients:

1. **`Target.createTarget(background=True)` + `chrome.tabs.update({active:true})` instead of `Target.activateTarget`.** The latter calls `[NSApp activate]` on macOS and pulls Brave over whatever app you're in. `chrome.tabs.update` is documented to "not affect whether the window is focused" — it only changes which tab is visible inside Brave.
2. **`Emulation.setFocusEmulationEnabled` per attached session.** Makes the renderer treat the page as always focused — `document.hasFocus()` returns true, `requestAnimationFrame` runs at full rate, focus/blur events fire as if user-driven. The OS app focus state is independent of this; the emulation just stops sites and Chromium internals from misbehaving because the tab "looks" backgrounded.

Native popovers (autofill, password save, translate) are *not* gated by focus emulation — they're triggered in the browser process per-form-field. If you see typing-time focus theft, those are the likely culprit; disable them at the profile level rather than chasing them through CDP.

## Tab discipline

Regularly (e.g. before opening many tabs), glance at `bh_list()` and close stale ones via raw CDP. Close the rest when a task ends. Chrome removes empty groups, so a disciplined session leaves zero clutter.
