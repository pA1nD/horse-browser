#!/usr/bin/env bash
# tests/e2e.sh — end-to-end suite for horse-browser against the REAL browser.
#
# Covers every failure class that has actually bitten (git log + shipmate build log):
# launch races, same-bundle collisions, stale locks, tab-preserving relaunch, daemon
# recycle/reap/anchoring, stdin modes, per-session identity, focus-safe tab grouping,
# helper shadowing, trusted input on hydrating SPAs, concurrent screenshots, inner
# scroll containers, real-site flows.
#
# Usage:
#   tests/e2e.sh                   run everything (last section bounces the browser;
#                                  open tabs are preserved — same path `update` uses)
#   HB_TEST_FAST=1 tests/e2e.sh    skip the browser-bouncing lifecycle section
#
# NOT covered (machine-state dependent, verify manually):
#   GPU-wedge self-heal, software-GL fallback backoff, display-asleep/clamshell rules.
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HB="$HERE/../bin/horse-browser"
PORT="$(sed -n 's/^PORT=//p' "$HOME/.config/horse-browser/config" 2>/dev/null | head -1 | tr -d '"' )"
PORT="${PORT:-9223}"
WORK="$(mktemp -d /tmp/hb-e2e.XXXXXX)"
LOCK="$HOME/.config/horse-browser/.browser-lock"
PASS=0; FAIL=0; SKIP=0; FAILED=()

say()  { printf '%s\n' "$*"; }
pass() { PASS=$((PASS+1)); say "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); FAILED+=("$1"); say "  ✗ $1${2:+ — $2}"; }
skip() { SKIP=$((SKIP+1)); say "  - $1 (skipped${2:+: $2})"; }

hb() { "$HB" <<<"$1" 2>&1; }        # run a driver script, capture stdout+stderr

cleanup() {
  hb '
for t in bh_list():
    if t["targetId"]:
        try: cdp("Target.closeTarget", targetId=t["targetId"])
        except Exception: pass
' >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT

cdp_up()    { curl -s --max-time 2 "http://127.0.0.1:$PORT/json/version" >/dev/null 2>&1; }
frontmost() { osascript -e 'tell application "System Events" to get name of first process whose frontmost is true' 2>/dev/null; }

# ─────────────────────────────────────────────────────────────────────────────
say "horse-browser e2e — $(date '+%Y-%m-%d %H:%M') — port $PORT"
"$HB" >/dev/null 2>&1 || { say "FATAL: horse-browser could not bring the browser up"; exit 1; }

# ═════ 1. launcher basics: status + every stdin mode ═════════════════════════
say "[1] launcher basics"

out="$("$HB" status 2>&1)"
grep -q "chrome (running)" <<<"$out" && grep -q "live on :$PORT" <<<"$out" \
  && pass "status reports a live browser" || fail "status reports a live browser" "$out"

t0=$(date +%s); "$HB" >/dev/null 2>&1; t1=$(date +%s)
[ $((t1 - t0)) -le 3 ] && pass "hot no-op is fast ($((t1-t0))s)" \
  || fail "hot no-op is fast" "took $((t1-t0))s"

out="$(hb 'print("HEREDOC-OK")')"
grep -q "HEREDOC-OK" <<<"$out" && pass "heredoc stdin runs" || fail "heredoc stdin runs" "$out"

echo 'print("FILE-OK")' > "$WORK/s.py"
out="$("$HB" < "$WORK/s.py" 2>&1)"
grep -q "FILE-OK" <<<"$out" && pass "redirected-file stdin runs" || fail "redirected-file stdin runs" "$out"

out="$(echo 'print("PIPE-OK")' | "$HB" 2>&1)"
grep -q "PIPE-OK" <<<"$out" && pass "shell-pipe stdin runs" || fail "shell-pipe stdin runs" "$out"

# Node stdio pipes are SOCKETPAIRS, not FIFOs — silently dropped before the select() peek fix.
if command -v node >/dev/null 2>&1; then
  out="$(node -e '
const {spawnSync} = require("child_process");
const r = spawnSync(process.argv[1], [], {input: "print(\"NODE-OK\")", encoding: "utf8"});
process.stdout.write((r.stdout||"") + (r.stderr||""));' "$HB" 2>&1)"
  grep -q "NODE-OK" <<<"$out" && pass "node socketpair stdin runs (socketpair regression)" \
    || fail "node socketpair stdin runs (socketpair regression)" "$out"
else
  skip "node socketpair stdin" "node not installed"
fi

# ═════ 2. session identity: daemon pinning, anchor, reaper restraint ═════════
say "[2] session identity"

hb 'page_info()' >/dev/null 2>&1     # ensure our session daemon exists
DPID=""
for p in $(pgrep -f browser_harness.daemon); do
  env="$(ps eww -o command= -p "$p" 2>/dev/null)"
  case "$env" in *"BU_CDP_URL=http://127.0.0.1:$PORT"*) ;; *) continue ;; esac
  case "$env" in *"BU_NAME=hb-"*) DPID="$p" ;; esac
