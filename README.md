<p align="center">
  <img src="./assets/horse-browser-banner-v2.png" alt="horse-browser — a dedicated browser for AI agents: colored per-session tab groups and a celestial navigation trail" width="100%" />
</p>

# horse-browser 🐴

**A browser where your agents live.** Log in once; every agent you point at it inherits the session — and you watch them all on one live wall.

<p align="center">
  <a href="https://github.com/pA1nD/horse-browser/releases"><img src="https://img.shields.io/github/v/release/pA1nD/horse-browser?color=2f855a&label=release" alt="release" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-2f855a" alt="MIT" /></a>
  <img src="https://img.shields.io/badge/platform-macOS-2f855a" alt="macOS" />
</p>

One dedicated browser, shared by all your agents. It coexists with your daily browser, **never steals your focus**, drops each agent's tabs into their own colored group — and ships a live monitor so you can watch every agent browse at once.

## Setup

You don't install this by hand. Hand the repo to your agent and let it do the work — paste into **Claude Code** or **Codex**:

```text
Set up https://github.com/pA1nD/horse-browser for me.

Read SKILL.md and run ./install.sh, then register the skill so you use bh_open
to open tabs from now on.
```

That's it. `install.sh` fetches a dedicated **Chrome for Testing** (lives alongside your daily browser, never fights it for the dock), puts the `horse-browser` launcher on your PATH, and opens the browser so you can **sign into your apps once** — those logins persist for every agent.

Prefer your own Chromium? `export HORSE_BROWSER_BIN=/path/to/chromium` before setup.

## Why a browser that *stays logged in*

The point isn't a throwaway browser — it's the opposite. Sign into Gmail, GitHub, your dashboards, whatever — **once** — and every agent you point at `:9223` inherits those sessions. No re-auth dance, no cookie juggling, no "paste your token" on every run.

The catch with one shared browser is everyone trips over everyone. So:

- **Coexists with your daily browser.** A *separate* browser (Chrome for Testing) — launching it never hijacks your everyday Chrome/Brave, and clicking yours never lands you in the agents' window.
- **Focus-safe by construction.** Tabs open in the background and activate through the extension instead of `Target.activateTarget` (which calls `[NSApp activate]` and yanks the browser over whatever you're doing). The page is told it's foregrounded via focus emulation, so nothing misbehaves.
- **Per-session tab groups.** Each agent's tabs live in their own colored group; you see whose-is-whose at a glance, humans and agents in one window.

## Watch them all — the Agent Monitor

<p align="center">
  <img src="./assets/monitor.png" alt="The Agent Monitor — a live grid of every agent's tabs" width="100%" />
</p>

Click the 🐴 toolbar button for a live **2×2 / 3×3 wall** of screencasts — one tab per cell — so you can watch every agent browse at once on a big screen.

Built around **stable slots**: a tab keeps its cell, so the picture never shuffles under you. Activity lights up *in place* (a green pulse on the tab an agent just acted on) instead of reordering everything; the wall only changes membership when a tab has gone idle and a busier one is waiting. A theme-aware sidebar lists every tab — slot-numbered, recency-ranked, with a cutoff line marking what's on the wall vs. waiting. Click any pane to jump to that tab. Pure read-only over CDP (a *second* client alongside whatever's driving), so it costs the agents nothing.

## How agents drive it

`horse-browser` runs a dedicated browser with CDP on `:9223`. Anything that speaks CDP can drive it — [browser-harness](https://github.com/browser-use/browser-harness) is the natural fit:

```bash
horse-browser                            # idempotent: launches if down, no-op if up
export BU_CDP_URL=http://127.0.0.1:9223  # point your CDP client at it
```

Agents open tabs with `bh_open(url)` (their own colored group, no focus steal) rather than bare `goto_url` (which clobbers whoever's focused). The discipline lives in [SKILL.md](SKILL.md) — register it once and agents follow it automatically.

## What's inside

```
extension/         MV3 extension — tab grouper (groupTab/activateTab/listTabs over CDP)
                   + the Agent Monitor (live grid of every agent's tabs)
bin/horse-browser  idempotent launcher — ensures the browser is up on :9223
install.sh         one-time setup — fetches the browser, registers launcher + statusline
statusline.sh      Claude Code statusline — shows ses:XXXX = your tab-group label
SKILL.md           the agent's playbook (bh_open discipline + the helper recipe it self-installs)
```

A thin, self-contained setup. browser-harness (or anything else speaking CDP) is just a *consumer* that drives it on port 9223.

## Why "horse"?

Two reasons, both real:

1. **"browse" literally comes from grazing animals.** It meant a deer or a horse nibbling leaves and shoots long before it meant clicking links. So a horse that browses isn't a pun — it's the *original* browser. 🐴
2. **It rides in [browser-harness](https://github.com/browser-use/browser-harness).** You put a harness on a horse. browser-harness drives the browser; this is the horse it harnesses.

The horse browses, the harness steers. Naturally.

## Prior art (turns out this is a real problem)

The focus-stealing half isn't us being fussy — it's an open sore in the big tools:

- [chrome-devtools-mcp #1254](https://github.com/ChromeDevTools/chrome-devtools-mcp/issues/1254) — *"macOS: Chrome steals window focus on every CDP command."*
- [vercel-labs/agent-browser #1247](https://github.com/vercel-labs/agent-browser/issues/1247) — a feature request for exactly the background-open trick this already ships.

And most other answers — Playwright contexts, [Browserbase](https://www.browserbase.com), [Steel](https://steel.dev), Hyperbrowser — isolate by handing each agent its *own throwaway* browser. Great for scale; useless when you want **one real browser, on your machine, that stays logged in**. That's this.

## License

MIT © pa1nd
