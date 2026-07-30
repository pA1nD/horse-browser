# Attached mode — driving a CDP endpoint we didn't launch

Status: design, 2026-07-30. Not implemented.

## Goal

horse-browser today assumes it launched the browser: our macOS binary, our profile, our
extension, our launch flags. Attached mode drops all four assumptions, so the same harness can
drive **any** CDP endpoint — a container, a remote box, a cloud browser service — while the
desktop keeps everything it has today.

## Two modes, detected not configured

|                  | owned      | attached |
| ---------------- | ---------- | -------- |
| we launched it   | yes        | no       |
| extension        | loaded     | absent   |
| launch flags     | ours       | theirs   |
| operator present | yes        | no       |

The detector already exists: `tools/sw_eval.py` probes for `self.groupTab` and returns
`__not_horse_sw__` when the service worker isn't ours. Attached mode is every call site
tolerating that answer.

## What the extension is actually for

Only four service-worker functions are load-bearing outside the extension:

    groupTab      daemon.py:168, helpers.py:457,573
    listTabs      daemon.py:319, helpers.py:589, lane-hook.py:148
    activateTab   daemon.py:273
    reapDeadTabs  bin/horse-browser:625

Between them they provide exactly two things:

1. **A registry** — the tab-group title *is* the session→tab map, stored in the browser.
2. **Focus-safety** — `chrome.tabs.update({active:true})` instead of `Target.activateTarget`'s
   `[NSApp activate]`.

Off-desktop #2 is moot (nothing to steal focus from) and #1 moves to the daemon.

## De-duplication: what leaves the extension

Building a CDP path for something the extension already does creates a fork — two
implementations that drift. Three buckets:

### 1. Move out entirely — CDP is strictly better

**Realness** (`realchrome.js`, `rules.json`, `sync_realness_version`)
→ one `Emulation.setUserAgentOverride` with `userAgentMetadata`.

Verified against the live protocol: the struct carries `brands`, `fullVersionList`,
`fullVersion`, `platform`, `platformVersion`, `architecture`, `bitness`, `mobile`,
`formFactors`. Chrome then emits **both halves itself** — `navigator.userAgentData` *and* the
`sec-ch-ua` request header.

Deletes:

- `extension/realchrome.js` — 43 lines of `Proxy` patching, and those proxies are themselves a
  fingerprint surface we'd rather not present.
- `extension/rules.json`, plus the manifest's `declarativeNetRequest` permission and
  `rule_resources` block.
- `bin/horse-browser:789-814 sync_realness_version` — exists only to keep a hardcoded
  `"Chromium";v="151"` matched to the running Chrome. Emulation derives it live, so the whole
  class of drift bug (the CfT-version-mismatch scar) stops existing.
- `content_scripts` and `host_permissions` from the manifest.

Running both at once would be worse than either: Emulation sets the brands natively, then
realchrome.js rewrites them again from the UA string — two masks, one of them inconsistent.

**netlog** (`getNetLog`, `clearNetLog`, the `webRequest` listeners, the `NETLOG` map)
→ `Network.enable` per session, which is strictly richer.

Zero callers — verified across the repo, `~/.config/browser-harness/agent-workspace/`, and
`~/.config/horse-browser/`. Delete it. If the capability is wanted back it's a helper over
`Network.*`, not extension state. Drops the `webRequest` permission.

**`activeTabTargets`** — *keep*, despite having no caller in the harness. `tests/e2e.sh:518`
uses it to prove that screenshotting a background tab leaves the viewer's visible tab
untouched, and there is no other way to observe that: CDP targets carry no "visible" flag.
It's a test-only observability hook, and the invariant it guards is one of the load-bearing
ones. In attached mode the test skips, as it already does when the extension isn't live.

### 2. Invert ownership — one source of truth, two renderings

The session→tab map should live in **the daemon** in both modes, with the extension demoted to
a renderer of state it no longer owns:

- `listTabs` **goes away** — the daemon already knows which targets are its own.
- `reapDeadTabs` shrinks to daemon-side `Target.closeTarget`, calling the extension only to
  tidy the now-empty group chrome.
- `groupTab` **stays**, demoted to "paint this tab with this session's colour".

Durability caveat, and the reason not to do this naively: a group title survives the daemon
dying; a daemon-memory map does not. So the registry must be

- persisted per-profile on disk (same convention as `${PROFILE%/}.relaunch-tabs.json`),
- keyed on the **tab-type** targetId — `Target.getTargets({filter:[{type:"tab"}]})` — which is
  stable across cross-origin navigation, unlike a page target,
- rebuildable from group titles in owned mode, which makes the extension the desktop's backup
  store rather than its primary one.

### 3. Stays — no CDP equivalent exists

`groupTab` (as renderer), `activateTab`, the toolbar badge, the pinned Monitor, the keepalive
alarm. Confirmed against the live protocol (906 commands + events): no tab-group API, no pin
API, no badge API, and the only activation primitives — `Target.activateTarget` and
`Page.bringToFront` — both steal OS focus.

## Resulting extension

    permissions: tabs, tabGroups, debugger, alarms, storage
                 (was: + webRequest, declarativeNetRequest)
    files:       background.js, monitor.*, hello.*, icons
                 (was: + realchrome.js, rules.json)

Roughly 909 JS lines down to ~500, with a materially smaller permission surface.

## What attached mode still can't have

Not because of the extension — because we didn't launch the browser, so we lose the command
line too:

- **Reliable background-tab screenshots.** `--disable-renderer-backgrounding` and its
  neighbours are what keep a backgrounded renderer painting; without them
  `Page.captureScreenshot` on an unfocused tab returns a degenerate 2×2 (see the comment at
  `bin/horse-browser:1105`). This degrades on a foreign endpoint whether or not the extension
  is present.
- **`--remote-allow-origins=*`** — moot once the Monitor is served over HTTP.

## The risk to design against

Realness flips **fail-safe → fail-open**. The extension applies with zero clients attached;
`Emulation` needs a supervisor holding
`Target.setAutoAttach(autoAttach, waitForDebuggerOnStart, flatten)` for the browser's whole
life. If that supervisor dies, new tabs get the naked Chrome-for-Testing UA and nothing says
so. Mitigation: assert on each navigation that `navigator.userAgentData.brands` contains
`Google Chrome`, and fail loud.

## Work order

1. **Capability probe + graceful absence** (~1h) — the four call sites tolerate `None` from
   `sw_eval`. Shippable alone: horse-browser can drive a bare endpoint, minus groups.
2. **Delete netlog + `activeTabTargets`** (~15 min) — dead code, independent of everything else.
3. **Daemon-owned registry** (~2h) — persisted, tab-targetId-keyed; `listTabs` deleted,
   `reapDeadTabs` inverted.
4. **Realness over `Emulation`** (~half a day) — the auto-attach supervisor and its
   `waitForDebuggerOnStart` handshake, which is where the bugs live. Then delete
   `realchrome.js`, `rules.json`, and `sync_realness_version`.
5. **Monitor over HTTP** (~2h) — serve `extension/monitor.*` from the launcher; same files, with
   the server supplying the port instead of `chrome.storage.local`.

Steps 1 and 2 are independent and safe to land first.
