# grok integration

Status: identity + session hook landed 2026-07-30, verified against grok 0.2.114.
Per-subagent lanes are blocked by grok — see below.

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
2. Walk up to the nearest `grok` ancestor, then two lookups keyed on that pid:
   - `~/.config/horse-browser/grok-sessions/<pid>` — written by our own session hook (below).
     This is the path that normally hits.
   - `~/.grok/active_sessions.json` — grok's own registry, as a backstop:
     ```json
     [{"session_id": "019fb004-…", "pid": 9573, "cwd": "…", "opened_at": "…"}]
     ```
   Either way the same walk yields `BH_ANCHOR_PID`, exactly like the claude-code detector.
3. Otherwise the workspace root, hashed: `grok-ws<cksum>`.

Step 3 was the path every headless run took before the session hook existed, because
**`grok -p` does not register in `active_sessions.json`**. It is now the last resort it should
be — reached only when grok is running without our hook installed. Its cost, when reached: two
grok sessions in one directory share a tab group. Coarser than per-session, and still strictly
better than inheriting a neighbour's id.

## Lanes: blocked by grok, not by us

Per-subagent lanes are **not implementable on grok 0.2.114**. Established by capturing real
hook payloads (a probe hook in `~/.grok/hooks/`, removed afterwards) and by comparing a
parent's tool process against a subagent's.

The good news first — grok is *better* than Claude Code here at the session level. Every
subagent gets its own `sessionId`, right down to its tool calls:

```
pre_tool_use  toolName=spawn_subagent        sessionId=019fb18d-db59-…   ← parent
pre_tool_use  toolName=run_terminal_command  sessionId=019fb18d-e9a7-…   ← the SUBAGENT's own
subagent_stop                                sessionId=019fb18d-e9a7-…   == subagentId
```

Claude Code shares one `CLAUDE_CODE_SESSION_ID` across all subagents, which is *why* lanes
exist there. grok has real per-subagent identity already.

The blocker is that none of it reaches the tool process:

- **No command rewriting.** grok's `PreToolUse` output vocabulary is `{"decision":
  "allow"|"deny"}` only. Claude Code's hook can rewrite the Bash command to inject the lane;
  grok's cannot.
- **The tool process cannot tell what it is.** A parent's and a subagent's tool call are
  indistinguishable — same 3 `GROK_*` variables, and the same grok process as ancestor:

  ```
  PARENT     54165 54129 sh  ←  54129 52904 /bin/zsh
  SUBAGENT   54680 54677 sh  ←  54677 52904 /bin/zsh
  ```

So there is no point at which a subagent's `horse-browser` call can be recognised as one.
A marker file written by `PreToolUse` would race, because grok runs subagents concurrently —
and mis-assigning a tab to the wrong subagent is worse than sharing one group.

Consequence: a grok session gets its own tab group and daemon; its **subagents share it** —
Claude Code's behaviour before lanes. Nothing is broken, tabs just aren't split per subagent.

This becomes implementable the moment grok's `PreToolUse` can modify `toolInput`, or exports
`GROK_SESSION_ID` to tool processes. Either one is enough, and the detector already prefers
`GROK_SESSION_ID` when present.

## What IS wired: the session hook

`integrations/grok/session-hook.sh`, registered by `ensure_lane_hook()` into
`~/.grok/hooks/horse-browser.json` (a global, always-trusted hook source — a file we own
outright, never merged into anyone's `config.toml`).

On `SessionStart` it writes the real `GROK_SESSION_ID` to
`~/.config/horse-browser/grok-sessions/<grok-pid>`; on `SessionEnd` it removes it. The pid is
the one key a hook process and a tool process can both compute and agree on — each walks up
its own ancestry to the nearest `grok`.

Measured effect, same command in the same workspace:

```
before   HORSE_SESSION=grok-ws1245865848              (workspace hash — collides)
after    HORSE_SESSION=grok-019fb191-120b-7193-…      (the real session id)
```

Confirmed live mid-session: `grok-sessions/3727` → `019fb191-8817-7b52-9739-bad34bbf93cf`,
while `active_sessions.json` held only a stale unrelated entry — so the hook is the source,
not the fallback. This also closes the headless gap: `grok -p` never registers in
`active_sessions.json`, and now it does not need to.

## A note on `~/.claude/settings.json`

grok scans it as an **always-trusted global hook source**, so the Claude lane hook we wire is
already being loaded by grok. It is harmless: `lane-hook.sh` gates on the literal substrings
`SubagentStop` and `agent_id`, and grok sends `subagent_stop` and `sessionId`, so the script
exits 0 without acting. The failure mode is "no lanes", never corruption — but it is worth
knowing that the file is shared, if its gating is ever loosened.

## Rules

grok reads `~/.claude/rules/` and `CLAUDE.md` directly (the strings in `claude_import.rs`
confirm `.claude/rules/` and `.cursor/rules/` scanning), so `horse-browser.md` and the
broker's `horse-browser-auth.md` already reach a grok session with no work on our side. It has
its own `~/.grok/rules/` too, currently empty.

## If grok adds what lanes need

1. `PreToolUse` able to modify `toolInput`, **or** `GROK_SESSION_ID` exported to tool
   processes — either alone unblocks it.
2. Then normalise the envelope in `lane-hook.py`: grok sends camelCase
   (`hookEventName` / `sessionId` / `toolInput`) where Claude sends snake_case.
3. Use `subagentId` from the payload as the lane name; it is already present on
   `SubagentStart` and `SubagentStop`.
