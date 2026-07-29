#!/usr/bin/env bash
# tests/e2e.sh — end-to-end suite for horse-browser against the REAL browser.
#
# Covers every failure class that has actually bitten (git log + shipmate build log):
# launch races, same-bundle collisions, stale locks, tab-preserving relaunch, daemon
# recycle/reap/anchoring, stdin modes, per-session identity, focus-safe tab grouping,
# helper shadowing, trusted input on hydrating SPAs, concurrent screenshots, inner
# scroll containers, real-site flows, and the realness/anti-bot fingerprint claims from
# the hb-stealth module (webdriver, the "Google Chrome" UA-CH patch, shiftKey coherence).
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
HB="${HB:-$HERE/../bin/horse-browser}"
[ -x "$HB" ] || HB="$(command -v horse-browser || true)"   # npm/manual installs
[ -x "$HB" ] || { echo "FATAL: horse-browser not found (set HB=…)"; exit 1; }
# Run against a DISPOSABLE instance (its own port + profile + lock) so this destructive suite
# never touches the live :9223 browser the operator/agents share. HB_ISOLATE=0 opts out.
source "$HERE/lib/isolate.sh"; hb_isolate || exit 1
# Outside a Claude session (e.g. plain ssh) there is no identity to derive — give
# this run its own, so the per-session daemon/grouping tests still mean something.
[ -z "${CLAUDE_CODE_SESSION_ID:-}${HORSE_SESSION:-}" ] && export HORSE_SESSION="e2e-$(date +%s)"
PORT="${HORSE_BROWSER_PORT:-$(sed -n 's/^PORT=//p' "$HOME/.config/horse-browser/config" 2>/dev/null | head -1 | tr -d '"' )}"
PORT="${PORT:-9223}"
WORK="$(mktemp -d /tmp/hb-e2e.XXXXXX)"
LOCK="${HB_LOCK_PATH:-$HOME/.config/horse-browser/.browser-lock}"
PASS=0; FAIL=0; SKIP=0; FAILED=()

