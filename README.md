# test-brave

A dedicated [Brave](https://brave.com) profile for browser automation, plus a
small MV3 extension that puts every automation tab into a per-session, coloured
tab group — so parallel agents (and you) never clobber each other's tabs.

It's a thin, self-contained setup: a launcher and one extension. It runs on its
own; [browser-harness](https://github.com/browser-use/browser-harness) (or
anything else speaking CDP) is just a *consumer* that drives it on port 9223.

## What's here

```
bin/test-brave        launcher — Brave on a dedicated profile, CDP :9223, extension loaded
extension/            the tab-grouper (MV3): bhGroup / bhActivate / bhList over CDP
agent-helpers/bh.py   bh_open / bh_switch_tab / bh_list for browser-harness
SKILL.md              usage + the bh_open discipline (read this)
install.md            setup steps
```

## Quick start

```bash
bin/test-brave                          # launch Brave (profile + CDP :9223 + extension)
export BU_CDP_URL=http://127.0.0.1:9223 # point your CDP client at it
```

The browser and extension need nothing else. To drive it with browser-harness
and get session-grouped tabs, copy `agent-helpers/bh.py` into browser-harness's
workspace — see [install.md](install.md).

## Why

`Target.activateTarget` (raw CDP) calls `[NSApp activate]` on macOS and yanks
Brave over whatever you're doing. `goto_url` navigates whatever tab is focused,
hijacking other agents' work. This setup avoids both: tabs open in the
background, get grouped per session, and the page is told it's foregrounded via
focus emulation. See [SKILL.md](SKILL.md) for the full rationale.

## License

MIT © pa1nd
