#!/usr/bin/env bash
# claude-md.sh — install horse-browser's rule file at ~/.claude/rules/horse-browser.md.
#
# Rules files load into every Claude Code session exactly like ~/.claude/CLAUDE.md.
# Owning a whole file beats editing a marked block inside the user's CLAUDE.md: no merge
# logic, no risk of mangling their instructions, trivially removable.
#
# The rule is ONE self-contained file — the canonical RULE.md, copied verbatim. No
# @-imports, no symlinks, no per-Python skill copies: those rotted across reinstalls and
# left dangling links. The full reference lives on-demand behind `horse-browser skill`
# (MANUAL.md), not in every session's context. RULE.md is the single source of truth;
# edit it, then `apply`.
#
# Usage:
#   ./claude-md.sh print      print the canonical rule to stdout (compare / copy by hand)
#   ./claude-md.sh check      is the installed rule current? (exit 0=yes, 1=drifted)
#   ./claude-md.sh apply      write ~/.claude/rules/horse-browser.md (+ strip legacy CLAUDE.md block)
#
# Env overrides: HORSE_BROWSER_RULES_MD, CLAUDE_MD (legacy strip).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RULES_MD="${HORSE_BROWSER_RULES_MD:-$HOME/.claude/rules/horse-browser.md}"
CLAUDE_MD="${CLAUDE_MD:-$HOME/.claude/CLAUDE.md}"
RULE_SRC="$HERE/RULE.md"
# Markers of the pre-rules era, when we managed a block INSIDE ~/.claude/CLAUDE.md.
# Kept only so apply/check can strip/flag a leftover block after an update.
BEGIN="<!-- horse-browser:begin (managed by claude-md.sh — edits between markers are overwritten) -->"
END="<!-- horse-browser:end -->"

# A one-line header marks the file as script-owned (so a human knows edits are overwritten),
# then the canonical RULE.md verbatim.
block() {
  echo "<!-- horse-browser rule (managed by claude-md.sh — this whole file is overwritten by 'apply'; edit RULE.md in the package) -->"
  cat "$RULE_SRC"
}

# Migration: drop the legacy managed block from ~/.claude/CLAUDE.md, if one is present.
strip_legacy_block() {
  [ -f "$CLAUDE_MD" ] && grep -qF "$BEGIN" "$CLAUDE_MD" || return 0
  local tmp; tmp="$(mktemp)"
  awk -v b="$BEGIN" -v e="$END" '$0==b{skip=1} !skip{print} $0==e{skip=0}' "$CLAUDE_MD" \
    | awk 'NF{last=NR} {buf[NR]=$0} END{for(i=1;i<=last;i++)print buf[i]}' > "$tmp"
  mv "$tmp" "$CLAUDE_MD"
  echo "claude-md: removed legacy block from $CLAUDE_MD"
}

apply() {
  [ -f "$RULE_SRC" ] || { echo "claude-md: RULE.md missing ($RULE_SRC)" >&2; exit 1; }
  mkdir -p "$(dirname "$RULES_MD")"
  block > "$RULES_MD"
  strip_legacy_block
  echo "claude-md: wrote rule → $RULES_MD"
}

check() {
  local rc=0
  if [ "$(cat "$RULES_MD" 2>/dev/null)" != "$(block)" ]; then
    echo "claude-md: rule file $RULES_MD is DRIFTED (or missing)" >&2; rc=1
  fi
  if [ -f "$CLAUDE_MD" ] && grep -qF "$BEGIN" "$CLAUDE_MD"; then
    echo "claude-md: legacy block still in $CLAUDE_MD — apply removes it" >&2; rc=1
  fi
  [ "$rc" = 0 ] && echo "claude-md: up to date" || echo "claude-md: run './claude-md.sh apply' to fix" >&2
  return $rc
}

case "${1:-print}" in
  print)   block ;;
  apply)   apply ;;
  check)   check ;;
  *) echo "usage: $(basename "$0") [print|apply|check]" >&2; exit 2 ;;
esac
