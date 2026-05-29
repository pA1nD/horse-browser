# test-brave

A dedicated [Brave](https://brave.com) profile for your agents — one you **log
into once and stay logged into** — plus a small MV3 extension that drops every
automation tab into a per-session, coloured group so agents (and you) never
clobber each other's tabs.

It's a thin, self-contained setup: a launcher and one extension. It runs on its
own; [browser-harness](https://github.com/browser-use/browser-harness) (or
anything else speaking CDP) is just a *consumer* that drives it on port 9223.

## What's here

```
extension/      Agent Tab Grouper (MV3): groupTab / activateTab / listTabs over CDP
install.sh      one-time setup — registers the statusline + first Brave launch
statusline.sh   Claude Code statusline — shows ses:XXXX = your tab-group label
SKILL.md        usage + the bh_open discipline (incl. the helper recipe agents self-install)
```

## Install

```bash
git clone https://github.com/pa1nd/test-brave
cd test-brave
./install.sh
```

`install.sh` registers `statusline.sh` in your Claude Code settings and launches
the dedicated Brave profile (Agent Tab Grouper loaded) for the first time. Sign
into the apps you want your agents to use — those logins persist. Then point a
CDP client at it:

```bash
export BU_CDP_URL=http://127.0.0.1:9223
```

**Relaunch later** (from the repo dir):

```bash
open -na "Brave Browser" --args --remote-debugging-port=9223 \
  --user-data-dir="$HOME/.config/test-brave" --load-extension="$PWD/extension" \
  --no-first-run --no-default-browser-check
```

There's no helper file to copy. The first time an agent drives this with
browser-harness, it writes the `bh_open` helpers into browser-harness's
`agent-workspace/agent_helpers.py` from the recipe in [SKILL.md](SKILL.md) —
generic across every install. To make the discipline available everywhere,
install the skill globally: `ln -s "$PWD/SKILL.md" ~/.claude/skills/test-brave/SKILL.md`.

## Why a dedicated, logged-in browser?

The point *isn't* a throwaway browser — it's the opposite. Sign into Gmail,
GitHub, your dashboards, whatever — once — and every agent you point at `:9223`
inherits those sessions. No re-auth dance, no cookie juggling, no "paste your
token" on every run. A real, persistent, authenticated browser that's yours,
that agents borrow.

The catch with one shared browser is everyone trips over everyone — so:

- **Per-session tab groups.** Each agent's tabs live in their own coloured group; you see whose-is-whose at a glance, and humans + agents coexist in one window.
- **Focus-safe by construction.** Tabs open in the background and activate through the extension instead of `Target.activateTarget` (which calls `[NSApp activate]` and yanks Brave over whatever you're doing). The page is told it's foregrounded via focus emulation, so nothing misbehaves. See [SKILL.md](SKILL.md).

## Prior art (or: turns out this is a real problem)

The focus-stealing half isn't us being fussy — it's an open sore in the big tools:

- [chrome-devtools-mcp #1254](https://github.com/ChromeDevTools/chrome-devtools-mcp/issues/1254) — *"macOS: Chrome steals window focus on every CDP command."*
- [vercel-labs/agent-browser #1247](https://github.com/vercel-labs/agent-browser/issues/1247) — a feature request for exactly the background-open trick this already ships.

And most other answers — Playwright contexts, [Browserbase](https://www.browserbase.com), [Steel](https://steel.dev), Hyperbrowser — isolate by handing each agent its *own throwaway* browser. Great for scale; useless when you want **one real browser, on your machine, that stays logged in**. That's this.

## License

MIT © pa1nd
