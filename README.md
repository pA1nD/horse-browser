<p align="center">
  <img src="https://raw.githubusercontent.com/pA1nD/pa1nd-media/main/horse-browser/banner-animated.svg" alt="Horse Browser — a dedicated browser for AI agents: colored per-session tab groups and a celestial navigation trail" width="100%" />
</p>
<!-- static fallback if the animated SVG ever fails to render:
<p align="center">
  <img src="https://raw.githubusercontent.com/pA1nD/pa1nd-media/main/horse-browser/banner.jpg" alt="Horse Browser" width="100%" />
</p>
-->

# Horse Browser 🐴

**A browser where your agents live.** Log in once; every agent you point at it inherits the session — and you watch them all on one live wall.

<p align="center">
  <a href="https://github.com/pA1nD/horse-browser/releases"><img src="https://img.shields.io/github/v/release/pA1nD/horse-browser?color=2f855a&label=release" alt="release" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-2f855a" alt="MIT" /></a>
  <img src="https://img.shields.io/badge/platform-macOS-2f855a" alt="macOS" />
</p>

Every other tool hands each agent a *throwaway* browser. But you don't want throwaway — you want **your** browser, logged into your stuff, that agents quietly borrow. The catch: on macOS, every CDP command yanks the browser to the foreground ([a known open sore](https://github.com/ChromeDevTools/chrome-devtools-mcp/issues/1254) across [the big tools](https://github.com/vercel-labs/agent-browser/issues/1247)). Horse Browser fixes that — agents open tabs **in the background**, each in their own **colored group**, in a **dedicated browser** that coexists with your daily one and never steals your focus.

