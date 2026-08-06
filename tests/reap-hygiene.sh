#!/usr/bin/env bash
# tests/reap-hygiene.sh — the per-session runtime-file GC (reap_stale_runtime).
#
# A daemon that dies UNCLEANLY (SIGKILL / crash / killed parent) leaves its runtime file-set
# (pid/sock/lock/port) behind. Session names are UNIQUE, so lifecycle's same-name cleanup never
# fires and the sets pile up forever — the leak that shipped undetected until v0.9.5 (62 files,
# 53 stale, oldest 4 days). This asserts the GC removes a set ONLY when its pid is provably dead,
# and NEVER a live daemon's files or a fresh (mid-spawn) orphan. Deterministic + browser-free:
# it sources the real reap_stale_runtime from the launcher (single source of truth) and runs it
# against a fixture runtime dir — so it catches a regression of the fix in any environment.
set -u

# A test run must never reach the operator's ~/.claude or ~/.grok. 16 of 19 suites once
# lacked this, so `npm test` from ANY clone wired that clone's path into the real global
# settings.json — which is how a build agent's throwaway checkout came to leave a dead
# hook behind that failed every Bash call on the machine. external-state.sh is the one
# suite that unsets this, against temp paths of its own.
export HORSE_BROWSER_NO_RECONCILE=1
HERE="$(cd "$(dirname "$0")" && pwd)"
HB="$HERE/../bin/horse-browser"
PASS=0; FAIL=0; FAILED=()
pass() { PASS=$((PASS+1)); echo "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); FAILED+=("$1"); echo "  ✗ $1${2:+ — $2}"; }

echo "horse-browser reap-hygiene — runtime-file GC"

FN="$(mktemp)"; T="$(mktemp -d)"; trap 'rm -f "$FN"; rm -rf "$T"' EXIT
sed -n '/^reap_stale_runtime() {$/,/^}$/p' "$HB" > "$FN"
if [ ! -s "$FN" ]; then fail "extract reap_stale_runtime from the launcher" "not found"; echo "── 0 passed, 1 failed"; exit 1; fi
. "$FN"

# fixture: a dead set, a live set (this shell), an aged orphan lock, a fresh orphan lock
echo 999999 > "$T/bu-dead.pid"; : > "$T/bu-dead.sock"; : > "$T/bu-dead.lock"   # pid > macOS pid_max ⇒ never alive
echo "$$"    > "$T/bu-live.pid"; : > "$T/bu-live.sock"
: > "$T/bu-oldorphan.lock"; touch -t 202601010000 "$T/bu-oldorphan.lock"
: > "$T/bu-freshorphan.lock"

BH_RUNTIME_DIR="$T" reap_stale_runtime >/dev/null 2>&1

[ ! -e "$T/bu-dead.pid" ] && [ ! -e "$T/bu-dead.sock" ] && [ ! -e "$T/bu-dead.lock" ] \
  && pass "dead daemon's pid/sock/lock set removed" \
  || fail "dead daemon's set removed" "files remain — leak not GC'd"
[ -e "$T/bu-live.pid" ] && [ -e "$T/bu-live.sock" ] \
  && pass "live daemon's files kept (kill -0 succeeds)" \
  || fail "live daemon's files kept" "GC DELETED A LIVE DAEMON'S FILES — dangerous"
[ ! -e "$T/bu-oldorphan.lock" ] \
  && pass "aged orphan lock (no .pid) swept" \
  || fail "aged orphan lock swept"
[ -e "$T/bu-freshorphan.lock" ] \
  && pass "fresh orphan kept (never races a mid-spawn daemon)" \
  || fail "fresh orphan kept" "GC deleted a mid-spawn file — could break a spawning daemon"

echo ""; echo "── $PASS passed, $FAIL failed"
[ "$FAIL" = 0 ]; exit $?
