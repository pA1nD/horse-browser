#!/usr/bin/env bash
# tests/first-run.sh — a machine that has never run horse-browser.
#
# THE gap this closes: every other suite runs against a $HOME where
# ~/.config/horse-browser/ has existed since install.sh created it, and the pod tests all
# passed HORSE_BROWSER_PROFILE/HORSE_BROWSER_CACHE explicitly and pre-created them for
# isolation. So the paths a genuine first run takes — the ones that must CREATE those
# directories — were never executed anywhere.
#
# The bug that hid there: hb_lock's `mkdir "$HB_LOCK"` (no -p) fails with ENOENT on a virgin
# $HOME, and the code read any mkdir failure as "another invocation holds the lock". A fresh
# machine reported contention that did not exist and refused to launch, pointing the reader at
# a process that was never running. An image layer, a copied checkout or a new $HOME all hit it.
#
# Everything here uses a throwaway HOME. Nothing touches the developer's.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
HB="$HERE/../bin/horse-browser"
PASS=0; FAIL=0; FAILED=()
pass() { PASS=$((PASS+1)); echo "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); FAILED+=("$1"); echo "  ✗ $1${2:+ — $2}"; }

echo "horse-browser first-run — a \$HOME that has never seen us"
T="$(mktemp -d -t hb-first.XXXXXX)"; trap 'chmod -R u+w "$T" 2>/dev/null; rm -rf "$T"' EXIT

# Source the lock machinery out of the launcher, exactly as shipped.
FN="$T/fns"
for f in _hb_mkparent _hb_stat_mtime hb_lock hb_unlock; do
  sed -n "/^$f() {/,/^}\$/p" "$HB" >> "$FN"
done
HB_OS="$(uname -s)"; HB_LOCK_DEPTH=0
# shellcheck disable=SC1090
. "$FN"
[ "$(grep -c '^}$' "$FN")" -eq 4 ] || { fail "extract the lock machinery" "$(grep -c '^}$' "$FN")/4"; echo "── 0 passed, 1 failed"; exit 1; }

echo "[1] the lock, on a \$HOME with no config dir"
VIRGIN="$T/home1"; mkdir -p "$VIRGIN"          # a home, but nothing of ours inside it
HB_LOCK="$VIRGIN/.config/horse-browser/.browser-lock"
[ ! -d "$(dirname "$HB_LOCK")" ] && pass "precondition: ~/.config/horse-browser does not exist" \
  || fail "precondition" "the config dir already existed"
if hb_lock; then
  pass "hb_lock creates its parent and takes the lock"
  [ -d "$HB_LOCK" ] && pass "…and the lock directory is really there" || fail "lock dir exists"
  hb_unlock
  [ ! -d "$HB_LOCK" ] && pass "…and hb_unlock removes it" || fail "hb_unlock removes it"
else
  fail "hb_lock takes the lock on a virgin HOME" \
       "this is the bug: ENOENT reported as contention, so a fresh machine refuses to launch"
fi

echo "[2] 'cannot create' must not masquerade as 'someone else has it'"
if [ "$(id -u)" = "0" ]; then
  # root ignores the directory mode (CAP_DAC_OVERRIDE), so there is no unwritable path to
  # build for it. The branch is covered when the suite runs unprivileged — which is the only
  # way the browser ever runs anyway, since we refuse to launch Chrome as root.
  skip_root=1
  echo "  ~ unwritable-path checks skipped as root (nothing is unwritable to uid 0)"
else
  skip_root=
  HB_LOCK_DEPTH=0
  RO="$T/readonly"; mkdir -p "$RO"; chmod 500 "$RO"
  HB_LOCK="$RO/nested/.browser-lock"
  out="$(hb_lock 2>&1)"; rc=$?
  chmod 700 "$RO"
  # rc 2 specifically: callers branch on it to skip the 45s "waiting for the other invocation"
  # loop, which would otherwise bury an accurate error under a wrong one.
  [ "$rc" -eq 2 ] && pass "an uncreatable lock path returns 2 (not 1 = contention)" \
    || fail "an uncreatable lock path returns 2" "rc=$rc"
  grep -qi "cannot create" <<<"$out" \
    && pass "…and says so, naming the directory" \
    || fail "the message distinguishes it from contention" "got: ${out:-<silence>}"
  grep -qi "another invocation" <<<"$out" \
    && fail "…and does not blame a nonexistent process" "it claimed contention" \
    || pass "…and does not blame a nonexistent process"
fi

echo "[3] real contention still reads as contention"
HB_LOCK_DEPTH=0
HB_LOCK="$T/home1/.config/horse-browser/.browser-lock"
mkdir -p "$HB_LOCK"; echo $$ > "$HB_LOCK/pid"          # a LIVE holder: this shell
hb_lock 2>/dev/null \
  && fail "a live holder still blocks" "the lock was taken from under a live holder" \
  || pass "a live holder still blocks (the stale-break did not over-trigger)"
rm -rf "$HB_LOCK"

echo "[4] the launcher itself runs on a virgin HOME"
# status touches config, cache and the lock path; it must not die or invent contention.
V2="$T/home2"; mkdir -p "$V2"
out="$(HOME="$V2" HORSE_BROWSER_CACHE="$V2/.cache/horse-browser" \
       HORSE_BROWSER_PORT=59999 "$HB" status 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && pass "status exits 0 on a virgin HOME" \
  || fail "status exits 0 on a virgin HOME" "rc=$rc: $(head -2 <<<"$out")"
grep -qi "another invocation\|holds the lock" <<<"$out" \
  && fail "status invents no contention" "$out" \
  || pass "status invents no contention"

echo
if [ "$FAIL" -eq 0 ]; then echo "── $PASS passed, 0 failed"; else
  echo "── $PASS passed, $FAIL failed"; for f in "${FAILED[@]}"; do echo "   ✗ $f"; done; fi
exit $((FAIL > 0))
