#!/usr/bin/env bash
# claude-md.sh — manage horse-browser's entry in your global ~/.claude/CLAUDE.md.
#
# Why a script: Claude Code loads CLAUDE.md verbatim, so its @-imports must be LITERAL
# paths — they can't run a command to resolve anything. But the packaged browser-harness
# SKILL.md lives under a Python-version-specific dir (…/lib/python3.12/site-packages/…)
# that moves whenever browser-harness is reinstalled on a different Python. So instead of
# hardcoding that brittle path, we point CLAUDE.md at a STABLE symlink we keep aimed at the
# current SKILL, and manage a marked block so updates stay idempotent and checkable.
#
# Usage:
#   ./claude-md.sh print      print the canonical block to stdout (compare / copy by hand)
#   ./claude-md.sh check      is CLAUDE.md's block + the symlink current? (exit 0=yes, 1=drifted)
#   ./claude-md.sh apply      (re)point the symlink + write the block into ~/.claude/CLAUDE.md
#   ./claude-md.sh symlink    only (re)point the stable symlink at the current SKILL
#
# Env overrides: CLAUDE_MD, HORSE_BROWSER_BH_SKILL_LINK.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_MD="${CLAUDE_MD:-$HOME/.claude/CLAUDE.md}"
LINK="${HORSE_BROWSER_BH_SKILL_LINK:-$HOME/.config/horse-browser/browser-harness-skill.md}"
BEGIN="<!-- horse-browser:begin (managed by claude-md.sh — edits between markers are overwritten) -->"
END="<!-- horse-browser:end -->"

_tilde() { echo "$1" | sed "s|^$HOME|~|"; }

# Echo the current packaged browser-harness SKILL.md path by ASKING browser-harness itself
# (its own interpreter, via the CLI shebang) — version- and install-agnostic. Empty if absent.
resolve_bh_skill() {
  command -v browser-harness >/dev/null 2>&1 || return 0
  local py; py="$(head -1 "$(command -v browser-harness)" 2>/dev/null | sed 's/^#!//;s/ .*//')"
  [ -n "$py" ] && [ -x "$py" ] || return 0
  "$py" -c 'import browser_harness, os; print(os.path.join(os.path.dirname(browser_harness.__file__), "SKILL.md"))' 2>/dev/null
}

ensure_symlink() {
  local target; target="$(resolve_bh_skill)"
  if [ -z "$target" ] || [ ! -f "$target" ]; then
    echo "claude-md: couldn't resolve the browser-harness SKILL — is browser-harness installed?" >&2
    return 1
  fi
  mkdir -p "$(dirname "$LINK")"
  ln -sfn "$target" "$LINK"
}

# The canonical block. Kept tiny on purpose — it's loaded into every session's context; the
# @-imports pull the full SKILLs on demand.
block() {
  cat <<EOF
$BEGIN
# Browsing

A dedicated, persistent CDP browser + tab-grouper extension (per-session colour groups, no
focus steal). Drive it with \`horse-browser <<'PY' … PY\` — a drop-in for browser-harness;
open tabs with \`bh_open(url)\`, then navigate, scrape, render, screenshot.

@$(_tilde "$LINK")
@$(_tilde "$HERE")/SKILL.md
$END
EOF
}

# Extract the current managed block (BEGIN..END inclusive) from CLAUDE.md, or empty.
_current_block() {
  [ -f "$CLAUDE_MD" ] || return 0
  awk -v b="$BEGIN" -v e="$END" '$0==b{f=1} f{print} $0==e{f=0; exit}' "$CLAUDE_MD"
}

apply() {
  ensure_symlink
  mkdir -p "$(dirname "$CLAUDE_MD")"; [ -f "$CLAUDE_MD" ] || : > "$CLAUDE_MD"
  local tmp; tmp="$(mktemp)"
  # drop any existing managed block, then trim trailing blank lines
  awk -v b="$BEGIN" -v e="$END" '$0==b{skip=1} !skip{print} $0==e{skip=0}' "$CLAUDE_MD" \
    | awk 'NF{last=NR} {buf[NR]=$0} END{for(i=1;i<=last;i++)print buf[i]}' > "$tmp"
  { [ -s "$tmp" ] && printf '\n'; block; } >> "$tmp"
  mv "$tmp" "$CLAUDE_MD"
  echo "claude-md: wrote block + symlink → $CLAUDE_MD"
}

check() {
  local rc=0
  if [ "$(_current_block)" != "$(block)" ]; then
    echo "claude-md: block in $CLAUDE_MD is DRIFTED (or missing)" >&2; rc=1
  fi
  local want have; want="$(resolve_bh_skill)"; have="$(readlink "$LINK" 2>/dev/null || true)"
  if [ -z "$want" ]; then
    echo "claude-md: cannot resolve browser-harness SKILL (not installed?)" >&2; rc=1
  elif [ "$have" != "$want" ]; then
    echo "claude-md: symlink stale — points at '${have:-<none>}', should be '$want'" >&2; rc=1
  fi
  [ "$rc" = 0 ] && echo "claude-md: up to date" || echo "claude-md: run './claude-md.sh apply' to fix" >&2
  return $rc
}

case "${1:-print}" in
  print)   block ;;
  apply)   apply ;;
  check)   check ;;
  symlink) ensure_symlink && echo "claude-md: symlink $(_tilde "$LINK") → $(readlink "$LINK")" ;;
  *) echo "usage: $(basename "$0") [print|apply|check|symlink]" >&2; exit 2 ;;
esac
