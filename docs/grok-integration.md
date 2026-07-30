# grok integration

Status: identity landed 2026-07-30 and verified against grok 0.2.114. Lanes not yet wired.

## What a grok tool process actually gets

Established by running `env | grep -E "^(GROK|CLAUDE)_"` through a real grok tool call:

```
GROK_AGENT=1
CLAUDE_CODE_SESSION_ID=ac1f5d87-…      ← inherited, because that grok was launched
                                          from a Claude Code session
```

Two things follow, and the second is a bug we already had:

- **`GROK_AGENT=1` is the only grok variable a tool process gets.** `GROK_SESSION_ID` does
  exist, but the hook runner injects it into **hook** processes, not tools
  (`~/.grok/docs/user-guide/10-hooks.md`, "Runner-injected variables").
- **`CLAUDE_CODE_SESSION_ID` leaks into any child process**, including another agent's CLI.
  Before the `GROK_AGENT` guard in `integrations/claude-code/detect.sh`, a grok session
  started from a Claude session was handed the *Claude* session's identity — same tab group,
  same daemon, same tab registry. Two agents, one lane. Guarded and tested in
  `tests/detect-identity.sh`.

## How identity is derived

`integrations/grok/detect.sh`, in order:

1. `GROK_SESSION_ID` when present (a hook process, or anything that exported it).
2. Walk up to the nearest `grok` ancestor, then look that pid up in
   `~/.grok/active_sessions.json`:
   ```json
   [{"session_id": "019fb004-…", "pid": 9573, "cwd": "…", "opened_at": "…"}]
   ```
   One walk yields the session id *and* `BH_ANCHOR_PID`, exactly like the claude-code detector.
3. Otherwise the workspace root, hashed: `grok-ws<cksum>`.

Step 3 is not a formality — **headless `grok -p` does not register in `active_sessions.json`**,
so it is the path a one-shot run actually takes. Verified end to end: horse-browser driven
from inside a real grok session resolved to `HORSE_SESSION=grok-ws1245865848`,
`BU_NAME=hb-ws1245865848`.

Its cost is honest: two grok sessions in one directory share a tab group. That is coarser than
Claude Code's per-session grouping, and still strictly better than inheriting a neighbour's id.

## Lanes: the mechanism is there, the payload differs

grok's hooks are close to Claude Code's — same two events, and the docs cite Claude
compatibility throughout:

| | Claude Code | grok |
| --- | --- | --- |
| events | `PreToolUse`, `SubagentStop` | same (`SubagentEnd` aliases `SubagentStop`) |
| transport | JSON on stdin | JSON on stdin |
| **key case** | `hook_event_name`, `session_id`, `tool_input` | **`hookEventName`, `sessionId`, `toolInput`** |
| session id in env | — | `GROK_SESSION_ID` (hook processes) |
| config | `~/.claude/settings.json` | `~/.grok/config.toml` `[[hooks.PreToolUse]]`, or hook JSON files |

So `lane-hook.py` will not work under grok as written: it reads `hook_event_name` /
`agent_id` / `session_id`, and grok sends camelCase. The fix is small — normalise the envelope
on entry and read `agent_id` from grok's subagent field — but it is unwritten and untested, so
lanes are Claude-Code-only today. A grok session still gets its own tab group and daemon via
identity above; only its *subagents* would share the parent's lane.

Worth knowing before wiring it: grok also has a Claude-compat importer
(`claude_import.rs`) that can pull hooks out of `~/.claude/settings.json` into
`config.toml`. If that ever runs on a machine where our lane hook is wired, grok would invoke
`lane-hook.sh` with a camelCase payload — which today's script silently ignores rather than
mishandles (its `case` gate matches on literal substrings), so the failure mode is "no lanes",
not corruption.

## Rules

grok reads `~/.claude/rules/` and `CLAUDE.md` directly (the strings in `claude_import.rs`
confirm `.claude/rules/` and `.cursor/rules/` scanning), so `horse-browser.md` and the
broker's `horse-browser-auth.md` already reach a grok session with no work on our side. It has
its own `~/.grok/rules/` too, currently empty.

## Next, if lanes matter under grok

1. Normalise the hook envelope in `lane-hook.py` (accept camelCase and snake_case).
2. Find grok's subagent identifier in a `SubagentStop` payload — `agent_id` has no documented
   grok equivalent, and the lane name needs it.
3. Wire via `~/.grok/config.toml` `[[hooks.PreToolUse]]` rather than relying on the Claude
   importer, so the wiring is explicit and greppable.
4. Extend `ensure_lane_hook()` to keep that TOML block current, the same way it does
   `settings.json`.
