#!/bin/sh
# lane-hook.sh — Claude Code hook entrypoint (wired by install.sh into
# ~/.claude/settings.json for PreToolUse:Bash and SubagentStop).
#
# PreToolUse fires on EVERY Bash call machine-wide, so this gate keeps the common
# case at shell-startup cost: only payloads that could matter (a subagent's
# horse-browser command, or a SubagentStop) pay the python3 startup. The substring
# checks are deliberately loose — lane-hook.py re-checks everything authoritatively.
payload=$(cat)
DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
case "$payload" in
  *SubagentStop*)
    printf '%s' "$payload" | python3 "$DIR/lane-hook.py"; exit 0 ;;
  *horse-browser*)
    case "$payload" in
      *agent_id*) printf '%s' "$payload" | python3 "$DIR/lane-hook.py"; exit 0 ;;
    esac ;;
esac
exit 0