done
[ -n "$DPID" ] && pass "per-session daemon exists (BU_NAME=hb-*, pinned :$PORT)" \
  || fail "per-session daemon exists" "no daemon with BU_NAME=hb-* and BU_CDP_URL=:$PORT"

if [ -n "$DPID" ]; then
  anchor="$(ps eww -o command= -p "$DPID" | tr ' ' '\n' | sed -n 's/^BH_ANCHOR_PID=//p' | head -1)"
  if [ -z "$anchor" ]; then
    skip "daemon anchor is a real claude" "no anchor (not run under claude?)"
  else
    comm="$(ps -o comm= -p "$anchor" 2>/dev/null)"
    case "$comm" in
      *bg-pty-host*|*bg-spare*) fail "daemon anchor is a real claude (bg-pty-host regression)" "anchored to '$comm'" ;;
      *claude*)                 pass "daemon anchor is a real claude (pid $anchor)" ;;
      *)                        fail "daemon anchor is a real claude" "anchor $anchor comm='$comm' (dead or foreign)" ;;
    esac
  fi
fi

# Dry-run reap through the REAL launcher path: live-anchored daemons must not be flagged.
out="$(HORSE_BROWSER_REAP_DRYRUN=1 HORSE_BROWSER_REAP_INTERVAL=0 \
       HORSE_BROWSER_REAP_STAMP="$WORK/reap-stamp" "$HB" 2>&1 >/dev/null)"
grep -q "would reap" <<<"$out" \
  && fail "reaper leaves live daemons alone (dry-run)" "$out" \
  || pass "reaper leaves live daemons alone (dry-run)"

# ═════ 3. tabs: grouping, focus safety, the 🐴 mark ══════════════════════════
say "[3] tabs + extension"

FRONT_BEFORE="$(frontmost)"
out="$(hb '
tid = bh_open("data:text/html,<title>hb-e2e-one</title><h1>one</h1>")
wait_for_load()
print("MINE", any(t["targetId"] == tid for t in bh_list()))
')"
grep -q "MINE True" <<<"$out" && pass "bh_open lands in this session's group" \
  || fail "bh_open lands in this session's group" "$out"

FRONT_AFTER="$(frontmost)"
if [ -n "$FRONT_BEFORE" ] && [ -n "$FRONT_AFTER" ]; then
  [ "$FRONT_BEFORE" = "$FRONT_AFTER" ] && pass "no macOS focus steal (frontmost: $FRONT_AFTER)" \
    || fail "no macOS focus steal" "frontmost changed: $FRONT_BEFORE → $FRONT_AFTER"
else
  skip "no macOS focus steal" "frontmost not readable"
fi

out="$(hb '
tabs = bh_list()
bh_switch_tab(tabs[0]["targetId"])
print("TITLE", page_info().get("title", ""))
')"
grep -q "TITLE 🐴" <<<"$out" && pass "🐴 active-work mark on the driven tab" \
  || fail "🐴 active-work mark on the driven tab" "$out"

hb 'cdp("Target.closeTarget", targetId=bh_list()[0]["targetId"])' >/dev/null 2>&1

# ═════ 4. helper namespace (shadowing regressions) ═══════════════════════════
say "[4] helper namespace"

