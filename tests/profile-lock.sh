#!/usr/bin/env bash
# tests/profile-lock.sh — profile_pids: who is holding the browser profile?
#
# Two places kill on this answer: the heal path (quit this profile's browser) and the
# free-profile gate that must not seed Preferences while a process still holds the dir —
# Chrome writes Preferences back FROM MEMORY on exit, so a seed written then is lost.
#
# It used to answer with a full process-table scan for `--user-data-dir=$PROFILE`, which
# matches ANY command line mentioning the path. A false hit there ends in `kill -9` on an
# innocent process. Chrome's own SingletonLock names the real owner in one readlink, so we
# ask that first — and, because a lock names a pid that may since have been recycled,
# confirm it is really a browser on this profile before trusting it.
#
# Deterministic + browser-free: sources the real profile_pids from the launcher and runs it
# against fixture processes.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
HB="$HERE/../bin/horse-browser"
PASS=0; FAIL=0; FAILED=()
pass() { PASS=$((PASS+1)); echo "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); FAILED+=("$1"); echo "  ✗ $1${2:+ — $2}"; }

echo "horse-browser profile-lock — who holds the profile"

FN="$(mktemp)"; T="$(mktemp -d)"
KIDS=()
cleanup() { for k in ${KIDS+"${KIDS[@]}"}; do kill "$k" 2>/dev/null || true; done; rm -f "$FN"; rm -rf "$T"; }
trap cleanup EXIT
sed -n '/^profile_pids() {$/,/^}$/p' "$HB" > "$FN"
if [ ! -s "$FN" ]; then fail "extract profile_pids from the launcher" "not found"; echo "── 0 passed, 1 failed"; exit 1; fi
. "$FN"

PROFILE="$T/profile"; mkdir -p "$PROFILE"

# A stand-in browser: a live process carrying --user-data-dir=$PROFILE in its argv, exactly
# as Chrome does. `bash -c <script> <argv0> <args…>` puts the extras in argv, so ps sees them.
# Sets FAKE_PID. Must run in THIS shell, not a $(…) subshell — the pid has to reach KIDS
# so the trap can reap it. stdout is detached so it never holds a pipe open either.
spawn_fake() {
  bash -c 'while :; do sleep 1; done' "$1" "--user-data-dir=$PROFILE" >/dev/null 2>&1 &
  FAKE_PID=$!; KIDS+=("$FAKE_PID")
  disown "$FAKE_PID" 2>/dev/null || true    # keep job control from printing "Terminated"
}

echo "[1] the lock is the authority"
spawn_fake fake-chrome;     BROWSER=$FAKE_PID
spawn_fake some-other-tool; DECOY=$FAKE_PID   # also mentions the profile — the old scan's false hit
ln -sf "$(hostname)-$BROWSER" "$PROFILE/SingletonLock"
sleep 0.3
GOT=$(profile_pids | tr '\n' ' ' | xargs || true)
[ "$GOT" = "$BROWSER" ] \
  && pass "returns only the lock's owner, not every process mentioning the profile" \
  || fail "returns only the lock's owner" "got [$GOT], wanted [$BROWSER] (decoy $DECOY)"

echo "[2] a lock is not trusted blindly"
# pid recycled: the lock names a live pid that is NOT a browser on this profile. Trusting it
# would hand a stranger's pid to `kill -9`.
ln -sf "$(hostname)-$$" "$PROFILE/SingletonLock"          # this shell — live, but not a browser
GOT=$(profile_pids | tr '\n' ' ' | xargs || true)
case " $GOT " in
  *" $$ "*) fail "rejects a recycled pid" "returned this shell ($$) — kill -9 on a stranger" ;;
  *)        pass "rejects a recycled pid (lock owner is not a browser on this profile)" ;;
esac
# …and having rejected it, it still finds the real ones by scanning.
case " $GOT " in
  *" $BROWSER "*) pass "falls back to the scan when the lock is untrustworthy" ;;
  *)              fail "falls back to the scan" "got [$GOT], expected $BROWSER among them" ;;
esac

echo "[3] a dead lock"
ln -sf "$(hostname)-999999" "$PROFILE/SingletonLock"      # > macOS pid_max ⇒ never alive
GOT=$(profile_pids | tr '\n' ' ' | xargs || true)
case " $GOT " in
  *" $BROWSER "*) pass "stale lock (dead pid) falls back to the scan" ;;
  *)              fail "stale lock falls back to the scan" "got [$GOT]" ;;
esac

echo "[4] a free profile"
rm -f "$PROFILE/SingletonLock"
for k in ${KIDS+"${KIDS[@]}"}; do kill "$k" 2>/dev/null || true; done
KIDS=(); sleep 0.5
GOT=$(profile_pids | tr '\n' ' ' | xargs || true)
[ -z "$GOT" ] \
  && pass "no lock and no browser → nobody holds it (the gate opens)" \
  || fail "free profile reports nobody" "got [$GOT]"

echo
if [ "$FAIL" -eq 0 ]; then echo "── $PASS passed, 0 failed"; else
  echo "── $PASS passed, $FAIL failed"; for f in "${FAILED[@]}"; do echo "   ✗ $f"; done; fi
exit $((FAIL > 0))
