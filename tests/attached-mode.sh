#!/usr/bin/env bash
# tests/attached-mode.sh — drive a browser we did NOT launch.
#
# Every other suite runs against a horse-browser-launched Chrome: our extension loaded, our
# launch flags, our profile. That means all of it passes even if the code silently depends on
# the extension being there. This one launches a BARE Chrome for Testing —
# --remote-debugging-port and --user-data-dir, nothing else, NO --load-extension — and points
# the harness straight at it.
#
# It is the only test that proves the three things the registry rewrite was for:
#   • which tabs are a session's is answered without any extension (the registry is the truth)
#   • the realness mask still lands, applied per session over CDP by the daemon
#   • nothing in the normal path crashes or hangs when the service worker simply isn't there
#
# Deliberately does NOT use bin/horse-browser to start the browser — that would defeat the
# point. It talks to horse_harness.run the way the launcher does, with BU_CDP_URL pointed
# somewhere the launcher has never been.
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
PY="$ROOT/harness/.venv/bin/python"
PASS=0; FAIL=0; SKIP=0
say()  { printf '%s\n' "$*"; }
pass() { PASS=$((PASS+1)); say "  ✓ $1"; }
fail() { FAIL=$((FAIL+1)); say "  ✗ $1${2:+ — $2}"; }
skip() { SKIP=$((SKIP+1)); say "  - $1 (skipped${2:+: $2})"; }

"$ROOT/bin/horse-browser" harness-setup >/dev/null 2>&1
[ -x "$PY" ] || { say "FATAL: harness venv missing"; exit 1; }

BIN="$(sed -n 's/^BROWSER_BIN=//p' "$HOME/.config/horse-browser/config" 2>/dev/null | tr -d '"')"
[ -x "$BIN" ] || { say "FATAL: no Chrome for Testing binary (run: horse-browser update)"; exit 1; }

PORT="$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')"
WORK="$(mktemp -d -t hb-attached.XXXXXX)"
SESS="attached-$$-$RANDOM"
BU="hb-${SESS##*-}"
REG="$HOME/.config/horse-browser/tabs/$BU"

cleanup() {
  for p in $(pgrep -f "(browser|horse)_harness.daemon" 2>/dev/null); do
    ps eww -o command= -p "$p" 2>/dev/null | tr ' ' '\n' | grep -qx "HORSE_SESSION=$SESS" && kill "$p" 2>/dev/null
  done
  # By profile, not only by pid: `open -g -n` returns before the browser exists, so there is
  # no pid to keep. --user-data-dir is unique to this run, which makes it the reliable handle.
  [ -n "${CHROME_PID:-}" ] && kill "$CHROME_PID" 2>/dev/null
  _pids="$(ps -ww -o pid= -o command= -ax 2>/dev/null \
             | grep -F -- "--user-data-dir=$WORK/profile" | grep -v grep | awk '{print $1}')"
  for _p in $_pids; do kill "$_p" 2>/dev/null; done
  # Give Chrome a moment to actually go. Without it the rm below races a live process still
  # writing its profile, and leaves the directory behind with a confusing "not empty".
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    _left=""; for _p in $_pids; do kill -0 "$_p" 2>/dev/null && _left=1; done
    [ -z "$_left" ] && break
    sleep 0.5
  done
  for _p in $_pids; do kill -9 "$_p" 2>/dev/null; done
  [ -n "${REFL_PID:-}"  ] && { kill "$REFL_PID"; wait "$REFL_PID"; } 2>/dev/null
  rm -f "$REG"
  rm -rf "$WORK"
}
trap cleanup EXIT

# ── a BARE browser: the flags that make it reachable, and nothing else ──────────────
# …plus SwiftShader, deliberately. A headless Linux pod has no hardware GL — Xvfb is a
# software X server — so its WebGL renderer string names SwiftShader out loud, one of the
# most-checked bot signals there is. Forcing it here is the only way a Mac, which always has
# real GL, can test the mask that hides it. --enable-unsafe-swiftshader is required as well:
# Chrome 151 gates software WebGL behind it, and having NO WebGL is the louder tell.
#
# Launched focus-free on macOS. This suite's point is that the LAUNCHER did not set this
# browser up — no extension, none of our flags, the harness attaching over BU_CDP_URL to
# something it has never seen. `open -g -n` changes none of that; it only stops the window
# taking the operator's keyboard. Running this bare `"$BIN" &` stole focus every time the
# suite ran, which on a developer's machine is the one thing horse-browser exists to prevent —
# and a test suite has no business doing what the product refuses to do.
CHROME_ARGS=( --remote-debugging-port="$PORT" --user-data-dir="$WORK/profile"
              --no-first-run --no-default-browser-check
              --use-gl=angle --use-angle=swiftshader --enable-unsafe-swiftshader about:blank )
APP="${BIN%/Contents/MacOS/*}"
if [ "$(uname -s)" = "Darwin" ] && [ "$APP" != "$BIN" ] && [ -d "$APP" ]; then
  open -g -n -a "$APP" --args "${CHROME_ARGS[@]}"
  CHROME_PID=""            # `open` returns immediately; cleanup finds it by --user-data-dir
