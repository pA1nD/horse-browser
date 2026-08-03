#!/usr/bin/env bash
# tests/platform-layer.sh — the few things that genuinely differ between macOS and Linux.
#
# horse-browser is meant to be the SAME command on a laptop and in a pod — that is what lets a
# skill, a domain-skills/ file, or an agent's muscle memory transfer unchanged. Only a handful
# of mechanisms differ, and they are named in one place in the launcher. This asserts each one
# behaves on the OS running the test, so the two platforms cannot quietly drift into two
# products.
#
# Runs on both. Browser-free and fast: it sources the real functions out of the launcher.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
HB="$HERE/../bin/horse-browser"
PASS=0; FAIL=0; SKIP=0; FAILED=()
pass() { PASS=$((PASS+1)); echo "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); FAILED+=("$1"); echo "  ✗ $1${2:+ — $2}"; }
skip() { SKIP=$((SKIP+1)); echo "  ~ $1${2:+ — $2}"; }

OS="$(uname -s)"
echo "horse-browser platform-layer — on $OS"
T="$(mktemp -d -t hb-plat.XXXXXX)"; trap 'rm -rf "$T"' EXIT

# Source the helpers straight out of the launcher — the point is to test the shipped code.
FN="$T/fns"
for f in _hb_stat_mtime _hb_ps_wide require_supported_os refuse_root_browser; do
  # No $ anchor after the brace: two of these carry a trailing comment on the same line.
  sed -n "/^$f() {/,/^}\$/p" "$HB" >> "$FN"
done
HB_OS="$OS"; say() { echo "$@"; }
# shellcheck disable=SC1090
. "$FN"
[ "$(grep -c '^}$' "$FN")" -eq 4 ] \
  && pass "all four platform helpers extracted from the launcher" \
  || { fail "extract platform helpers" "got $(grep -c '^}$' "$FN")/4"; echo "── 0 passed, 1 failed"; exit 1; }

echo "[1] stat(1) — BSD -f vs GNU -c"
touch "$T/f"
m="$(_hb_stat_mtime "$T/f")"
{ [ -n "$m" ] && [ "$m" -gt 1700000000 ] 2>/dev/null; } \
  && pass "_hb_stat_mtime returns a plausible epoch ($m)" \
  || fail "_hb_stat_mtime returns an epoch" "got [$m] — the lock's staleness check reads this"
[ "$(_hb_stat_mtime "$T/does-not-exist")" = "0" ] \
  && pass "_hb_stat_mtime is 0 for a missing file (never blank)" \
  || fail "_hb_stat_mtime is 0 for a missing file" "blank would make the age arithmetic explode"

echo "[2] ps(1) — BSD -ax vs GNU -e"
out="$(_hb_ps_wide)"
grep -qE "^ *$$ " <<<"$out" \
  && pass "_hb_ps_wide lists this very shell with its pid" \
  || fail "_hb_ps_wide lists this shell" "profile_pids and the GPU heal both read this"
grep -qE "^ *[0-9]+ +[^ ]" <<<"$out" \
  && pass "_hb_ps_wide emits 'pid argv' with no header row" \
  || fail "_hb_ps_wide shape is 'pid argv'" "awk '{print \$1}' would take a header as a pid"

echo "[3] supported OS"
require_supported_os >/dev/null 2>&1 \
  && pass "$OS is supported" || fail "$OS is supported"
( HB_OS="Plan9"; require_supported_os >/dev/null 2>&1 ) \
  && fail "an unknown OS is refused" "Plan9 was accepted" \
  || pass "an unknown OS is refused"
( HB_OS="Plan9"; HORSE_BROWSER_ALLOW_UNSUPPORTED_OS=1 require_supported_os >/dev/null 2>&1 ) \
  && pass "…and the escape hatch still opens it" \
  || fail "escape hatch opens an unknown OS"

echo "[4] never --no-sandbox: Chrome must not run as root on Linux"
# Chrome disables its own sandbox rather than run as uid 0. Rootless podman with nested userns
# keeps that sandbox intact, so the answer is to drop privileges — never to pass --no-sandbox.
# The invariant is narrow and worth stating precisely: the flag must never reach Chrome's
# argv. It DOES appear in prose — a comment and the refusal message both name it to explain
# why it is not used — so grepping the whole file only proves someone wrote about it. Assert
# on the launch args instead, which is the thing that would actually disable the sandbox.
grep -E '^[[:space:]]*args(\+)?=\(' -A 20 "$HB" | grep -q -- "--no-sandbox" \
  && fail "--no-sandbox never reaches the launch args" "it is in the args array" \
  || pass "--no-sandbox never reaches the launch args"
if [ "$OS" = "Linux" ] && [ "$(id -u)" = "0" ]; then
  refuse_root_browser >/dev/null 2>&1 \
    && fail "root is refused on Linux" "it allowed uid 0" \
    || pass "root is refused on Linux"
elif [ "$OS" = "Linux" ]; then
  refuse_root_browser >/dev/null 2>&1 \
    && pass "a non-root uid is allowed on Linux" || fail "non-root allowed on Linux"
else
  ( HB_OS="Darwin"; refuse_root_browser >/dev/null 2>&1 ) \
    && pass "the root check is Linux-only (macOS unaffected)" \
    || fail "root check is Linux-only"
fi

echo "[5] the Linux launch flags are decided here, not by an image"
# --enable-unsafe-swiftshader pairs with the WebGL mask in extension/realchrome.js: without
# the flag there is no WebGL at all (a louder tell than a software renderer), and without the
# mask the renderer string names SwiftShader. Neither half works alone, so both live here.
grep -q -- "--enable-unsafe-swiftshader" "$HB" \
  && pass "launcher owns --enable-unsafe-swiftshader" \
  || fail "launcher owns --enable-unsafe-swiftshader"
grep -q "SOFTWARE" "$HERE/../extension/realchrome.js" \
  && pass "…and realchrome.js carries the matching renderer mask" \
  || fail "realchrome.js carries the renderer mask"

echo
if [ "$FAIL" -eq 0 ]; then echo "── $PASS passed, 0 failed${SKIP:+, $SKIP skipped}"; else
  echo "── $PASS passed, $FAIL failed"; for f in "${FAILED[@]}"; do echo "   ✗ $f"; done; fi
exit $((FAIL > 0))
