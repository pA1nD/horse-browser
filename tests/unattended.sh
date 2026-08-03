#!/usr/bin/env bash
# tests/unattended.sh — the browser nobody is going to look at.
#
# The axis is NOT how many agents share a browser: a pod can run a hundred subagent lanes
# against one, and that is the most multi-tenant thing in the system. The axis is whether a
# human will ever WATCH. When none will, the extension earns nothing — coloured tab groups,
# the pinned Monitor and the CCTV wall are all for eyes — and it costs a service worker, a
# tab, and an extension round trip on every tab minted.
#
# What must survive it, because it is what the browser is FOR:
#   • the verbs, all of them, unchanged
#   • tab ownership — the registry is the truth, tab groups only ever rendered it
#   • trusted input
#   • screenshots, per-target and over plain CDP — so a remote viewer built later can watch a
#     browser that was never launched to be watched
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$HERE/.."; HB="$ROOT/bin/horse-browser"
PASS=0; FAIL=0; FAILED=()
pass() { PASS=$((PASS+1)); echo "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); FAILED+=("$1"); echo "  ✗ $1${2:+ — $2}"; }

echo "horse-browser unattended — no extension, everything else intact"
WORK="$(mktemp -d -t hb-unatt.XXXXXX)"
PORT="$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')"
SESS="unatt-$$"
cleanup() {
  for p in "$WORK/profile" "$WORK/watched"; do
    pids=$(ps -ww -o pid= -o command= -ax 2>/dev/null | grep -F -- "--user-data-dir=$p" \
             | grep -v grep | awk '{print $1}') || true
    [ -n "${pids:-}" ] && kill $pids 2>/dev/null
  done
  sleep 1
  rm -f "$HOME/.config/horse-browser/tabs/hb-$SESS"* "$HOME/.config/horse-browser/current/hb-$SESS"* 2>/dev/null
  rm -rf "$WORK"
}
trap cleanup EXIT

run() {  # run <profile> <unattended?> <script>
  HORSE_BROWSER_PORT="$PORT" HORSE_BROWSER_PROFILE="$1" \
  HORSE_BROWSER_UNATTENDED="$2" HORSE_BROWSER_NO_LANE_HOOK=1 \
  HORSE_SESSION="$SESS" BH_ANCHOR_PID=$$ "$HB" <<<"$3" 2>&1
}
targets() { curl -s -m 3 "http://127.0.0.1:$PORT/json/list" 2>/dev/null; }

echo "[1] the extension is not loaded"
out="$(run "$WORK/profile" 1 'print("URL", page_info()["url"])')"
grep -q "^URL " <<<"$out" || { fail "unattended browser came up" "$out"; echo "── 0 passed, 1 failed"; exit 1; }
pass "unattended browser came up and answered a verb"

argv="$(ps -ww -o command= -ax 2>/dev/null | grep -F -- "--user-data-dir=$WORK/profile" | grep -v grep | head -1)"
grep -q -- "--load-extension" <<<"$argv" \
  && fail "no --load-extension in argv" "it was passed anyway" \
  || pass "no --load-extension in argv"

sw="$(targets | python3 -c 'import json,sys; print(len([t for t in json.load(sys.stdin) if t.get("type")=="service_worker" and t.get("url","").startswith("chrome-extension://")]))' 2>/dev/null || echo "?")"
# Not "zero workers of any kind": every Chrome runs Google's own component extension. On a
# profile launched with no --load-extension there is simply nothing of OURS to find, and the
# monitor page is the visible proof.
mon="$(targets | python3 -c 'import json,sys; print(sum(1 for t in json.load(sys.stdin) if "monitor.html" in t.get("url","")))' 2>/dev/null || echo "?")"
[ "$mon" = "0" ] && pass "no Monitor tab (nothing pinned, nothing to resurrect)" \
                 || fail "no Monitor tab" "found $mon (service workers: $sw)"

echo "[2] everything the browser is FOR still works"
out="$(run "$WORK/profile" 1 '
tid = open_tab("https://example.com"); wait_for_load()
print("TABS", len(list_tabs()))
print("EXT", ext_call("groupTab", tid, "x"))
goto_url("data:text/html,<button id=b onclick=\"o.textContent=event.isTrusted\">go</button><div id=o>x</div>")
wait_for_load(); click("#b")
print("TRUSTED", js("document.getElementById(\"o\").textContent"))
import os; print("SHOT", os.path.getsize(capture_screenshot()))
')"
[ "$(sed -n 's/^TABS //p' <<<"$out")" -ge 1 ] 2>/dev/null \
  && pass "list_tabs answers from the registry, with no tab group to read" \
  || fail "list_tabs answers from the registry" "$out"
[ "$(sed -n 's/^EXT //p' <<<"$out")" = "None" ] \
  && pass "ext_call returns None instead of hunting for a worker that isn't there" \
  || fail "ext_call returns None" "$out"
[ "$(sed -n 's/^TRUSTED //p' <<<"$out")" = "true" ] \
  && pass "input is still trusted (isTrusted true)" \
  || fail "input is still trusted" "$out"
{ [ -n "$(sed -n 's/^SHOT //p' <<<"$out")" ] && [ "$(sed -n 's/^SHOT //p' <<<"$out")" -gt 1000 ]; } 2>/dev/null \
  && pass "screenshots work — a remote viewer could watch this browser later" \
  || fail "screenshots work unattended" "$out"

echo "[3] status says which mode this is"
out="$(HORSE_BROWSER_PORT="$PORT" HORSE_BROWSER_PROFILE="$WORK/profile" \
       HORSE_BROWSER_UNATTENDED=1 "$HB" status 2>&1)"
grep -qi "unattended" <<<"$out" \
  && pass "status reports unattended" \
  || fail "status reports unattended" "$(grep -c . <<<"$out") lines, none matched"
out="$(HORSE_BROWSER_PORT="$PORT" HORSE_BROWSER_PROFILE="$WORK/profile" "$HB" status 2>&1)"
grep -qi "unattended" <<<"$out" \
  && fail "status stays quiet when watched" "it claimed unattended" \
  || pass "status stays quiet when watched"

echo
if [ "$FAIL" -eq 0 ]; then echo "── $PASS passed, 0 failed"; else
  echo "── $PASS passed, $FAIL failed"; for f in "${FAILED[@]}"; do echo "   ✗ $f"; done; fi
exit $((FAIL > 0))
