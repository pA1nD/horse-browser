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

> Why "horse"? *Browse* meant a horse grazing — nibbling shoots — long before it meant clicking links. A horse that browses is the *original* browser. And its harness began as a fork of [browser-harness](https://github.com/browser-use/browser-harness): you harness a horse. 🐴

## Setup

**Install (macOS, Node 18+):**

The harness that drives the browser is **vendored** (a fork of browser-harness's core with the shared-browser invariants baked into its daemon) — no separate install. Its private venv needs `uv` (preferred) or `python3` ≥3.11 on the machine:

```bash
npm install -g @pa1nd/horse-browser
```

The npm install fetches a dedicated **Chrome for Testing** (lives alongside your daily browser, never fights it for the dock), puts the `horse-browser` launcher on your PATH, and wires in the extension. Then start it and **sign into your apps once** — those logins persist for every agent:

```bash
horse-browser        # launches the dedicated browser, no focus steal
```

Prefer your own Chromium? `export HORSE_BROWSER_BIN=/path/to/chromium` before installing.

<details>
<summary>Or hand the repo to your agent (clone + <code>./install.sh</code>)</summary>

You don't have to install by hand. Paste into **Claude Code** or **Codex**:

```text
Set up https://github.com/pA1nD/horse-browser for me.

Clone it, run ./install.sh, then ./claude-md.sh apply so you always use
bh_open to open tabs from now on.
```

`install.sh` does the same setup as the npm install; `./claude-md.sh apply` then registers the rule file (the npm install prints that step for you — see below).
</details>

### Teaching agents the discipline

The paved path (open tabs with `bh_open`; the shared-browser sharp edges) must be in context *before* the first browser call — so it's an always-on rule, not a load-on-demand skill. It's **one self-contained file** (`RULE.md`, ~650 tokens, no `@`-imports); the full reference stays out of context until an agent asks for it via `horse-browser skill`.

**Recommended** — let `claude-md.sh` own a rule file at `~/.claude/rules/horse-browser.md` (rules files load into every session exactly like `CLAUDE.md`):

```bash
./claude-md.sh apply     # write/refresh the rule file (idempotent; also removes any legacy CLAUDE.md block)
./claude-md.sh print     # just print it — to compare, or copy by hand
./claude-md.sh check     # is the rule file up to date? (exit 1 if drifted — good for a cron)
```

`apply` copies `RULE.md` verbatim — no symlinks, no per-Python copies, nothing to rot across reinstalls. `RULE.md` is the single source; edit it and re-run `apply`.

**By hand** — or paste the rule into `~/.claude/CLAUDE.md` (or a single repo's `CLAUDE.md`) yourself: run `./claude-md.sh print` and drop the output in. (Codex users: `~/.codex/AGENTS.md` works the same way.) Either way, the agent loads the `bh_open` discipline automatically from then on.

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

`horse-browser` brings the dedicated browser up first (launching if down, self-healing a frozen GPU after sleep), then runs your script against it over CDP through its vendored harness — so agents never touch a port:

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

Agents open tabs with `bh_open(url)` — their own colored group, in the background, no focus steal. The discipline lives in [RULE.md](RULE.md) — register it as an always-on rule ([see Setup](#teaching-agents-the-discipline)) and agents follow it automatically. (Once open, plain `goto_url` is safe: the daemon pins each session to its own tab, so a navigation can't land on another agent's page.)

## Design philosophy — power, not guardrails

The reason a harness beats Playwright is **raw CDP**: the full protocol, and the agent's own intelligence to compose it. Playwright gives you a method per case — and the day you need a case it didn't wrap, you're fighting the abstraction instead of just using the browser. Horse Browser refuses that trade, which means resisting *two* temptations, not one:

- **Don't wrap every capability.** A helper per case (`screenshot(width,…)`, scroll-this-container, …) is just Playwright again. If an agent needs a sized screenshot, it composes `Emulation.setDeviceMetricsOverride` + `Page.captureScreenshot` itself — that's the point.
- **Don't jail raw CDP either.** Intercepting calls so "raw is safe by construction" is the *same* control reflex, one layer down. An agent *can* call `Target.activateTarget` (which would steal your focus) the same way it *can* `rm -rf` — and "can" doesn't mean "will." We don't sandbox the shell; we don't sandbox CDP.

So the harness does four things, then gets out of the way:

1. **Pave the default.** The calls the skill points agents to (`bh_open`, `capture_screenshot`, a focus-safe `new_tab`) have no footguns — the recommended road is paved, not walled.
2. **Teach the sharp edges.** One short, always-on note: *this is a shared browser; these specific raw calls steal the operator's focus or hang on an occluded window — here's why and the safe way.* Non-obvious side effects are the only thing worth a line in the manual; everything else the agent derives from CDP.
3. **Keep the substrate coherent.** The one thing that's structural, not etiquette: *your tab means your tab.* Session isolation isn't restricting power — it's the ground honoring what the agent meant.
4. **Trust the rest.** Full protocol, no wrapper per case, no interceptor-as-jail.

The test for anything we add: does it **encode knowledge** the agent can't derive (trusted input's real event sequences, challenge-solving gestures — keep) or **wrap a capability** it can compose itself (drop)? The differentiator was never "we prevent the bad calls." It's *full power, plus the knowledge to use it well.*

## What's inside

- **`extension/`** — MV3 extension: the tab grouper, the Agent Monitor (sidebar collapsed by default), and a first-run welcome page.
- **`bin/horse-browser`** — the launcher *and* the driver: ensures the browser is up (self-heals a GPU wedge after sleep), then runs your script against it through the vendored harness. Also `horse-browser status` (versions + state), `horse-browser skill` (print the manual), and `horse-browser update` (fetch the latest Chrome for Testing — it has no auto-updater — and restart onto it).
- **`harness/`** — the vendored CDP harness (`horse_harness`): the daemon that holds the websocket to Chrome, the pre-imported helpers, and the shared-browser invariants (focus-safe activate, per-session tab pinning, self-reap). Runs from a private venv; a fork of browser-harness's core, no external dependency.
- **`install.sh`** — one-time setup; fetches the browser, builds the harness venv, registers the launcher.
- **`claude-md.sh`** — installs horse-browser's guidance as a rule file at `~/.claude/rules/horse-browser.md` (`apply`/`print`/`check`) — one self-contained file, no imports.
- **`RULE.md`** — the always-on rule (the paved path + the sharp edges, ~650 tokens). **`MANUAL.md`** — the full on-demand reference (`horse-browser skill`).

A thin, self-contained setup — the vendored harness (or anything else speaking CDP) is just a *consumer* on port 9223.

## Tests

`npm test` runs `tests/e2e.sh` then `tests/tab-reap.sh` against the real browser. The e2e suite covers launcher basics and every stdin mode, session identity (daemon pinning, anchoring, reaper restraint), focus-safe tab grouping, helper-namespace integrity, trusted input on real pages, concurrent background-tab screenshots (reliable and non-hijacking under multi-agent load), the realness/anti-bot fingerprint, real-site flows, and the lifecycle races (hard-kill relaunch, 5-way launch stampede, stale-lock recovery). `tests/tab-reap.sh` covers the orphan-tab leak: a tab-less session's stray `about:blank`, and the reaper that closes an ended session's tabs while sparing live ones. The lifecycle section bounces the browser (tabs are preserved); `HB_TEST_FAST=1` skips it. GPU-wedge healing and display-asleep behaviour are machine-state dependent and stay manual.

`tests/captcha-bench.sh` (on-demand, not part of `npm test`) drives horse-browser against public captcha playgrounds — the official vendor demo widgets — and reports, per vendor, whether our realness + challenge-solving stack clears it. It's a bench rather than a gate because captchas are reputation/IP-based and non-deterministic. What "pass" means differs by class: **managed/score** captchas (Cloudflare Turnstile managed, reCAPTCHA v3) drop a token with *no interaction* — that's what the always-on fingerprint buys, and both auto-pass; **gesture** captchas (checkbox, slider, press-&-hold) are solved by `solve_challenge()` — same-document ones directly, cross-origin ones via a vision brief the driving agent acts on; **perception** captchas (image grids, distorted text, audio) escalate to the operator by design — those we deliberately do *not* claim to pass. `HB_CAPTCHA_SOLVE=1` attempts the gesture solves on the checkbox demos. The gesture *mechanics* (steady press-&-hold, slide-to-verify) are covered deterministically and offline by the e2e `[5]` fixtures; live-vendor bypass rates live in the hb-stealth Stealth Bench.

`tests/agent-e2e.sh` (on-demand, needs the `claude` CLI) is the true end-to-end check: a real headless agent (`claude -p`) is given a browser goal and must fulfil it through horse-browser. It parses the agent's tool-use stream to assert both that the agent *actually invoked* horse-browser (not curl or a fetch tool) and that it *returned the correct answer* off the live page — proving the whole chain (rules/skill teach the agent → it writes a driver script → the launcher brings the browser up focus-safely → it reads the page). It spawns an LLM agent, so it's non-deterministic and costs tokens; not part of `npm test`.

## License

MIT © pa1nd