say()  { printf '%s\n' "$*"; }
pass() { PASS=$((PASS+1)); say "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); FAILED+=("$1"); say "  ✗ $1${2:+ — $2}"; }
skip() { SKIP=$((SKIP+1)); say "  - $1 (skipped${2:+: $2})"; }

hb() { "$HB" <<<"$1" 2>&1; }        # run a driver script, capture stdout+stderr

cleanup() {
  hb '
for t in list_tabs():
    if t["targetId"]:
        try: cdp("Target.closeTarget", targetId=t["targetId"])
        except Exception: pass
' >/dev/null 2>&1 || true
  rm -rf "$WORK"
  _hb_isolate_teardown
}
trap cleanup EXIT

cdp_up()    { curl -s --max-time 2 "http://127.0.0.1:$PORT/json/version" >/dev/null 2>&1; }
# frontmost macOS app via lsappinfo — no accessibility permission needed (unlike System Events).
front_app() { lsappinfo info -only name "$(lsappinfo front 2>/dev/null)" 2>/dev/null | sed -n 's/.*"LSDisplayName"="\(.*\)"/\1/p'; }

# ─────────────────────────────────────────────────────────────────────────────
say "horse-browser e2e — $(date '+%Y-%m-%d %H:%M') — port $PORT"
"$HB" >/dev/null 2>&1 || { say "FATAL: horse-browser could not bring the browser up"; exit 1; }

# ═════ 1. launcher basics: status + every stdin mode ═════════════════════════
say "[1] launcher basics"

out="$("$HB" status 2>&1)"
grep -q "chrome (running)" <<<"$out" && grep -q "live on :$PORT" <<<"$out" \
  && pass "status reports a live browser" || fail "status reports a live browser" "$out"

# …and reports a DOWN browser instead of dying silently. It used to: curl fails, pipefail
# hands that to the assignment, set -e kills the command before its first line — so the one
# command you run when things look broken printed nothing and exited 1.
out="$(HORSE_BROWSER_PORT="$(_hb_free_port)" "$HB" status 2>&1)"; rc=$?
[ "$rc" = 0 ] && grep -q "chrome (running)   down" <<<"$out" \
  && pass "status reports a down browser (no silent set -e exit)" \
  || fail "status reports a down browser (no silent set -e exit)" "rc=$rc out=${out:-<empty>}"

# A forced install off macOS must say so, fast — the launcher owns a browser through macOS-only
# mechanisms, and without the guard that surfaces as a 30s CDP timeout. Faked with a uname stub
# on PATH; require_macos is the launcher's only uname caller.
mkdir -p "$WORK/fakeos"
printf '#!/bin/sh\n[ "$1" = "-s" ] && echo Linux || /usr/bin/uname "$@"\n' > "$WORK/fakeos/uname"
chmod +x "$WORK/fakeos/uname"
out="$(PATH="$WORK/fakeos:$PATH" "$HB" </dev/null 2>&1)"; rc=$?
[ "$rc" = 1 ] && grep -q "macOS only" <<<"$out" \
  && pass "non-macOS fails fast with a clear message (not a 30s timeout)" \
  || fail "non-macOS fails fast with a clear message" "rc=$rc out=$(head -1 <<<"$out")"
out="$(PATH="$WORK/fakeos:$PATH" HORSE_BROWSER_ALLOW_UNSUPPORTED_OS=1 "$HB" </dev/null 2>&1)"
grep -q "continuing anyway" <<<"$out" \
  && pass "the unsupported-OS escape hatch still runs" \
  || fail "the unsupported-OS escape hatch still runs" "$(head -1 <<<"$out")"

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
# Derive THIS session's daemon name the same way the launcher does, so stale daemons
# from ended sessions (still matching hb-*) can't be mistaken for ours.
_sid="${HORSE_SESSION:-${CLAUDE_CODE_SESSION_ID:-}}"
_stail="${_sid##*-}"; [ "${#_stail}" -gt 12 ] && _stail="$(printf '%s' "$_stail" | tail -c 12)"
MYNAME="${_stail:+hb-$_stail}"
DPID=""
for p in $(pgrep -f "(browser|horse)_harness.daemon"); do
  env="$(ps eww -o command= -p "$p" 2>/dev/null)"
  case "$env" in *"BU_CDP_URL=http://127.0.0.1:$PORT"*) ;; *) continue ;; esac
  if [ -n "$MYNAME" ]; then
    case " $env " in *" BU_NAME=$MYNAME "*) DPID="$p" ;; esac
  else
    case "$env" in *"BU_NAME=hb-"*) DPID="$p" ;; esac
  fi
done
[ -n "$DPID" ] && pass "per-session daemon exists (BU_NAME=${MYNAME:-hb-*}, pinned :$PORT)" \
  || fail "per-session daemon exists" "no daemon with BU_NAME=${MYNAME:-hb-*} and BU_CDP_URL=:$PORT"

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

# Dry-run reap through the REAL launcher path: OUR daemon (live anchor) must never be
# flagged. Orphans from genuinely-ended sessions (dead anchor) SHOULD be flagged — that's
# the reaper working — so we assert on our own daemon specifically, not on "nothing".
out="$(HORSE_BROWSER_REAP_DRYRUN=1 HORSE_BROWSER_REAP_INTERVAL=0 \
       HORSE_BROWSER_REAP_STAMP="$WORK/reap-stamp" "$HB" 2>&1 >/dev/null)"
mine="$(ps eww -o command= -p "${DPID:-0}" 2>/dev/null | tr ' ' '\n' | sed -n 's/^BU_NAME=//p' | head -1)"
if [ -n "$mine" ] && grep -q "would reap orphan daemon.* $mine " <<<"$out"; then
  fail "reaper leaves THIS session's live daemon alone" "flagged $mine: $out"
else
  pass "reaper leaves THIS session's live daemon alone${mine:+ ($mine)}"
fi

# ═════ 3. tabs: grouping, focus safety, the 🐴 mark ══════════════════════════
say "[3] tabs + extension"

