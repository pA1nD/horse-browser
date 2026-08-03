#!/usr/bin/env bash
# tests/install-chrome.sh — the build-time subcommand, and the stdin rule it exposed.
#
# `horse-browser install-chrome --prefix DIR` is what an image build runs: horse-browser
# owns WHICH Chrome and WHICH flags (--remote-allow-origins decides whether CDP is reachable
# at all; the backgrounding trio decides whether screenshots survive load), while the bytes
# live in the image so a locked-down pod needs no network and no package manager.
#
# It must therefore be inert at build time: no browser, no daemon, no display, no config.
#
# And it must not eat stdin. The launcher captures stdin early to decide driver-vs-bare mode,
# which meant `horse-browser install-chrome` inside a pipeline swallowed everything after it.
# Found running it as a build step over `ssh host bash -s`, where the session simply stopped.
#
# Deliberately downloads nothing — 290 MB per run is not a unit test.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
HB="$HERE/../bin/horse-browser"
PASS=0; FAIL=0; FAILED=()
pass() { PASS=$((PASS+1)); echo "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); FAILED+=("$1"); echo "  ✗ $1${2:+ — $2}"; }

echo "horse-browser install-chrome — the build-time seam"
T="$(mktemp -d -t hb-icrm.XXXXXX)"; trap 'rm -rf "$T"' EXIT

echo "[1] argument handling"
"$HB" install-chrome --help >/dev/null 2>&1 \
  && pass "--help exits 0" || fail "--help exits 0"
"$HB" install-chrome --help 2>&1 | grep -q -- "--prefix" \
  && pass "--help names --prefix" || fail "--help names --prefix"
"$HB" install-chrome --nonsense >/dev/null 2>&1 \
  && fail "unknown argument exits non-zero" "it exited 0" \
  || pass "unknown argument exits non-zero"
out="$("$HB" install-chrome --prefix "" 2>&1)"; rc=$?
[ "$rc" -ne 0 ] && printf '%s' "$out" | grep -qi "prefix" \
  && pass "empty --prefix refused with a message naming it" \
  || fail "empty --prefix refused" "rc=$rc out=$out"

echo "[2] subcommands must not swallow stdin"
# THE regression: the early stdin capture ran before dispatch, so a subcommand in a pipeline
# consumed the rest of the stream. Assert the bytes after the call are still readable.
got="$(printf 'SURVIVED\n' | { "$HB" install-chrome --help >/dev/null 2>&1; cat; })"
[ "$got" = "SURVIVED" ] \
  && pass "install-chrome leaves stdin for the next reader" \
  || fail "install-chrome leaves stdin alone" "downstream read [$got]"
# Enumerated from the launcher's own dispatch, not hand-listed. The hand-listed version
# checked three of nine and missed `harness-setup` and `rule`, which were consuming stdin —
# a list that has to be kept in step with another list will drift.
subs="$(sed -n '/^case "\${1:-}" in/,/^esac/p' "$HB" | grep -oE '^  [a-z-]+\)' | tr -d ' )')"
[ -n "$subs" ] && pass "enumerated the dispatch cases from the launcher ($(wc -w <<<"$subs" | tr -d ' ') found)" \
  || fail "enumerate the dispatch cases" "the case block did not parse"
# The two lists must agree. When they drifted, the only symptom was a subcommand quietly
# eating stdin again — a long way from the cause. Name it instead.
guard="$(sed -n 's/^_SUBCOMMANDS="\(.*\)"$/\1/p' "$HB")"
missing=""
for sub in $subs; do
  grep -qw -- "$sub" <<<"$guard" || missing="$missing $sub"
done
[ -z "$missing" ] \
  && pass "every dispatch case is in the stdin guard's list" \
  || fail "dispatch and stdin guard agree" "not in _SUBCOMMANDS:$missing"

for sub in $subs; do
  case "$sub" in focus-watch|update|relaunch) continue ;; esac   # these do real work / need a browser
  got="$(printf 'SURVIVED\n' | { "$HB" "$sub" --help >/dev/null 2>&1; cat; })"
  [ "$got" = "SURVIVED" ] \
    && pass "$sub leaves stdin for the next reader" \
    || fail "$sub leaves stdin alone" "downstream read [$got]"
done

echo "[3] inert at build time"
# A build has no display, no browser and no config. Nothing may appear beside the prefix.
HOME="$T/home" HORSE_BROWSER_CACHE="$T/cache" \
  "$HB" install-chrome --help >/dev/null 2>&1
[ ! -e "$T/home/.config/horse-browser/config" ] \
  && pass "writes no config" || fail "writes no config"
[ ! -d "$T/cache/chrome" ] \
  && pass "downloads nothing on the --help path" || fail "downloads nothing on --help"

echo
if [ "$FAIL" -eq 0 ]; then echo "── $PASS passed, 0 failed"; else
  echo "── $PASS passed, $FAIL failed"; for f in "${FAILED[@]}"; do echo "   ✗ $f"; done; fi
exit $((FAIL > 0))