out="$(hb '
fn = type_into
for cell in (fn.__closure__ or []):
    cc = cell.cell_contents
    if callable(cc) and getattr(cc, "__name__", "") == "type_into":
        fn = cc
        break
g = fn.__globals__
bad = []
for name in ("_focus", "_eval", "_center", "_keyinfo", "_key"):
    f = g.get(name)
    src = getattr(getattr(f, "__code__", None), "co_filename", "MISSING")
    if not src.endswith("horse_input.py"):
        bad.append(name + "<-" + src)
print("SHADOWED", ",".join(bad) if bad else "none")
missing = [n for n in ("bh_open", "bh_list", "bh_switch_tab", "click", "type_into",
                       "press", "press_hold", "drag", "solve_challenge",
                       "challenge_cleared") if n not in globals()]
print("MISSING", ",".join(missing) if missing else "none")
')"
grep -q "SHADOWED none" <<<"$out" && grep -q "MISSING none" <<<"$out" \
  && pass "input privates resolve from horse_input.py; all verbs present" \
  || fail "input privates resolve from horse_input.py" "$out"

# ═════ 5. trusted input mechanics (local pages, deterministic) ═══════════════
say "[5] trusted input mechanics"

out="$(hb '
import urllib.parse
html = """<title>hb-e2e-form</title>
<input id=t placeholder=type-here>
<button id=b disabled>go</button>
<div id=log></div>
<script>
  const t = document.getElementById("t"), b = document.getElementById("b");
  t.addEventListener("input", () => { b.disabled = t.value.length === 0; });
  t.addEventListener("keyup", () => log.dataset.keyup = "1");
  b.addEventListener("click", () => log.textContent = "CLICKED:" + t.value);
</script>"""
tid = bh_open("data:text/html," + urllib.parse.quote(html))
wait_for_load()
type_into("#t", "Horse!")
print("ENABLED", js("!document.getElementById(\"b\").disabled"))
print("KEYUP", js("document.getElementById(\"log\").dataset.keyup === \"1\""))
click("#b")
print("RESULT", js("document.getElementById(\"log\").textContent"))
cdp("Target.closeTarget", targetId=tid)
')"
grep -q "ENABLED True" <<<"$out" && grep -q "KEYUP True" <<<"$out" \
  && grep -q "RESULT CLICKED:Horse!" <<<"$out" \
  && pass "type_into/click fire real events (listeners ran, button enabled)" \
  || fail "type_into/click fire real events" "$out"

# Inner overflow container (the "can't screenshot below the fold" report):
# window scroll no-ops there; wheel-at-coords must scroll the inner container.
out="$(hb '
import time, urllib.parse
html = """<title>hb-e2e-scroll</title><style>
  body{margin:0} .pin{height:100vh;overflow:hidden}
  .inner{height:100%;overflow-y:auto} .pad{height:3000px}
</style><div class=pin><div class=inner id=sc><div class=pad>
<div id=deep style=margin-top:2500px>DEEP</div></div></div></div>"""
tid = bh_open("data:text/html," + urllib.parse.quote(html))
wait_for_load()
for _ in range(8):
    cdp("Input.dispatchMouseEvent", type="mouseWheel", x=300, y=300, deltaX=0, deltaY=300)
    time.sleep(0.05)
time.sleep(0.3)
print("WINSCROLL", js("window.scrollY"))
print("INNERSCROLL", js("document.getElementById(\"sc\").scrollTop > 1000"))
cdp("Target.closeTarget", targetId=tid)
')"
grep -q "INNERSCROLL True" <<<"$out" && pass "wheel-at-coords scrolls inner overflow container" \
  || fail "wheel-at-coords scrolls inner overflow container" "$out"

# ═════ 6. screenshots: validity, per-session names, parallel correctness ═════
say "[6] screenshots"

out="$(hb '
import urllib.parse
tid = bh_open("data:text/html," + urllib.parse.quote(
  "<title>hb-e2e-red</title><body style=background:#f00;margin:0>"))