FRONT_BEFORE="$(front_app)"
out="$(hb '
tid = open_tab("data:text/html,<title>hb-e2e-one</title><h1>one</h1>")
wait_for_load()
print("MINE", any(t["targetId"] == tid for t in list_tabs()))
')"
grep -q "MINE True" <<<"$out" && pass "open_tab lands in this session's group" \
  || fail "open_tab lands in this session's group" "$out"

FRONT_AFTER="$(front_app)"
if [ -n "$FRONT_BEFORE" ] && [ -n "$FRONT_AFTER" ]; then
  # Focus-safe = open_tab doesn't CHANGE the frontmost app. Asserting "Chrome is never frontmost"
  # is wrong when Chrome was already frontmost (e.g. an isolated cold-launched test browser) — a
  # steal is Chrome frontmost AFTER when it wasn't BEFORE.
  case "$FRONT_AFTER" in
    *"Chrome for Testing"*)
      case "$FRONT_BEFORE" in
        *"Chrome for Testing"*) pass "no macOS focus steal from open_tab (Chrome already frontmost, unchanged)" ;;
        *) fail "no macOS focus steal from open_tab" "frontmost $FRONT_BEFORE → $FRONT_AFTER" ;;
      esac ;;
    *) pass "no macOS focus steal from open_tab (frontmost: $FRONT_AFTER)" ;;
  esac
else
  skip "no macOS focus steal" "frontmost not readable"
fi

out="$(hb '
tabs = list_tabs()
switch_tab(tabs[0]["targetId"])
print("TITLE", page_info().get("title", ""))
')"
grep -q "TITLE 🐴" <<<"$out" && pass "🐴 active-work mark on the driven tab" \
  || fail "🐴 active-work mark on the driven tab" "$out"

hb 'cdp("Target.closeTarget", targetId=list_tabs()[0]["targetId"])' >/dev/null 2>&1

# Focus safety for the STOCK tab verbs. The vendored harness's open_tab / switch_tab / ensure_real_tab
# call Target.activateTarget → [NSApp activate] → the browser steals OS focus; the horse helpers
# override all three to be focus-safe. Drive each and assert Chrome for Testing never became the
# frontmost app (lsappinfo works without accessibility perms; skip if it can't read a name).
fa0="$(front_app)"
if [ -n "$fa0" ] && command -v lsappinfo >/dev/null 2>&1; then
  hb '
import urllib.parse
t = open_tab("data:text/html," + urllib.parse.quote("<title>hb-e2e-focus</title>x"))
switch_tab(t)
ensure_real_tab()
cdp("Target.closeTarget", targetId=t)
' >/dev/null 2>&1
  fa1="$(front_app)"
  # a steal is Chrome frontmost AFTER when it wasn't BEFORE (fa0); Chrome already-frontmost is fine
  case "$fa1" in
    *"Chrome for Testing"*)
      case "$fa0" in
        *"Chrome for Testing"*) pass "stock open_tab/switch_tab/ensure_real_tab don't steal OS focus (Chrome already frontmost, unchanged)" ;;
        *) fail "stock open_tab/switch_tab/ensure_real_tab don't steal focus" "frontmost $fa0 → $fa1" ;;
      esac ;;
    *) pass "stock open_tab/switch_tab/ensure_real_tab don't steal OS focus (frontmost: $fa1)" ;;
  esac
else
  skip "stock tab verbs don't steal focus" "lsappinfo unavailable"
fi

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
    if not src.endswith("horse_harness/helpers.py"):
        bad.append(name + "<-" + src)
print("SHADOWED", ",".join(bad) if bad else "none")
missing = [n for n in ("open_tab", "list_tabs", "switch_tab", "click", "type_into",
                       "press", "press_hold", "drag", "solve_challenge",
                       "challenge_cleared") if n not in globals()]
print("MISSING", ",".join(missing) if missing else "none")
')"
grep -q "SHADOWED none" <<<"$out" && grep -q "MISSING none" <<<"$out" \
  && pass "input privates resolve from the harness helpers module; all verbs present" \
  || fail "input privates resolve from the harness helpers module" "$out"

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
tid = open_tab("data:text/html," + urllib.parse.quote(html))
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
tid = open_tab("data:text/html," + urllib.parse.quote(html))
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

