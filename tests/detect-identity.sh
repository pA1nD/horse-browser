#!/usr/bin/env bash
# tests/detect-identity.sh — an agent session gets ITS OWN identity, never a neighbour's.
#
# HORSE_SESSION decides the tab group, the daemon name and the tab registry, so two agents
# resolving to one identity means two agents driving one lane — the exact isolation the
# shared browser exists to provide. The detectors are sourced in glob order by
# bin/horse-browser, so each one has to be certain the session is really its own.
#
# The bug this pins down: CLAUDE_CODE_SESSION_ID is an ordinary exported variable, so ANY
# process started from a Claude Code session inherits it — including another agent CLI.
# Verified live: `env` inside a grok tool call launched from a Claude session prints
# CLAUDE_CODE_SESSION_ID. Without a guard, that grok session is handed the Claude
# session's identity.
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
PASS=0; FAIL=0
say()  { printf '%s\n' "$*"; }
pass() { PASS=$((PASS+1)); say "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); say "  ✗ $1${2:+ — $2}"; }

# resolve <env assignments...> — run the detector chain exactly as the launcher does and
# print the HORSE_SESSION it settles on.
resolve() {
  env -i HOME="$HOME" PATH="$PATH" PWD="$PWD" "$@" bash -c '
    set -euo pipefail
    for d in '"$ROOT"'/integrations/*/detect.sh; do
      [ -f "$d" ] || continue
      . "$d"
      [ -n "${HORSE_SESSION:-}" ] && break
    done
    printf "%s\n" "${HORSE_SESSION:-<none>}"
  ' 2>/dev/null
}

say "horse-browser detect-identity"

# ── 1. plain Claude Code ───────────────────────────────────────────────────────────
got="$(resolve CLAUDE_CODE_SESSION_ID=claude-abc-123)"
[ "$got" = "claude-abc-123" ] \
  && pass "Claude Code session resolves to its own session id" \
  || fail "claude-code detect" "got '$got'"

# ── 2. THE regression: grok launched from inside a Claude session ──────────────────
got="$(resolve CLAUDE_CODE_SESSION_ID=claude-abc-123 GROK_AGENT=1 GROK_SESSION_ID=grok-xyz-789)"
[ "$got" != "claude-abc-123" ] \
  && pass "grok under Claude does NOT inherit the Claude session id" \
  || fail "grok must not inherit the Claude identity" "got '$got' — two agents, one lane"
[ "$got" = "grok-grok-xyz-789" ] \
  && pass "grok uses GROK_SESSION_ID when the hook runner provided it" \
  || fail "grok uses GROK_SESSION_ID" "got '$got'"

# ── 3. grok with no session id at all (tool processes don't get one) ───────────────
# Must still be grok-prefixed and stable — never a fallthrough to a neighbour's id.
a="$(resolve CLAUDE_CODE_SESSION_ID=claude-abc-123 GROK_AGENT=1)"
b="$(resolve CLAUDE_CODE_SESSION_ID=claude-abc-123 GROK_AGENT=1)"
case "$a" in
  grok-*) pass "grok without GROK_SESSION_ID still resolves to a grok identity" ;;
  *)      fail "grok fallback identity" "got '$a'" ;;
esac
[ "$a" = "$b" ] && pass "…and it is stable across calls (same workspace, same group)" \
  || fail "fallback identity is stable" "$a vs $b"
[ "$a" != "claude-abc-123" ] \
  && pass "…and never the inherited Claude id" \
  || fail "fallback must not be the Claude id" "got '$a'"

# ── 4. no agent at all → no identity (stock browser-harness behaviour) ─────────────
got="$(resolve)"
[ "$got" = "<none>" ] \
  && pass "no agent detected → no HORSE_SESSION (grouping simply skipped)" \
  || fail "no-agent case is side-effect free" "got '$got'"

# ── 5. an explicit HORSE_SESSION always wins ───────────────────────────────────────
got="$(resolve HORSE_SESSION=explicit-one CLAUDE_CODE_SESSION_ID=claude-abc-123 GROK_AGENT=1)"
[ "$got" = "explicit-one" ] \
  && pass "an explicit HORSE_SESSION overrides every detector" \
  || fail "explicit HORSE_SESSION wins" "got '$got'"

say ""
say "── $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