wait_for_load()
print("SHOT", capture_screenshot())
cdp("Target.closeTarget", targetId=tid)
')"
shot="$(sed -n 's/^SHOT //p' <<<"$out" | head -1)"
if [ -n "$shot" ] && [ -s "$shot" ] && file "$shot" | grep -q "PNG image"; then
  case "$(basename "$shot")" in
    shot-hb-*) pass "screenshot valid PNG with per-session filename" ;;
    *)         fail "screenshot has per-session filename (shot.png race)" "$(basename "$shot")" ;;
  esac
else
  fail "screenshot valid PNG" "$out"
fi

# Two LANES capture in parallel — distinct files, each showing its OWN page.
lane_shot() {  # $1 lane  $2 color-name  $3 hex
  "$HB" --lane "$1" <<PY 2>&1
import urllib.parse
tid = bh_open("data:text/html," + urllib.parse.quote(
  "<title>hb-e2e-$2</title><body style=background:#$3;margin:0>"))
wait_for_load()
print("SHOT-$2", capture_screenshot())
cdp("Target.closeTarget", targetId=tid)
PY
}
lane_shot r red f00 > "$WORK/r.out" 2>&1 &
lane_shot b blue 00f > "$WORK/b.out" 2>&1 &
wait
rshot="$(sed -n 's/^SHOT-red //p' "$WORK/r.out" | head -1)"
bshot="$(sed -n 's/^SHOT-blue //p' "$WORK/b.out" | head -1)"
if [ -n "$rshot" ] && [ -n "$bshot" ] && [ "$rshot" != "$bshot" ]; then
  colors="$(python3 - "$rshot" "$bshot" <<'PY' 2>/dev/null
import sys
try:
    from PIL import Image
except Exception:
    print("nopil"); raise SystemExit
def mid(p):
    im = Image.open(p).convert("RGB"); w, h = im.size
    return im.getpixel((w // 2, h // 2))
a, b = mid(sys.argv[1]), mid(sys.argv[2])
print(("red" if a[0] > 150 and a[2] < 100 else "other:%r" % (a,)),
      ("blue" if b[2] > 150 and b[0] < 100 else "other:%r" % (b,)))
PY
)"
  case "$colors" in
    nopil)      pass "parallel lane screenshots: distinct files (pixel check needs PIL)" ;;
    "red blue") pass "parallel lane screenshots show each lane's own page" ;;
    *)          fail "parallel lane screenshots show each lane's own page" "center pixels: $colors" ;;
  esac
else
  fail "parallel lane screenshots produce distinct files" "r='$rshot' b='$bshot'"
fi

# ═════ 7. real websites ══════════════════════════════════════════════════════
say "[7] real websites"

out="$(hb '
tid = bh_open("https://news.ycombinator.com")
wait_for_load()
print("HN", "Hacker News" in page_info().get("title",""),
      js("document.querySelectorAll(\".athing\").length"))
cdp("Target.closeTarget", targetId=tid)
')"
grep -q "HN True" <<<"$out" && pass "HN loads with a story list ($(sed -n 's/^HN True //p' <<<"$out" | head -1) stories)" \
  || fail "HN loads" "$out"

out="$(hb '
import time
tid = bh_open("https://duckduckgo.com")
wait_for_load()
for _ in range(20):                     # SPA hydration: the box appears late
    if js("!!document.querySelector(\"#searchbox_input\")"): break
    time.sleep(0.5)
type_into("#searchbox_input", "horse browser e2e", enter=True)
wait_for_load()
for _ in range(20):
    if js("document.querySelectorAll(\"article\").length") or 0: break
    time.sleep(0.5)
print("DDG-URL-OK", "q=horse+browser+e2e" in page_info().get("url",""))
print("DDG-RESULTS", js("document.querySelectorAll(\"article\").length"))
cdp("Target.closeTarget", targetId=tid)
')"
grep -q "DDG-URL-OK True" <<<"$out" && pass "DDG: hydrated SPA typed+submitted (trusted input)" \
  || fail "DDG: hydrated SPA typed+submitted" "$out"