# The CORE claim: our input is TRUSTED. A recorder page captures e.isTrusted for every
# event our verbs fire; an untrusted el.click() control proves the fixture can tell.
out="$(hb '
import json, urllib.parse
html = """<title>hb-e2e-trust</title>
<input id=t><button id=b>go</button>
<script>
window.rec = {};
["keydown","keyup","input","mousedown","mouseup","click"].forEach(function(ev){
  document.addEventListener(ev, function(e){
    (window.rec[ev] = window.rec[ev] || []).push(e.isTrusted);
  }, true);
});
window.report = function(){
  var o = {};
  Object.keys(window.rec).forEach(function(k){
    o[k] = {n: window.rec[k].length, all: window.rec[k].every(Boolean)};
  });
  return JSON.stringify(o);
};
</script>"""
tid = open_tab("data:text/html," + urllib.parse.quote(html))
wait_for_load()
type_into("#t", "Hi!")
click("#b")
r = json.loads(js("window.report()"))
need = ["keydown", "keyup", "input", "mousedown", "mouseup", "click"]
print("TRUSTED", all(r.get(k, {}).get("n", 0) > 0 and r.get(k, {}).get("all") for k in need), r)
js("window.rec = {}; document.getElementById(\"b\").click()")
r2 = json.loads(js("window.report()"))
print("CONTROL", r2.get("click", {}).get("n", 0) > 0 and r2.get("click", {}).get("all") is False)
cdp("Target.closeTarget", targetId=tid)
')"
grep -q "TRUSTED True" <<<"$out" && grep -q "CONTROL True" <<<"$out" \
  && pass "every input event is isTrusted (untrusted control detected)" \
  || fail "every input event is isTrusted" "$out"

# Press & Hold fixture with real anti-bot semantics: must stay held 1.5s, a metronomic
# move stream (>25 moves) fails, early release fails. press_hold must clear it, and
# solve_challenge(act=False) must classify the page as an easy hold.
out="$(hb '
import urllib.parse
html = """<title>hb-e2e-hold</title><body style=margin:0>
<div id=px-captcha style=width:240px;height:80px;background:#dde>Press &amp; Hold</div>
<div id=st>idle</div>
<script>
var down = 0, moves = 0, timer = null;
var el = document.getElementById("px-captcha"), st = document.getElementById("st");
el.addEventListener("mousedown", function(e){
  if(!e.isTrusted){ st.textContent = "untrusted"; return; }
  down = 1; moves = 0; st.textContent = "holding";
  timer = setTimeout(function(){ st.textContent = "cleared"; down = 0; }, 1500);
});
document.addEventListener("mousemove", function(){
  if(!down) return; moves++;
  if(moves > 25){ clearTimeout(timer); st.textContent = "jitter-fail"; down = 0; }
});
document.addEventListener("mouseup", function(){
  if(down){ clearTimeout(timer); st.textContent = "released-early"; down = 0; }
});
</script>"""
tid = open_tab("data:text/html," + urllib.parse.quote(html))
wait_for_load()
print("DETECT", solve_challenge(act=False))
press_hold("#px-captcha", 2)
print("HOLD", js("document.getElementById(\"st\").textContent"))
cdp("Target.closeTarget", targetId=tid)
')"
grep -q "HOLD cleared" <<<"$out" && pass "press_hold clears a steady-hold fixture (jitter would fail it)" \
  || fail "press_hold clears a steady-hold fixture" "$out"
grep -q "DETECT easy:hold" <<<"$out" && pass "solve_challenge classifies the hold challenge" \
  || fail "solve_challenge classifies the hold challenge" "$(grep DETECT <<<"$out")"

