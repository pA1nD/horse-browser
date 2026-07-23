#!/usr/bin/env bash
# claude-md.sh — manage horse-browser's rule file at ~/.claude/rules/horse-browser.md.
#
# Rules files load into every Claude Code session exactly like ~/.claude/CLAUDE.md
# (same injection, @-imports expand the same way — verified empirically), but owning a
# whole file beats editing a marked block inside the user's CLAUDE.md: no merge logic,
# no risk of mangling their own instructions, trivially removable.
#
# Why a script: Claude Code loads the rule verbatim, so its @-imports must be LITERAL
# paths — they can't run a command to resolve anything. The harness SKILL ships inside
# the package dir, which under `npm -g` is a volatile Node-version path — so we keep a
# STABLE COPY at ~/.config/horse-browser and point the rule there, rewriting the whole
# (script-owned) file so updates stay idempotent.
#
# Usage:
#   ./claude-md.sh print      print the canonical rule to stdout (compare / copy by hand)
#   ./claude-md.sh check      is the rule file + the symlink current? (exit 0=yes, 1=drifted)
#   ./claude-md.sh apply      (re)point the symlink + write ~/.claude/rules/horse-browser.md
#                             (also removes the legacy managed block from ~/.claude/CLAUDE.md)
#   ./claude-md.sh symlink    only (re)point the stable symlink at the current SKILL
#   ./claude-md.sh skill      copy our own SKILL.md to a stable, Node-independent path
#
# Env overrides: HORSE_BROWSER_RULES_MD, CLAUDE_MD (legacy strip), HORSE_BROWSER_BH_SKILL_LINK,
# HORSE_BROWSER_SKILL.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RULES_MD="${HORSE_BROWSER_RULES_MD:-$HOME/.claude/rules/horse-browser.md}"
CLAUDE_MD="${CLAUDE_MD:-$HOME/.claude/CLAUDE.md}"
LINK="${HORSE_BROWSER_BH_SKILL_LINK:-$HOME/.config/horse-browser/browser-harness-skill.md}"
SKILL_COPY="${HORSE_BROWSER_SKILL:-$HOME/.config/horse-browser/skill.md}"
# Markers of the pre-rules era, when we managed a block INSIDE ~/.claude/CLAUDE.md.
# Kept only so apply/check can strip/flag a leftover block after an update.
BEGIN="<!-- horse-browser:begin (managed by claude-md.sh — edits between markers are overwritten) -->"
END="<!-- horse-browser:end -->"

_tilde() { echo "$1" | sed "s|^$HOME|~|"; }

# The harness (driving) SKILL ships vendored with this package. Empty if absent.
resolve_bh_skill() {
  local p="$HERE/harness/horse_harness/SKILL.md"
  [ -f "$p" ] && echo "$p"
}

ensure_symlink() {
  # Historically a symlink at the packaged browser-harness SKILL; now a stable COPY
  # of the vendored harness SKILL (a symlink into an npm dir rots on Node switches).
  local target; target="$(resolve_bh_skill)"
  if [ -z "$target" ]; then
    echo "claude-md: vendored harness SKILL missing ($HERE/harness/horse_harness/SKILL.md)" >&2
    return 1
  fi
  mkdir -p "$(dirname "$LINK")"
  rm -f "$LINK"
  cp "$target" "$LINK"
}

# Our OWN SKILL.md ships inside the package dir. Under `npm -g` that dir is deep in a
# Node-version-specific tree (…/fnm/node-versions/vX.Y.Z/…/node_modules/…) that moves or
# vanishes when Node is switched or upgraded — so a literal @-import of it rots. Copy it to
# a stable, Node-independent home; the rule points there instead of into the install dir.
ensure_skill() {
  mkdir -p "$(dirname "$SKILL_COPY")"
  cp "$HERE/SKILL.md" "$SKILL_COPY"
}