out="$(hb '
import time
tid = bh_open("https://en.wikipedia.org/wiki/Chrome_DevTools_Protocol")
wait_for_load()
time.sleep(1)
h1 = js("(document.querySelector(\"#firstHeading, h1\") || {}).textContent || \"\"") or ""
print("WIKI", len(h1.strip()) > 0, repr(h1.strip()[:40]))
cdp("Target.closeTarget", targetId=tid)
')"
grep -q "WIKI True" <<<"$out" && pass "Wikipedia article loads + parses" \
  || fail "Wikipedia article loads" "$out"

# ═════ 8. lifecycle: relaunch, recycle, stampede, locks (browser bounces) ════
if [ -n "${HB_TEST_FAST:-}" ]; then
  say "[8] lifecycle (SKIPPED: HB_TEST_FAST)"
  skip "cold relaunch after hard kill" "fast mode"
  skip "no dead-websocket daemon after relaunch" "fast mode"
  skip "5-way cold-launch stampede" "fast mode"
  skip "dead-holder stale lock break" "fast mode"
else
  say "[8] lifecycle (bounces the browser)"

  pkill -f "remote-debugging-port=$PORT" 2>/dev/null
  sleep 2
  t0=$(date +%s); "$HB" >/dev/null 2>&1; rc=$?; t1=$(date +%s)
  if [ "$rc" = 0 ] && cdp_up; then
    pass "cold relaunch after hard kill ($((t1-t0))s)"
  else
    fail "cold relaunch after hard kill" "rc=$rc after $((t1-t0))s"
  fi

  # the pre-kill daemon must NOT be left holding a websocket into the dead browser
  out="$(hb 'print("ALIVE", page_info() is not None)')"
  grep -q "ALIVE True" <<<"$out" && pass "no dead-websocket daemon after relaunch (recycle works)" \
    || fail "no dead-websocket daemon after relaunch" "$out"

  pkill -f "remote-debugging-port=$PORT" 2>/dev/null
  sleep 2
  for i in 1 2 3 4 5; do ( "$HB" >/dev/null 2>&1; echo "$?" > "$WORK/rc$i" ) & done
  wait
  rcs=""; for i in 1 2 3 4 5; do rcs="$rcs$(cat "$WORK/rc$i" 2>/dev/null || echo 9)"; done
  # helpers + crashpad inherit the flag in argv — count only the main binary
  nb="$(pgrep -fl "remote-debugging-port=$PORT" | grep -cv "/Frameworks/" || true)"
  if [ "$rcs" = "00000" ] && [ "$nb" = "1" ]; then
    pass "5-way cold-launch stampede → 1 browser, all rc=0"
  else
    fail "5-way cold-launch stampede" "rcs=$rcs browsers=$nb"
  fi
  [ -d "$LOCK" ] && fail "lock released after stampede" "lock dir still present" \
                 || pass "lock released after stampede"

  # A lock left by a DEAD holder must be broken on sight, not waited out.
  # Only matters on the cold path — with CDP up the launcher never touches the lock.
  pkill -f "remote-debugging-port=$PORT" 2>/dev/null
  sleep 2
  sleep 2 & sp=$!; kill -9 "$sp" 2>/dev/null; wait "$sp" 2>/dev/null
  mkdir -p "$LOCK"; echo "$sp" > "$LOCK/pid"
  t0=$(date +%s); "$HB" >/dev/null 2>&1; rc=$?; t1=$(date +%s)
  if [ "$rc" = 0 ] && cdp_up && [ ! -d "$LOCK" ] && [ $((t1-t0)) -le 25 ]; then
    pass "dead-holder stale lock broken on sight, cold launch proceeded ($((t1-t0))s)"
  else
    fail "dead-holder stale lock broken on sight" \
         "rc=$rc t=$((t1-t0))s lock=$([ -d "$LOCK" ] && echo held || echo free)"
    rm -rf "$LOCK"
  fi
fi

# ═════ summary ═══════════════════════════════════════════════════════════════
say ""
say "── $PASS passed, $FAIL failed, $SKIP skipped"
if [ "$FAIL" -gt 0 ]; then
  for f in "${FAILED[@]}"; do say "   FAILED: $f"; done
  exit 1
fi
exit 0