# Slide-to-verify fixture: knob follows held-button moves only; latches at the far end.
out="$(hb '
import urllib.parse
html = """<title>hb-e2e-slide</title><body style=margin:0>
<div id=track style=position:relative;width:320px;height:44px;background:#eee>
<div id=knob style=position:absolute;left:0;top:0;width:44px;height:44px;background:#88a></div></div>
<div id=ds>idle</div>
<script>
var down = false, kx = 0, left = 0;
var k = document.getElementById("knob"), ds = document.getElementById("ds");
k.addEventListener("mousedown", function(e){
  if(!e.isTrusted) return;
  down = true; kx = e.clientX - left; ds.textContent = "dragging";
});
document.addEventListener("mousemove", function(e){
  if(!down) return;
  left = Math.max(0, Math.min(276, e.clientX - kx)); k.style.left = left + "px";
});
document.addEventListener("mouseup", function(){
  if(!down) return;
  down = false; ds.textContent = left >= 270 ? "slid" : "short:" + left;
});
</script>"""
tid = open_tab("data:text/html," + urllib.parse.quote(html))
wait_for_load()
drag("#knob", dx=320)
print("SLIDE", js("document.getElementById(\"ds\").textContent"))
cdp("Target.closeTarget", targetId=tid)
')"
grep -q "SLIDE slid" <<<"$out" && pass "drag completes a slide-to-verify fixture" \
  || fail "drag completes a slide-to-verify fixture" "$out"

# ═════ 6. screenshots: validity, per-session names, parallel correctness ═════
say "[6] screenshots"

