#!/usr/bin/env bash
# tests/challenge-frame.sh — reading a control INSIDE a cross-origin challenge frame.
#
# The claim under test: a challenge sealed in a cross-origin iframe is NOT out of reach. The
# frame is its own CDP target, so we can attach, read the control's real rect, and convert it
# to page coordinates — instead of escalating to vision or guessing a fraction of the frame
# (the PerimeterX path hardcoded 0.486×w, 0.553×h, which is right until a layout changes).
#
# Ground truth is the whole point. A live captcha cannot tell you where it put its button, and
# it moves under you — so this serves its own challenge on a DIFFERENT SITE with the control at
# coordinates the fixture chose, and asserts the solver returns those coordinates.
#
# Positions are RANDOM per run. A solver that hardcodes anything — the frame offset, a centre
# fraction, a vendor's usual layout — passes once and then fails, which is the point.
#
# Real out-of-process frames: site isolation keys on SITE, so two ports on 127.0.0.1 are
# cross-origin but SAME-site and the frame stays in the parent's process, with no separate
# target to attach to. --host-resolver-rules maps distinct hostnames, which produces the real
# thing (verified: the frame appears as its own iframe target).
set -u

# A test run must never reach the operator's ~/.claude or ~/.grok. 16 of 19 suites once
# lacked this, so `npm test` from ANY clone wired that clone's path into the real global
# settings.json — which is how a build agent's throwaway checkout came to leave a dead
# hook behind that failed every Bash call on the machine. external-state.sh is the one
# suite that unsets this, against temp paths of its own.
export HORSE_BROWSER_NO_RECONCILE=1
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
PY="$ROOT/harness/.venv/bin/python"
PASS=0; FAIL=0; FAILED=()
pass() { PASS=$((PASS+1)); echo "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); FAILED+=("$1"); echo "  ✗ $1${2:+ — $2}"; }
port() { python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()'; }

# Start a fixture and PROVE it is serving before anything depends on it. Sets FX_PORT to the
# parent port it actually bound; returns non-zero if it never came up.
#
# The fixture picks its own ports (0 0) and never lets go of the sockets between picking and
# serving. Picking here instead — bind port 0, read the number, close it, hand it over — leaves
# a window in which the OS can give that port to any outgoing connection, and on a busy machine
# (a browser per agent, a CDP websocket per session) it does. The old code then had no way to
# notice: the fixture bound inside its serving thread, so the failure killed the thread quietly
# while the JSON still appeared, `[ -s fx.json ]` said "ready", and Chrome loaded
# chrome-error://chromewebdata. Every assertion after that was measured against a blank page.
start_fixture() { # start_fixture <jsonfile> <kind> <x> <y> <w> <h>
  local out="$1"; shift
  : > "$out"
  python3 "$HERE/lib/oopif-fixture.py" 0 0 "$@" > "$out" &
  FIXPIDS="$FIXPIDS $!"
  for _ in $(seq 1 30); do [ -s "$out" ] && break; sleep 0.2; done
  FX_PORT=$(python3 -c "import json;print(json.load(open('$out'))['parent_port'])" 2>/dev/null)
  [ -n "$FX_PORT" ] || return 1
  # The JSON is printed once the serving threads are started, not once they are accepting —
  # so ask the socket itself rather than trusting the announcement.
  for _ in $(seq 1 40); do
    curl -sf -m 1 "http://127.0.0.1:$FX_PORT/" >/dev/null 2>&1 && return 0
    sleep 0.2
  done
  return 1
}

echo "horse-browser challenge-frame — reading a sealed cross-origin control"
"$ROOT/bin/horse-browser" harness-setup >/dev/null 2>&1
[ -x "$PY" ] || { echo "FATAL: harness venv missing"; exit 1; }
BIN="$(sed -n 's/^BROWSER_BIN=//p' "$HOME/.config/horse-browser/config" 2>/dev/null | tr -d '"')"
[ -x "$BIN" ] || { echo "FATAL: no Chrome (horse-browser update)"; exit 1; }

W="$(mktemp -d -t hb-cfr.XXXXXX)"
CPORT="$(port)"
cleanup() {
  for p in ${FIXPIDS:-}; do kill "$p" 2>/dev/null; done
  pids="$(ps -ww -o pid= -o command= -ax 2>/dev/null | grep -F -- "--user-data-dir=$W/p" | grep -v grep | awk '{print $1}')"
  for p in $pids; do kill "$p" 2>/dev/null; done
  sleep 1; for p in $pids; do kill -9 "$p" 2>/dev/null; done
  rm -rf "$W"
}
trap cleanup EXIT
FIXPIDS=""

# One browser for the whole suite, launched focus-free (a test must not take the operator's
# keyboard — see tests/attached-mode.sh, which learned this the hard way).
CHROME_ARGS=( --remote-debugging-port="$CPORT" --user-data-dir="$W/p"
              --no-first-run --no-default-browser-check
              # Without these a backgrounded test browser never acks Input.dispatchMouseEvent:
              # the renderer is throttled, every gesture call sits until the IPC timeout, and
              # the run looks like a hang rather than a missing flag. The real launcher passes
              # them for the same reason (bin/horse-browser).
              --disable-renderer-backgrounding
              --disable-background-timer-throttling
              --disable-backgrounding-occluded-windows
              --host-resolver-rules="MAP parent.test 127.0.0.1, MAP frame.test 127.0.0.1"
              about:blank )
APP="${BIN%/Contents/MacOS/*}"
if [ "$(uname -s)" = "Darwin" ] && [ "$APP" != "$BIN" ] && [ -d "$APP" ]; then
  open -g -n -a "$APP" --args "${CHROME_ARGS[@]}"
else
  "$BIN" "${CHROME_ARGS[@]}" >/dev/null 2>&1 &
fi
for _ in $(seq 1 60); do curl -sf -m 1 "http://127.0.0.1:$CPORT/json/version" >/dev/null 2>&1 && break; sleep 0.5; done
curl -sf -m 2 "http://127.0.0.1:$CPORT/json/version" >/dev/null 2>&1 \
  || { echo "FATAL: browser did not come up"; exit 1; }

hb() { BU_CDP_URL="http://127.0.0.1:$CPORT" BU_NAME="hb-cfr$$" HORSE_SESSION="cfr-$$" \
       PYTHONPATH="$ROOT/harness" "$PY" -m horse_harness.run <<<"$1" 2>&1; }

# --- one case: random control position, assert the reported page coords ------------------
one() {   # one <kind> <label>
  local kind="$1" label="$2"
  local cx cy cw ch p1 fx
  cx=$(( (RANDOM % 300) + 20 ))          # inside the 400x300 frame, below the decoy banner
  # NB the control CAN land on top of the decoy footer (which starts at y=240): cy+ch reaches
  # 278 on about 17% of draws. That is not a flake — the control is declared after the footer,
  # so it paints above it, and a solver that picks the topmost element at those coordinates must
  # still come back with the control. The earlier claim here that the draw was "clear of the
  # decoys" was simply wrong about the footer.
  cy=$(( (RANDOM % 140) + 90 ))
  cw=$(( (RANDOM % 60) + 24 ))
  ch=$(( (RANDOM % 30) + 20 ))
  start_fixture "$W/fx.json" "$kind" "$cx" "$cy" "$cw" "$ch" \
    || { fail "$label" "fixture did not start"; return; }
  p1="$FX_PORT"
  local ex ey
  ex=$(python3 -c "import json;print(json.load(open('$W/fx.json'))['expect_x'])" 2>/dev/null)
  ey=$(python3 -c "import json;print(json.load(open('$W/fx.json'))['expect_y'])" 2>/dev/null)
  [ -n "$ex" ] || { fail "$label" "fixture did not start"; return; }

  local out
  out="$(hb "
import time
from horse_harness.helpers import _xorigin_challenge, _frame_control
goto_url('http://parent.test:$p1/'); wait_for_load(); time.sleep(1.2)
xo = _xorigin_challenge()
fc = _frame_control(xo)
print('XO', xo)
# One value per line, already parsed. Regexing a Python dict repr out of shell is how the
# first version of this test 'failed' while the solver was returning correct coordinates.
if fc:
    print('FCX', fc['x']); print('FCY', fc['y']); print('FCK', fc['kind'])
")"
  local gx gy gk
  gx="$(sed -n 's/^FCX //p' <<<"$out")"
  gy="$(sed -n 's/^FCY //p' <<<"$out")"
  gk="$(sed -n 's/^FCK //p' <<<"$out")"
  if [ -z "$gx" ]; then
    fail "$label" "no control found: $(grep -E '^(XO|FC)' <<<"$out" | tr '\n' ' ' | cut -c1-120)"
    return
  fi
  # ±3px: the fixture's expectation uses integer division on the centre, and the browser
  # rounds subpixel layout. Anything larger means the offset maths is actually wrong.
  local dx=$(( gx > ex ? gx - ex : ex - gx )) dy=$(( gy > ey ? gy - ey : ey - gy ))
  if [ "$dx" -le 3 ] && [ "$dy" -le 3 ]; then
    pass "$label — found ($gx,$gy), expected ($ex,$ey), kind=$gk"
  else
    fail "$label" "got ($gx,$gy) expected ($ex,$ey) — off by ($dx,$dy)"
  fi
  [ "$gk" = "$kind" ] || fail "$label kind" "reported '$gk', fixture placed '$kind'"
}

echo "[1] each control kind, at a random position"
for k in checkbox press-hold slider button; do one "$k" "$k"; done

echo "[3] a BLOCK page is not a challenge"
# A vendor hard block has no solvable control — its only button is "contact support". The
# first version of this solver clicked it, because on a live DataDome block it was the single
# interactive element in the frame and therefore won by default. Reporting "vision:" there is
# worse than useless: it sends the agent to squint at a dead end and tells the operator the
# wrong thing about why the site did not open.
one_blocked() {
  local cx=$(( (RANDOM % 200) + 40 )) cy=$(( (RANDOM % 120) + 100 ))
  local p1
  start_fixture "$W/fxb.json" blocked "$cx" "$cy" 240 22 \
    || { fail "blocked frame" "fixture did not start"; return; }
  p1="$FX_PORT"
  local out
  out="$(hb "
import time
from horse_harness.helpers import _xorigin_challenge, _frame_control
goto_url('http://parent.test:$p1/'); wait_for_load(); time.sleep(1.2)
fc = _frame_control(_xorigin_challenge())
print('FCK', (fc or {}).get('kind'))
print('SC', solve_challenge(False))
")"
  local k; k="$(sed -n 's/^FCK //p' <<<"$out")"
  [ "$k" = "blocked" ] \
    && pass "a hard-block page reports kind=blocked, not a clickable decoy" \
    || fail "hard block detected" "reported kind='$k' — it would have clicked contact-support"
}
one_blocked

echo "[4] a second tab on the same site must not hijack the frame lookup"
# The browser is shared: a hundred subagents can be on the same challenged site at once, and
# every one of those tabs contributes an iframe target whose URL matches. Matching by URL alone
# returns whichever Chrome lists first — another agent's frame, at coordinates that mean nothing
# on our page. Caught live on Hermes: the frame read visibilityState "hidden" and its widget had
# never rendered, because it belonged to a backgrounded tab.
one_two_tabs() {
  local cx=$(( (RANDOM % 200) + 60 )) cy=$(( (RANDOM % 120) + 120 ))
  local p1 q1
  start_fixture "$W/fa.json" checkbox "$cx" "$cy" 30 30 \
    || { fail "two tabs" "fixture did not start"; return; }
  p1="$FX_PORT"
  # a DECOY tab: same vendor frame URL, deliberately different control coordinates
  start_fixture "$W/fb.json" checkbox 470 460 30 30 \
    || { fail "two tabs" "decoy fixture did not start"; return; }
  q1="$FX_PORT"
  local ex ey
  ex=$(python3 -c "import json;print(json.load(open('$W/fa.json'))['expect_x'])" 2>/dev/null)
  ey=$(python3 -c "import json;print(json.load(open('$W/fa.json'))['expect_y'])" 2>/dev/null)
  [ -n "$ex" ] || { fail "two tabs" "fixture did not start"; return; }
  local out
  out="$(hb "
import time
from horse_harness.helpers import _xorigin_challenge, _frame_control
# the decoy first, in a tab this session does NOT drive
other = cdp('Target.createTarget', url='http://parent.test:$q1/')['targetId']
time.sleep(2.0)
goto_url('http://parent.test:$p1/'); wait_for_load(); time.sleep(1.5)
fc = _frame_control(_xorigin_challenge())
if fc:
    print('FCX', fc['x']); print('FCY', fc['y'])
cdp('Target.closeTarget', targetId=other)
")"
  local gx gy; gx="$(sed -n 's/^FCX //p' <<<"$out")"; gy="$(sed -n 's/^FCY //p' <<<"$out")"
  if [ -z "$gx" ]; then fail "two tabs" "no control found"; return; fi
  local dx=$(( gx > ex ? gx - ex : ex - gx )) dy=$(( gy > ey ? gy - ey : ey - gy ))
  if [ "$dx" -le 3 ] && [ "$dy" -le 3 ]; then
    pass "with a decoy tab open, the control comes from OUR tab — ($gx,$gy)"
  else
    fail "two tabs" "got ($gx,$gy), our tab's control is at ($ex,$ey) — read the wrong tab's frame"
  fi
}
one_two_tabs

echo "[5] end to end: solve_challenge actually operates a working slider"
# Everything above proves the solver can FIND a control. This proves the gesture drives one:
# a real slide-to-verify widget on pointer events, which moves its handle, decides whether the
# release landed on the target, and drops the frame when it did. It exists because live
# challenges cannot be iterated against — a failed slide costs reputation, and on this address
# one failure took a site straight from "challenge" to "hard block". Two burned sites bought it.
one_live() {
  local cx=$(( (RANDOM % 60) + 30 )) cy=$(( (RANDOM % 120) + 100 ))
  local p1
  start_fixture "$W/fl.json" live-slider "$cx" "$cy" 60 40 \
    || { fail "live slider" "fixture did not start"; return; }
  p1="$FX_PORT"
  local out
  out="$(hb "
import time
goto_url('http://parent.test:$p1/'); wait_for_load(); time.sleep(1.2)
print('SC', solve_challenge())
from horse_harness.helpers import _xorigin_challenge
print('GONE', _xorigin_challenge() is None)
")"
  local sc gone
  sc="$(sed -n 's/^SC //p' <<<"$out")"; gone="$(sed -n 's/^GONE //p' <<<"$out")"
  if [ "$gone" = "True" ]; then
    pass "solve_challenge cleared a working slider — ${sc:0:60}"
  else
    fail "live slider" "not cleared: ${sc:0:110}"
  fi
}
one_live

echo "[2] repeated with fresh random positions (a hardcoded answer cannot survive this)"
for i in 1 2 3; do one checkbox "checkbox run $i"; done

echo
if [ "$FAIL" -eq 0 ]; then echo "── $PASS passed, 0 failed"; else
  echo "── $PASS passed, $FAIL failed"; for f in "${FAILED[@]}"; do echo "   ✗ $f"; done; fi
exit $((FAIL > 0))
