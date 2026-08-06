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

# A test run must never reach the operator's ~/.claude or ~/.grok. 16 of 19 suites once
# lacked this, so `npm test` from ANY clone wired that clone's path into the real global
# settings.json — which is how a build agent's throwaway checkout came to leave a dead
# hook behind that failed every Bash call on the machine. external-state.sh is the one
# suite that unsets this, against temp paths of its own.
export HORSE_BROWSER_NO_RECONCILE=1

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
PASS=0; FAIL=0
WORK="$(mktemp -d -t hb-identity.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT
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

# ── 6. the grok session hook publishes the id a tool process can't see ─────────────
# grok gives a TOOL process only GROK_AGENT=1; GROK_SESSION_ID reaches HOOK processes.
# The hook bridges that, keyed on the grok pid so a hook and a detector agree.
GS="$WORK/grok-sessions"
HOOK="$ROOT/integrations/grok/session-hook.sh"
HORSE_BROWSER_GROK_SESSIONS="$GS" HORSE_BROWSER_GROK_PID=4242 \
  GROK_HOOK_EVENT=session_start GROK_SESSION_ID=019fb191-abc "$HOOK" </dev/null
[ "$(cat "$GS/4242" 2>/dev/null)" = "019fb191-abc" ] \
  && pass "session_start publishes the real grok session id, keyed by grok pid" \
  || fail "session hook publishes the id" "got '$(cat "$GS/4242" 2>/dev/null)'"

HORSE_BROWSER_GROK_SESSIONS="$GS" HORSE_BROWSER_GROK_PID=4242 \
  GROK_HOOK_EVENT=session_end "$HOOK" </dev/null
[ ! -e "$GS/4242" ] \
  && pass "session_end removes it (the id is only valid while the session lives)" \
  || fail "session hook cleans up on session_end"

HORSE_BROWSER_GROK_SESSIONS="$GS" HORSE_BROWSER_GROK_PID=4243 \
  GROK_HOOK_EVENT=session_start "$HOOK" </dev/null      # no GROK_SESSION_ID
[ ! -e "$GS/4243" ] \
  && pass "writes nothing when grok gave no session id (never invents one)" \
  || fail "no id means no file" "wrote '$(cat "$GS/4243" 2>/dev/null)'"

# ── 7. the launcher registers the grok hook, and only rewrites it on drift ─────────
# The one place this suite lets the reconciler run — every target retargeted into $WORK, so
# the operator's real ~/.grok and ~/.claude are still untouched. See tests/external-state.sh.
GH="$WORK/grok-hooks"; mkdir -p "$GH"
grok_run() {
  env -u HORSE_BROWSER_NO_RECONCILE \
    HORSE_BROWSER_GROK_HOOKS="$GH" HORSE_BROWSER_CLAUDE_SETTINGS="$WORK/ignore.json" \
    HORSE_BROWSER_RULES_MD="$WORK/ignore-rule.md" HORSE_BROWSER_RECONCILE_STAMP="$1" \
    "$ROOT/bin/horse-browser" status >/dev/null 2>&1
}
grok_run "$WORK/gstamp"
python3 -c "
import json,sys
d=json.load(open('$GH/horse-browser.json'))
ev=set(d.get('hooks',{}))
assert ev == {'SessionStart','SessionEnd'}, ev
c=d['hooks']['SessionStart'][0]['hooks'][0]['command']
assert 'integrations/grok/session-hook.sh' in c, c
" 2>/dev/null \
  && pass "launcher registers ~/.grok/hooks/horse-browser.json (SessionStart + SessionEnd)" \
  || fail "launcher registers the grok hook" "$(cat "$GH/horse-browser.json" 2>/dev/null | head -3)"

before="$(cat "$GH/horse-browser.json")"
grok_run "$WORK/gstamp2"
[ "$(cat "$GH/horse-browser.json")" = "$before" ] \
  && pass "…and leaves it untouched when it is already correct" \
  || fail "grok hook rewritten without drift"

say ""
say "── $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