> Why "horse"? *Browse* meant a horse grazing — nibbling shoots — long before it meant clicking links. A horse that browses is the *original* browser. And it rides in [browser-harness](https://github.com/browser-use/browser-harness): you harness a horse. 🐴

## Setup

**Install (macOS, Node 18+):**

```bash
npm install -g @pa1nd/horse-browser
```

That fetches a dedicated **Chrome for Testing** (lives alongside your daily browser, never fights it for the dock), puts the `horse-browser` launcher on your PATH, and wires in the extension. Then start it and **sign into your apps once** — those logins persist for every agent:

```bash
horse-browser        # launches the dedicated browser, no focus steal
```

To *drive* it, you also need **browser-harness** (a small Python tool horse-browser wraps) — a one-time prereq:

```bash
uv tool install browser-harness      # or: pipx install browser-harness
```

Prefer your own Chromium? `export HORSE_BROWSER_BIN=/path/to/chromium` before installing.

<details>
<summary>Or hand the repo to your agent (clone + <code>./install.sh</code>)</summary>

You don't have to install by hand. Paste into **Claude Code** or **Codex**:

```text
Set up https://github.com/pA1nD/horse-browser for me.

Clone it, run ./install.sh, then add SKILL.md to my CLAUDE.md so you always use
bh_open to open tabs from now on.
```

`install.sh` does the same setup as the npm install, and additionally wires `SKILL.md` into your `CLAUDE.md` (the npm install prints that import line for you to add — see below).
</details>

### Teaching agents the discipline

`SKILL.md` isn't an optional skill — it's a guardrail (open tabs with `bh_open`, never bare `goto_url`) that must be in context *before* the first browser call. So you register it by importing it into a `CLAUDE.md`, not as a load-on-demand skill.

**Recommended** — let `claude-md.sh` manage a small, marked block in your global `~/.claude/CLAUDE.md`:

```bash
./claude-md.sh apply     # write/refresh the block (idempotent)
./claude-md.sh print     # just print it — to compare, or copy by hand
./claude-md.sh check     # is CLAUDE.md up to date? (exit 1 if drifted — good for a cron)
```

It imports both playbooks — horse-browser's, and browser-harness's via a **stable symlink** it keeps aimed at the current install, so the `@`-import never rots when browser-harness is reinstalled on a different Python (`install.sh` refreshes that symlink too).

**By hand** — or just add the import yourself (global `~/.claude/CLAUDE.md`, or a single repo's `CLAUDE.md`):

```text
@~/path/to/horse-browser/SKILL.md
```

(Codex users: `~/.codex/AGENTS.md` works the same way.) Either way, the agent loads the `bh_open` discipline automatically from then on.

> **Installed via npm?** `SKILL.md` lives inside the global package — the installer prints the exact `@…/SKILL.md` line to paste, or find it with `echo "$(npm root -g)/@pa1nd/horse-browser/SKILL.md"`.

## How it stays out of your way

Sign into Gmail, GitHub, your dashboards, whatever — **once** — and every agent you point at `:9223` inherits those sessions. No re-auth dance, no cookie juggling, no "paste your token" on every run. The catch with one shared browser is everyone trips over everyone, so three things keep it civil:

- **Coexists with your daily browser.** A *separate* browser (Chrome for Testing) — launching it never hijacks your everyday Chrome/Brave, and clicking yours never lands you in the agents' window.
- **Focus-safe by construction.** Tabs open in the background and activate through the extension instead of `Target.activateTarget` (which calls `[NSApp activate]` and yanks the browser over whatever you're doing). The page is told it's foregrounded via focus emulation, so nothing misbehaves.
- **Per-session tab groups.** Each agent's tabs live in their own colored group; you see whose-is-whose at a glance, humans and agents in one window.

(Browserbase, Steel, Hyperbrowser & co. solve scale by giving each agent its *own throwaway* browser. Great for the cloud; useless when you want **one real browser, on your machine, that stays logged in**. That's this.)

## Watch them all — the Agent Monitor

<p align="center">
  <img src="https://raw.githubusercontent.com/pA1nD/pa1nd-media/main/horse-browser/monitor.jpg" alt="The Agent Monitor — a live grid of every agent's tabs" width="100%" />
</p>

Click the 🐴 toolbar button for a live **2×2 / 3×3 wall** of screencasts — one tab per cell — so you can watch every agent browse at once on a big screen.

Built around **stable slots**: a tab keeps its cell, so the picture never shuffles under you. Activity lights up *in place* (a green pulse on the tab an agent just acted on) instead of reordering everything; the wall only changes membership when a tab has gone idle and a busier one is waiting. A theme-aware sidebar **groups every tab by session** — coloured Chrome-style groups (emoji + code), favicon rows, and a slot number on each on-wall tab so a row maps straight to its preview. Click any pane to jump to that tab. Pure read-only over CDP (a *second* client alongside whatever's driving), so it costs the agents nothing.

## How agents drive it

`horse-browser` is a drop-in for [browser-harness](https://github.com/browser-use/browser-harness) that brings the dedicated browser up first (launching if down, self-healing a frozen GPU after sleep) and points the harness at it — so agents never touch a port:

```bash
horse-browser <<'PY'
bh_open("https://example.com")   # own colored tab group, no focus steal
print(page_info())
PY
```

It owns the CDP endpoint, so the port lives in exactly one place (its config) — change it there and every agent follows. Under the hood it's just a dedicated browser speaking CDP, so any other CDP client can still attach the classic way:

```bash
horse-browser && export BU_CDP_URL=http://127.0.0.1:9223   # for an arbitrary CDP client
```

Agents open tabs with `bh_open(url)` (their own colored group, no focus steal) rather than bare `goto_url` (which clobbers whoever's focused). The discipline lives in [SKILL.md](SKILL.md) — import it into a `CLAUDE.md` ([see Setup](#teaching-agents-the-discipline)) and agents follow it automatically.

## What's inside

- **`extension/`** — MV3 extension: the tab grouper, the Agent Monitor (sidebar collapsed by default), and a first-run welcome page.
- **`bin/horse-browser`** — the launcher *and* a browser-harness drop-in: ensures the browser is up (self-heals a GPU wedge after sleep), then runs your script against it. Also `horse-browser status` (versions + state) and `horse-browser update` (fetch the latest Chrome for Testing — it has no auto-updater — and restart onto it).
- **`install.sh`** — one-time setup; fetches the browser, registers the launcher + helpers.
- **`claude-md.sh`** — registers horse-browser's guidance in your `~/.claude/CLAUDE.md` (`apply`/`print`/`check`), via a version-proof symlink to the browser-harness SKILL.
- **`SKILL.md`** — the agent's playbook (the `bh_open` discipline + a helper recipe it self-installs).

A thin, self-contained setup — browser-harness (or anything else speaking CDP) is just a *consumer* on port 9223.

## License

MIT © pa1nd
