#!/bin/bash
# Single-line statusline:  cwd  branch[*]  model  ctx:NN%  ses:XXXX  › last prompt
# The last user prompt fills the remaining terminal width (truncated to fit, no wrap).
# Width comes from $COLUMNS, which Claude Code sets before running this (v2.1.153+).
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

# Build the line in two parallel strings: `line1` (with colour) for display, and
# `plain` (no colour) so we can measure the real visible width.
line1="${C_DIR}${dir_short}${C_RST}"; plain="$dir_short"
[ -n "$branch" ]    && { line1="${line1}  ${C_DIM}${branch}${dirty}${C_RST}"; plain="${plain}  ${branch}${dirty}"; }
[ -n "$model" ]     && { line1="${line1}  ${C_DIM}${model}${C_RST}";          plain="${plain}  ${model}"; }
[ -n "$used" ]      && { pct="ctx:$(printf %.0f "$used")%"; line1="${line1}  ${C_DIM}${pct}${C_RST}"; plain="${plain}  ${pct}"; }
[ -n "$sid_short" ] && { line1="${line1}  ${C_DIM}ses:${sid_short}${C_RST}";  plain="${plain}  ses:${sid_short}"; }

# Reintroduce the last user prompt, filling whatever width is left on this line.
last_msg=""
if [ -n "$transcript" ] && [ -f "$transcript" ]; then
  last_msg=$(tail -n 2000 "$transcript" 2>/dev/null \
    | jq -r 'select(.type=="user") | if (.message.content | type) == "string" then .message.content else empty end' 2>/dev/null \
    | grep -vE '^[[:space:]]*<' \
    | grep -vE '^[[:space:]]*$' \
    | tail -1 | tr '\n' ' ' | cut -c1-400)
fi
if [ -n "$last_msg" ]; then
  width=${COLUMNS:-120}
  avail=$(( width - ${#plain} - 6 ))   # 6 = "  › " separator + safety margin
  if [ "$avail" -ge 12 ]; then
    [ "${#last_msg}" -gt "$avail" ] && last_msg="${last_msg:0:$((avail - 1))}…"
    line1="${line1}  ${C_DIM}› ${last_msg}${C_RST}"
  fi
fi

# %s (not %b): the colours are already real ESC bytes via $'…', and %s won't
# misinterpret backslashes that may appear in the user's prompt text.
printf '%s' "$line1"