# Where the rule should @-import our SKILL from. A stable checkout is imported live (handy
# for dev); an npm-installed package (path contains /node_modules/) is volatile, so we point
# at the stable copy instead. Pure — the copy itself is (re)made by ensure_skill.
skill_import_path() {
  case "$HERE" in
    */node_modules/*) _tilde "$SKILL_COPY" ;;
    *)                echo "$(_tilde "$HERE")/SKILL.md" ;;
  esac
}

# The canonical rule file. Kept tiny on purpose — it's loaded into every session's context;
# the @-imports pull the full SKILLs on demand.
block() {
  cat <<EOF
<!-- horse-browser rule (managed by claude-md.sh — this whole file is overwritten on update) -->
# Browsing

A dedicated, persistent CDP browser + tab-grouper extension (per-session colour groups, no
focus steal). Drive it with \`horse-browser <<'PY' … PY\` — a drop-in for browser-harness;
open tabs with \`bh_open(url)\`, then navigate, scrape, render, screenshot.

@$(_tilde "$LINK")
@$(skill_import_path)
EOF
}

# Migration: drop the legacy managed block from ~/.claude/CLAUDE.md, if one is present.
strip_legacy_block() {
  [ -f "$CLAUDE_MD" ] && grep -qF "$BEGIN" "$CLAUDE_MD" || return 0
  local tmp; tmp="$(mktemp)"
  # drop the block, then trim trailing blank lines
  awk -v b="$BEGIN" -v e="$END" '$0==b{skip=1} !skip{print} $0==e{skip=0}' "$CLAUDE_MD" \
    | awk 'NF{last=NR} {buf[NR]=$0} END{for(i=1;i<=last;i++)print buf[i]}' > "$tmp"
  mv "$tmp" "$CLAUDE_MD"
  echo "claude-md: removed legacy block from $CLAUDE_MD"
}

apply() {
  ensure_symlink
  case "$HERE" in */node_modules/*) ensure_skill ;; esac
  mkdir -p "$(dirname "$RULES_MD")"
  block > "$RULES_MD"
  strip_legacy_block
  echo "claude-md: wrote rule + symlink → $RULES_MD"
}

check() {
  local rc=0
  if [ "$(cat "$RULES_MD" 2>/dev/null)" != "$(block)" ]; then
    echo "claude-md: rule file $RULES_MD is DRIFTED (or missing)" >&2; rc=1
  fi
  if [ -f "$CLAUDE_MD" ] && grep -qF "$BEGIN" "$CLAUDE_MD"; then
    echo "claude-md: legacy block still in $CLAUDE_MD — apply removes it" >&2; rc=1
  fi
  local want; want="$(resolve_bh_skill)"
  if [ -z "$want" ]; then
    echo "claude-md: vendored harness SKILL missing" >&2; rc=1
  elif ! cmp -s "$want" "$LINK" 2>/dev/null; then
    echo "claude-md: harness-skill copy stale/missing — $(_tilde "$LINK") ≠ vendored SKILL" >&2; rc=1
  fi
  case "$HERE" in
    */node_modules/*)
      cmp -s "$HERE/SKILL.md" "$SKILL_COPY" 2>/dev/null || {
        echo "claude-md: skill copy stale/missing — $(_tilde "$SKILL_COPY") ≠ packaged SKILL.md" >&2; rc=1; }
      ;;
  esac
  [ "$rc" = 0 ] && echo "claude-md: up to date" || echo "claude-md: run './claude-md.sh apply' to fix" >&2
  return $rc
}

case "${1:-print}" in
  print)   block ;;
  apply)   apply ;;
  check)   check ;;
  symlink) ensure_symlink && echo "claude-md: harness-skill copy $(_tilde "$LINK") ← vendored SKILL" ;;
  skill)   ensure_skill && echo "claude-md: skill copy $(_tilde "$SKILL_COPY") ← $(_tilde "$HERE")/SKILL.md" ;;
  *) echo "usage: $(basename "$0") [print|apply|check|symlink|skill]" >&2; exit 2 ;;
esac
