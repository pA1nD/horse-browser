# horse-browser 🐴

A dedicated browser for your AI agents — one you **log into once and stay logged
into** — that coexists peacefully with your daily browser. Agents share it, each
gets its own coloured tab group, and it never steals your focus.

### Why "horse"?

Two reasons, and they're both real:

1. **"browse" literally comes from grazing animals.** It meant a deer or a horse
   nibbling leaves and shoots long before it meant clicking links. So a horse
   that browses isn't a pun — it's the *original* browser. 🐴
2. **It rides in [browser-harness](https://github.com/browser-use/browser-harness).**
   You put a harness on a horse. browser-harness drives the browser; this is the
   horse it harnesses. (That's also where the 🐴 marker on the controlled tab
   comes from.)

So: the horse browses, the harness steers. Naturally.

## What's here

```
extension/      Agent Tab Grouper (MV3): groupTab / activateTab / listTabs over CDP
bin/horse-browser  idempotent launcher — ensures the browser is up on :9223
install.sh      one-time setup — fetches the browser, registers launcher + statusline
statusline.sh   Claude Code statusline — shows ses:XXXX = your tab-group label
SKILL.md        usage + the bh_open discipline (incl. the helper recipe agents self-install)
```

It's a thin, self-contained setup. [browser-harness](https://github.com/browser-use/browser-harness)
(or anything else speaking CDP) is just a *consumer* that drives it on port 9223.

## Install

```bash
git clone https://github.com/pa1nd/horse-browser
cd horse-browser
./install.sh
```

You don't install a browser by hand — `install.sh` fetches **Chrome for Testing**
(a dedicated, automation-purposed Chromium that lives *alongside* your daily
browser, never fighting it for the dock) via `@puppeteer/browsers`, registers the
`horse-browser` launcher on your PATH, wires up the statusline, and opens the
browser so you can sign into your apps. Prefer your own Chromium? Set
`HORSE_BROWSER_BIN=/path/to/chromium` before running.

Then, anytime — you or an agent:

```bash
horse-browser                            # idempotent: launches if down, no-op if up
export BU_CDP_URL=http://127.0.0.1:9223  # point your CDP client at it
```

There's no helper file to copy. The first time an agent drives this with
browser-harness, it writes the `bh_open` helpers into browser-harness's
`agent-workspace/agent_helpers.py` from the recipe in [SKILL.md](SKILL.md) —
generic across every install. To make the discipline available everywhere,
install the skill globally:
`ln -s "$PWD/SKILL.md" ~/.claude/skills/horse-browser/SKILL.md`.

## Why a dedicated, logged-in browser?

The point *isn't* a throwaway browser — it's the opposite. Sign into Gmail,
GitHub, your dashboards, whatever — once — and every agent you point at `:9223`
inherits those sessions. No re-auth dance, no cookie juggling, no "paste your
token" on every run. A real, persistent, authenticated browser that's yours,
that agents borrow.

The catch with one shared browser is everyone trips over everyone — so:

- **Coexists with your daily browser.** It's a *separate* browser (Chrome for Testing), so launching it never hijacks your everyday Brave/Chrome, and clicking yours never lands you in the agents' window.
- **Per-session tab groups.** Each agent's tabs live in their own coloured group; you see whose-is-whose at a glance, and humans + agents coexist in one window.
- **Focus-safe by construction.** Tabs open in the background and activate through the extension instead of `Target.activateTarget` (which calls `[NSApp activate]` and yanks the browser over whatever you're doing). The page is told it's foregrounded via focus emulation, so nothing misbehaves. See [SKILL.md](SKILL.md).

## Prior art (or: turns out this is a real problem)

The focus-stealing half isn't us being fussy — it's an open sore in the big tools:

- [chrome-devtools-mcp #1254](https://github.com/ChromeDevTools/chrome-devtools-mcp/issues/1254) — *"macOS: Chrome steals window focus on every CDP command."*
- [vercel-labs/agent-browser #1247](https://github.com/vercel-labs/agent-browser/issues/1247) — a feature request for exactly the background-open trick this already ships.

And most other answers — Playwright contexts, [Browserbase](https://www.browserbase.com), [Steel](https://steel.dev), Hyperbrowser — isolate by handing each agent its *own throwaway* browser. Great for scale; useless when you want **one real browser, on your machine, that stays logged in**. That's this.

## License

MIT © pa1nd