out="$(hb '
import urllib.parse
tid = open_tab("data:text/html," + urllib.parse.quote(
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
tid = open_tab("data:text/html," + urllib.parse.quote(
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

# The realistic multi-agent guarantee: many persistent agents each screenshotting their
# OWN backgrounded, never-window-visible tab, concurrently — every capture must be a real
# image (not a degenerate 2x2), and the viewer's visible tab must never move. This is the
# scenario agents actually hit; it exercises the per-target-session + screencast capture.
vtid="$(hb '
import urllib.parse
v = open_tab("data:text/html," + urllib.parse.quote("<title>hb-e2e-viewer2</title>"))
wait_for_load(); ext_call("activateTab", v); print("VTID", v)
' | sed -n 's/^VTID //p' | head -1)"
cap_worker() {  # $1 = worker index — its OWN session (distinct daemon + tab group).
  # BU_NAME derives from the text after the LAST hyphen, so the unique index must land
  # there (a shared trailing $$ would collide every worker onto one daemon).
  HORSE_SESSION="e2ecap$$-w$1" "$HB" <<PY 2>&1
import struct, urllib.parse
ok = bad = 0
for i in range(6):
    tid = open_tab("data:text/html," + urllib.parse.quote(
        f"<title>hb-e2e-cap$1-{i}</title><body style=background:#0a{i%9}>c{i}"))
    wait_for_load()
    p = capture_screenshot()
    with open(p, "rb") as f: h = f.read(24)
    w = struct.unpack(">II", h[16:24])[0] if h[:8] == b"\x89PNG\r\n\x1a\n" else 0
    ok += w > 4; bad += w <= 4
    cdp("Target.closeTarget", targetId=tid)
print(f"CAPRESULT $1 ok={ok} bad={bad}")
PY
}
for i in 1 2 3 4 5 6; do cap_worker "$i" > "$WORK/cap-$i" 2>&1 & done
wait
capbad=0; capok=0
for f in "$WORK"/cap-*; do
  line="$(grep CAPRESULT "$f" | head -1)"
  o="$(sed -n 's/.*ok=\([0-9]*\).*/\1/p' <<<"$line")"
  b="$(sed -n 's/.*bad=\([0-9]*\).*/\1/p' <<<"$line")"
  capok=$((capok + ${o:-0}))
  capbad=$((capbad + ${b:-0}))
done
[ "$capbad" = 0 ] && [ "$capok" -ge 30 ] \
  && pass "6 concurrent agents × 6 captures of background tabs: all real ($capok/36)" \
  || fail "concurrent background-tab captures all real" "$capbad degenerate of $((capok+capbad))"
if [ -n "$vtid" ]; then
  vis="$(hb "print('VIS', '$vtid' in (ext_call('activeTabTargets') or []))")"
  grep -q "VIS True" <<<"$vis" && pass "viewer's visible tab never moved during the capture storm" \
    || fail "viewer's visible tab never moved" "$vis"
  hb "cdp('Target.closeTarget', targetId='$vtid')" >/dev/null 2>&1
fi

# A screenshot of a background tab must NOT hijack the window's visible tab from whoever
# is watching. Give a background tab a surface requires raising it window-visible; the
# viewer's tab must be exactly what it was afterward (the "captured my inputs" regression).
if hb 'print("HAS", ext_call("activeTabTargets") is not None)' | grep -q "HAS True"; then
  out="$(hb '
import time, urllib.parse
a = open_tab("data:text/html," + urllib.parse.quote("<title>hb-e2e-viewer</title>viewing"))
wait_for_load(); ext_call("activateTab", a); time.sleep(0.2)
before = ext_call("activeTabTargets")
b = open_tab("data:text/html," + urllib.parse.quote("<title>hb-e2e-agent</title><body style=background:#08f>"))
wait_for_load()
capture_screenshot()                       # forces b window-visible to raster, then restores
after = ext_call("activeTabTargets")
print("RESTORED", before == after and a in (after or []))
cdp("Target.closeTarget", targetId=a); cdp("Target.closeTarget", targetId=b)
')"
  grep -q "RESTORED True" <<<"$out" \
    && pass "screenshot restores the viewer's visible tab (no hijack)" \
    || fail "screenshot restores the viewer's visible tab" "$out"
else
  skip "screenshot restores the viewer's visible tab" "extension activeTabTargets not live (relaunch needed)"
fi

# ═════ 7. real websites ══════════════════════════════════════════════════════
say "[7] real websites"

out="$(hb '
tid = open_tab("https://news.ycombinator.com")
wait_for_load()
print("HN", "Hacker News" in page_info().get("title",""),
      js("document.querySelectorAll(\".athing\").length"))
cdp("Target.closeTarget", targetId=tid)
')"
grep -q "HN True" <<<"$out" && pass "HN loads with a story list ($(sed -n 's/^HN True //p' <<<"$out" | head -1) stories)" \
  || fail "HN loads" "$out"

out="$(hb '
import time
tid = open_tab("https://duckduckgo.com")
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
if [ "${HB_ISOLATED:-0}" = 1 ]; then
  # A fresh isolated profile can land on DDG's consent/region interstitial, which eats the typed
  # query — non-hermetic. Validate trusted-input-on-a-real-SPA interactively (HB_ISOLATE=0).
  grep -q "DDG-URL-OK True" <<<"$out" && pass "DDG: hydrated SPA typed+submitted (trusted input)" \
    || skip "DDG: hydrated SPA typed+submitted" "external SPA on a fresh profile — run HB_ISOLATE=0"
else
  grep -q "DDG-URL-OK True" <<<"$out" && pass "DDG: hydrated SPA typed+submitted (trusted input)" \
    || fail "DDG: hydrated SPA typed+submitted" "$out"
fi

out="$(hb '
import time
tid = open_tab("https://en.wikipedia.org/wiki/Chrome_DevTools_Protocol")
wait_for_load()
time.sleep(1)
h1 = js("(document.querySelector(\"#firstHeading, h1\") || {}).textContent || \"\"") or ""
print("WIKI", len(h1.strip()) > 0, repr(h1.strip()[:40]))
cdp("Target.closeTarget", targetId=tid)
')"
grep -q "WIKI True" <<<"$out" && pass "Wikipedia article loads + parses" \
  || fail "Wikipedia article loads" "$out"

# ═════ 8. realness (from the hb-stealth module: fingerprint + input forensics) ═
say "[8] realness / anti-bot fingerprint"

# The always-on realness claims, all locally checkable. webdriver MUST be false (the #1
# automation tell); window.chrome present; and — the one Chrome-for-Testing tell the
# extension patches — "Google Chrome" must be in the UA-CH brand list (plain CfT shows
# only Chromium + Not;A=Brand), in BOTH the sync brands and getHighEntropyValues.
out="$(hb '
import json
tid = open_tab("https://example.com")
wait_for_load()
fp = json.loads(js("""JSON.stringify({
  webdriver: navigator.webdriver === true,
  hasChrome: typeof window.chrome === "object",
  brands: (navigator.userAgentData && navigator.userAgentData.brands || []).map(function(b){return b.brand}),
  plugins: navigator.plugins.length,
  langs: (navigator.languages || []).length,
  cores: navigator.hardwareConcurrency || 0
})"""))
he = js("""(async()=>{try{var h=await navigator.userAgentData.getHighEntropyValues([\"fullVersionList\"]);
  return (h.fullVersionList||[]).map(function(b){return b.brand}).join(\",\");}catch(e){return \"ERR\"}})()""") or ""
print("WEBDRIVER_TRUE", fp["webdriver"])
print("HASCHROME", fp["hasChrome"])
print("BRAND_JS", "Google Chrome" in fp["brands"])
print("BRAND_HE", "Google Chrome" in he)
print("NONDEGEN", fp["plugins"] > 0 and fp["langs"] > 0 and fp["cores"] > 0, fp)
cdp("Target.closeTarget", targetId=tid)
')"
if grep -q "WEBDRIVER_TRUE False" <<<"$out" && grep -q "HASCHROME True" <<<"$out"; then
  pass "navigator.webdriver is false; window.chrome present"
else
  fail "navigator.webdriver is false; window.chrome present" "$out"
fi
grep -q "BRAND_JS True" <<<"$out" && grep -q "BRAND_HE True" <<<"$out" \
  && pass '"Google Chrome" UA-CH brand present (CfT tell patched, sync + high-entropy)' \
  || fail '"Google Chrome" UA-CH brand present (extension fingerprint patch)' "$out"
grep -q "NONDEGEN True" <<<"$out" && pass "fingerprint non-degenerate (plugins/languages/cores)" \
  || fail "fingerprint non-degenerate" "$out"

# Input forensics (the hb-stealth Input Probe): a real keyboard sends shifted characters
# with shiftKey=true. Synthetic input that fires the char without a Shift modifier is a
# tell a real keyboard can never produce — and this guards horse_input.py's Shift-aware
# _key() (the exact path the _keyinfo 4-tuple collision once broke).
out="$(hb '
import json, urllib.parse
html = """<title>hb-e2e-shift</title><input id=t><script>window.ev=[];
document.getElementById(\"t\").addEventListener(\"keydown\",function(e){
  if(e.key.length===1) window.ev.push({k:e.key, s:e.shiftKey});});</script>"""
tid = open_tab("data:text/html," + urllib.parse.quote(html))
wait_for_load()
type_into("#t", "aB$3^")
ev = {d["k"]: d["s"] for d in json.loads(js("JSON.stringify(window.ev)"))}
# shifted glyphs must carry shiftKey=true; unshifted must not
ok = ev.get("B") is True and ev.get("$") is True and ev.get("^") is True \
     and ev.get("a") is False and ev.get("3") is False
print("SHIFT-COHERENT", ok, ev)
cdp("Target.closeTarget", targetId=tid)
')"
grep -q "SHIFT-COHERENT True" <<<"$out" \
  && pass "typed shifted chars carry shiftKey=true (unshifted do not)" \
  || fail "shiftKey coherence on typed input" "$out"

# ═════ 9. lifecycle: relaunch, recycle, stampede, locks (browser bounces) ════
if [ -n "${HB_TEST_FAST:-}" ]; then
  say "[9] lifecycle (SKIPPED: HB_TEST_FAST)"
  skip "cold relaunch after hard kill" "fast mode"
  skip "no dead-websocket daemon after relaunch" "fast mode"
  skip "5-way cold-launch stampede" "fast mode"
  skip "dead-holder stale lock break" "fast mode"
else
  say "[9] lifecycle (bounces the browser)"

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
