#!/bin/bash
# Single-line statusline:  cwd  branch[*]  model  ctx:NN%  ses:XXXX
#
# `ses:XXXX` is the last 4 chars of the session id — the SAME label the Agent
# Tab Grouper uses for this session's tab group. So you can match "which
# terminal am I in" to "which coloured group is mine" at a glance.
#
# Wire it up in Claude Code settings.json:
#   "statusLine": { "type": "command", "command": "/ABSOLUTE/PATH/TO/statusline.sh" }
#
# Requires: jq.

input=$(cat)
dir=$(echo "$input" | jq -r '.workspace.current_dir // empty')
model=$(echo "$input" | jq -r '.model.display_name // empty')
used=$(echo "$input" | jq -r '.context_window.used_percentage // empty')
transcript=$(echo "$input" | jq -r '.transcript_path // empty')
session_id=$(echo "$input" | jq -r '.session_id // empty')
# Fallback: parse from transcript filename (<session-id>.jsonl)
[ -z "$session_id" ] && [ -n "$transcript" ] && session_id=$(basename "$transcript" .jsonl)
sid_short=""
[ -n "$session_id" ] && sid_short="${session_id: -4}"

dir_short=${dir/#$HOME/\~}
branch=$(git -C "$dir" symbolic-ref --short HEAD 2>/dev/null)
dirty=""
if [ -n "$branch" ]; then
  git -C "$dir" diff --quiet --ignore-submodules HEAD 2>/dev/null || dirty="*"
fi

C_DIR=$'\033[36m'
C_DIM=$'\033[90m'
C_RST=$'\033[0m'

line1="${C_DIR}${dir_short}${C_RST}"
[ -n "$branch" ] && line1="${line1} ${C_DIM}${branch}${dirty}${C_RST}"
[ -n "$model" ] && line1="${line1}  ${C_DIM}${model}${C_RST}"
[ -n "$used" ] && line1="${line1} ${C_DIM}ctx:$(printf %.0f "$used")%${C_RST}"
[ -n "$sid_short" ] && line1="${line1} ${C_DIM}ses:${sid_short}${C_RST}"

printf '%b' "$line1"