else
  "$BIN" "${CHROME_ARGS[@]}" >/dev/null 2>&1 &
  CHROME_PID=$!
fi
for _ in $(seq 1 60); do
  curl -sf --max-time 1 "http://127.0.0.1:$PORT/json/version" >/dev/null 2>&1 && break
  sleep 0.5
done
curl -sf --max-time 2 "http://127.0.0.1:$PORT/json/version" >/dev/null 2>&1 \
  || { say "FATAL: bare browser did not come up on :$PORT"; exit 1; }

say "== attached mode — a browser horse-browser never launched (:$PORT) =="

# hb <script> — the launcher's invocation, minus the launcher.
hb() { BU_CDP_URL="http://127.0.0.1:$PORT" BU_NAME="$BU" HORSE_SESSION="$SESS" \
       PYTHONPATH="$ROOT/harness" "$PY" -m horse_harness.run <<<"$1" 2>&1; }

# ── 0. the test is testing what it thinks it is ────────────────────────────────────
# NOT "zero service workers": a bare Chrome already runs Google's component extension
# (nkeimhogjdpnpccoofpliimaahmaaome). That is the point — its presence is exactly what made
# "the first chrome-extension:// worker" the wrong way to find ours. Assert instead that none
# of the workers here is OURS, which is the condition this whole suite is built on.
mine="$(hb 'print("MINE", ext_call("listTabsProbeThatDoesNotExist") is not None)' | sed -n 's/^MINE //p')"
[ "$mine" = "False" ] \
  && pass "bare browser carries no horse extension (component extensions notwithstanding)" \
  || fail "bare browser carries no horse extension" "ext_call resolved a worker: $mine"

# ── 1..4. the registry answers without an extension ────────────────────────────────
out="$(hb '
t1 = open_tab("data:text/html,<title>ATT-ONE</title>x"); wait_for_load()
t2 = open_tab("data:text/html,<title>ATT-TWO</title>x"); wait_for_load()
ids = [t["targetId"] for t in list_tabs()]
print("OPENED", t1 is not None and t2 is not None)
print("LISTED", t1 in ids and t2 in ids, len(ids))
print("TITLES", sorted(t.get("title") or "" for t in list_tabs()))
cdp("Target.closeTarget", targetId=t1)
print("PRUNED", t1 not in [t["targetId"] for t in list_tabs()])
')"
grep -q "OPENED True" <<<"$out" \
  && pass "open_tab works with no extension to group the tab" \
  || fail "open_tab works with no extension" "$out"
grep -q "LISTED True" <<<"$out" \
  && pass "list_tabs() names both tabs from the registry alone" \
  || fail "list_tabs() names both tabs" "$out"
grep -q "PRUNED True" <<<"$out" \
  && pass "list_tabs() drops a tab that was closed behind its back" \
  || fail "list_tabs() prunes a closed tab" "$out"
[ -s "$REG" ] && python3 -c "import json,sys; sys.exit(0 if json.load(open('$REG')) else 1)" 2>/dev/null \
  && pass "registry file written for this session (tabs/$BU)" \
  || fail "registry file written" "$REG missing or empty"

# ── 5. realness, JS half — applied per session by the daemon, not a content script ──
# On an https: page, not a data: URL: navigator.userAgentData is secure-context only, so a
# data: page reads as "no mask" whatever the mask is actually doing.
out="$(hb '
open_tab("https://example.com/"); wait_for_load()
r = realness_ok()
print("REAL", r["ok"], r["why"], r["brands"])
')"
grep -q "REAL True" <<<"$out" \
  && pass "realness JS half applied over CDP (brands claim Google Chrome)" \
  || fail "realness JS half applied over CDP" "$out"

# ── 6. realness, wire half — the header a bare browser would never send on its own ──
REFL_PORT="$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')"
python3 - "$REFL_PORT" >/dev/null 2>&1 <<'PY' &
import http.server, json, sys
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        b = json.dumps({"sec_ch_ua": self.headers.get("sec-ch-ua"),
                        "ua": self.headers.get("user-agent")}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)
    def log_message(self, *a): pass
http.server.HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
PY
REFL_PID=$!
for _ in $(seq 1 20); do curl -sf "http://127.0.0.1:$REFL_PORT/" >/dev/null 2>&1 && break; sleep 0.2; done
out="$(REFL_PORT="$REFL_PORT" hb '
import json, os, re
open_tab("http://127.0.0.1:" + os.environ["REFL_PORT"] + "/"); wait_for_load()
seen = json.loads(js("document.body.textContent") or "{}")
major = (re.search(r"Chrome/(\d+)", seen.get("ua") or "") or [None, None])[1]
brands = dict(re.findall(r"\"([^\"]+)\";v=\"([^\"]+)\"", seen.get("sec_ch_ua") or ""))
print("WIRE", brands.get("Google Chrome") == major and major is not None, seen.get("sec_ch_ua"))
')"
grep -q "WIRE True" <<<"$out" \
  && pass "realness wire half applied over CDP (sec-ch-ua matches the UA major)" \
  || fail "realness wire half applied over CDP" "$out"

# ── 6b. realness, WebGL half — the string that says "SwiftShader" out loud ──────────
# The browser above is forced onto SwiftShader, so unmasked it reports a software
# rasteriser, which real desktop Chrome never does. Assert BOTH strings, together: they are
# read as a pair and compared, so masking only the renderer would ship an Intel GPU behind a
# Google vendor — a pairing that exists on no real machine, i.e. one tell traded for another.
out="$(hb '
goto_url("https://example.com"); wait_for_load()
r = js("""(()=>{const rd=(c)=>{const g=document.createElement("canvas").getContext(c);
 if(!g)return ["no ctx","no ctx"]; const d=g.getExtension("WEBGL_debug_renderer_info");
 if(!d)return ["no ext","no ext"];
 return [g.getParameter(d.UNMASKED_VENDOR_WEBGL),g.getParameter(d.UNMASKED_RENDERER_WEBGL)];};
const a=rd("webgl"),b=rd("webgl2");
return {v:a[0],r:a[1],v2:b[0],r2:b[1],
        native:(""+WebGLRenderingContext.prototype.getParameter).includes("[native code]")};})()""")
print("V", r["v"]); print("R", r["r"])
print("SAME", r["v"]==r["v2"] and r["r"]==r["r2"])
print("NATIVE", r["native"])
')"
v="$(sed -n 's/^V //p' <<<"$out")"; rr="$(sed -n 's/^R //p' <<<"$out")"
same="$(sed -n 's/^SAME //p' <<<"$out")"; nat="$(sed -n 's/^NATIVE //p' <<<"$out")"
case "$rr" in
  *SwiftShader*|*swiftshader*|*llvmpipe*)
    fail "WebGL renderer no longer names a software rasteriser" "renderer=$rr" ;;
  "no ctx"|"no ext"|"")
    fail "WebGL renderer masked" "no usable WebGL context: [$rr] — $out" ;;
  *)
    pass "WebGL renderer no longer names a software rasteriser" ;;
esac
# Not just "both say Intel": the substitute has to agree with the PLATFORM beside it. A fixed
# Intel-on-Mesa tuple made an ARM Mac claim an x86 Intel chip through a Linux graphics stack,
# and this assertion approved it — the same fingerprinting code reads both.
plat="$(hb 'print("PLAT", js("(navigator.userAgentData||{}).platform || navigator.platform"))' | sed -n 's/^PLAT //p')"
case "$plat:$rr" in
  *mac*:*Metal*|*Mac*:*Metal*)   ok=1 ;;
  *Win*:*Direct3D*|*win*:*D3D*)  ok=1 ;;
  *Linux*:*Mesa*|*linux*:*Mesa*) ok=1 ;;
  *) ok= ;;
esac
[ -n "$ok" ] \
  && pass "WebGL renderer matches the platform ($plat / $(grep -oE 'Metal|Direct3D11|Mesa' <<<"$rr" | head -1))" \
  || fail "WebGL renderer matches the platform" "platform=$plat renderer=$rr — no real machine pairs those"
case "$v:$rr" in
  *Intel*:*Intel*|*Apple*:*Apple*) pass "WebGL vendor and renderer agree (masked as one pair)" ;;
  *) fail "WebGL vendor and renderer agree" "vendor=$v renderer=$rr" ;;
esac
[ "$same" = "True" ] \
  && pass "WebGL2 reports the same strings as WebGL1" \
  || fail "WebGL2 matches WebGL1" "$out"
[ "$nat" = "True" ] \
  && pass "patched getParameter still reads as [native code]" \
  || fail "patched getParameter reads as native" "$out"

# ── 7. the reap path: the registry names what to close, no extension involved ───────
before="$(curl -s "http://127.0.0.1:$PORT/json/list" | python3 -c \
  "import json,sys; print(sum(1 for t in json.load(sys.stdin) if 'ATT-' in (t.get('title') or '')))")"
ids="$(python3 -c "import json; print(' '.join(json.load(open('$REG'))))" 2>/dev/null)"
for tid in $ids; do curl -s "http://127.0.0.1:$PORT/json/close/$tid" >/dev/null 2>&1; done
sleep 0.5
after="$(curl -s "http://127.0.0.1:$PORT/json/list" | python3 -c \
  "import json,sys; print(sum(1 for t in json.load(sys.stdin) if 'ATT-' in (t.get('title') or '')))")"
{ [ "${before:-0}" -ge 1 ] && [ "${after:-0}" -eq 0 ]; } \
  && pass "every tab this session opened is closable from the registry ($before->$after)" \
  || fail "registry names every tab this session opened" "ATT-* $before->$after"

say ""
say "== $PASS passed, $FAIL failed, $SKIP skipped =="
[ "$FAIL" -eq 0 ]
