#!/usr/bin/env python3
"""Record what a REAL hand does to a slide-to-verify widget, and compare ours against it.

Every property drag() is tested on so far came from reasoning about hands, not from measuring
one: approach, asymmetric velocity, overshoot-and-correct, a hold before release. Those are
plausible. They are not evidence. And the one judge that could settle it — DataDome's scoring —
answers one bit at a time and charges a site per answer.

So: serve the widget with the live one's geometry (280x40 track, 63x40 handle, 222px of travel),
let a person drag it with their own hand, and post the raw event stream back here. Then run our
drag against the identical page and put the two distributions side by side. Whatever differs is
either a tell we are emitting or an assumption we invented.

    python3 tests/lib/human-trace.py            # record: open the URL, drag it a few times
    python3 tests/lib/human-trace.py --report   # compare what was recorded against ours

Traces land in tests/lib/traces/ as JSON, so a recording session is worth keeping and re-running
against a changed drag() later.
"""
import http.server, json, os, statistics, sys, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
TRACES = os.path.join(HERE, "traces")
PORT = int(os.environ.get("HB_TRACE_PORT") or 8777)

# The live DataDome slider, measured on hermes.com: handle 63x40, target 222px to the right.
# Same distance matters — velocity profiles do not scale linearly, so a 100px toy would not
# tell us about a 222px drag.
GEOM = dict(track_w=280, track_h=40, handle_w=63, handle_h=40, travel=222, x=60, y=180)

PAGE = """<!doctype html><meta charset=utf-8><title>drag recorder</title>
<style>
  :root { color-scheme: light dark; }
  body { margin:0; font:15px/1.5 system-ui, sans-serif; padding:28px 32px; }
  h1 { font-size:17px; margin:0 0 4px; font-weight:650; }
  p  { margin:0 0 22px; opacity:.75; }
  #stage { position:relative; height:300px; }
  #track { position:absolute; left:%(x)dpx; top:%(y)dpx; width:%(track_w)dpx;
           height:%(track_h)dpx; background:#8883; border-radius:6px; }
  #target{ position:absolute; top:%(y)dpx; width:%(handle_w)dpx; height:%(handle_h)dpx;
           background:#4a90d922; border:1px dashed #4a90d9; border-radius:6px; box-sizing:border-box; }
  #h    { position:absolute; left:%(x)dpx; top:%(y)dpx; width:%(handle_w)dpx;
          height:%(handle_h)dpx; background:#4a90d9; border-radius:6px; cursor:grab;
          touch-action:none; }
  #h.grabbing { cursor:grabbing; }
  #count { font-variant-numeric:tabular-nums; font-weight:650; }
  #hint  { opacity:.6; }
</style>
<body>
<h1>Drag the blue handle onto the dashed box.</h1>
<p>Do it the way you normally would — no need to be careful or precise.
   <span id=count>0</span> recorded. <span id=hint>Five or so is plenty.</span></p>
<div id=stage>
  <div id=track></div>
  <div id=target style="left:%(target_x)dpx"></div>
  <div id=h></div>
</div>
<script>
var G = %(geom)s;
var h = document.getElementById('h'), n = 0, log = [], down = false, sx = 0, ox = 0, t0 = 0;

// Capture the pointer's whole life, not just what the widget needs — the approach BEFORE the
// press is part of what a scorer sees, and it is the part a synthetic drag most often omits.
addEventListener('pointermove', function(e){
  if (log.length) log.push(['m', r(e.clientX), r(e.clientY), Math.round(performance.now()-t0)]);
}, true);
h.addEventListener('pointerenter', function(e){
  if (!log.length && !down) { t0 = performance.now(); log = [['e', r(e.clientX), r(e.clientY), 0]]; }
});
function r(v){ return Math.round(v*10)/10; }

h.addEventListener('pointerdown', function(e){
  down = true; sx = e.clientX; ox = parseInt(h.style.left || G.x, 10);
  h.setPointerCapture(e.pointerId); h.className = 'grabbing';
  if (!log.length) { t0 = performance.now(); log = []; }
  log.push(['d', r(e.clientX), r(e.clientY), Math.round(performance.now()-t0)]);
});
addEventListener('pointermove', function(e){
  if (!down) return;
  var nx = Math.max(G.x, Math.min(G.x + G.travel + 40, ox + (e.clientX - sx)));
  h.style.left = nx + 'px';
});
addEventListener('pointerup', function(e){
  if (!down) return;
  down = false; h.className = '';
  log.push(['u', r(e.clientX), r(e.clientY), Math.round(performance.now()-t0)]);
  var landed = parseInt(h.style.left, 10);
  var hit = Math.abs(landed - (G.x + G.travel)) <= 10;
  fetch('/trace', {method:'POST', headers:{'content-type':'application/json'},
                   body: JSON.stringify({events: log, hit: hit, landed: landed})});
  n++; document.getElementById('count').textContent = n;
  if (n >= 5) document.getElementById('hint').textContent = 'Thanks — that is enough.';
  log = []; h.style.left = G.x + 'px';       // back to the start for the next one
});
</script>
"""


def page():
    g = dict(GEOM); g["target_x"] = GEOM["x"] + GEOM["travel"]
    g["geom"] = json.dumps(GEOM)
    return PAGE % g


def record():
    os.makedirs(TRACES, exist_ok=True)

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            b = page().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers(); self.wfile.write(b)

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            try:
                d = json.loads(raw)
            except Exception:
                self.send_response(400); self.end_headers(); return
            p = os.path.join(TRACES, "human-%d.json" % int(time.time() * 1000))
            d["source"] = "human"
            with open(p, "w") as f:
                json.dump(d, f)
            n = len([x for x in os.listdir(TRACES) if x.startswith("human-")])
            print("  recorded %s  (%d event%s, landed=%s, hit=%s)  [%d total]"
                  % (os.path.basename(p), len(d["events"]), "" if len(d["events"]) == 1 else "s",
                     d.get("landed"), d.get("hit"), n), flush=True)
            self.send_response(204); self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", PORT), H)
    print("\n  Open  http://127.0.0.1:%d/  and drag the handle onto the dashed box a few times.\n"
          "  Ctrl-C when done, then: python3 tests/lib/human-trace.py --report\n" % PORT, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped — %d trace(s) in %s" % (
            len([x for x in os.listdir(TRACES) if x.endswith(".json")]), TRACES))


# ── analysis ────────────────────────────────────────────────────────────────────────────────
def metrics(ev):
    """The same numbers tests/drag-profile.sh asserts on, so human and synthetic are directly
    comparable. Returns None for a trace with no usable press/release pair."""
    m = [e for e in ev if e[0] in ("m", "e")]
    try:
        d = next(e for e in ev if e[0] == "d")
        u = next(e for e in ev if e[0] == "u")
    except StopIteration:
        return None
    during = [e for e in m if d[3] <= e[3] <= u[3]]
    if len(during) < 3:
        return None
    dur = (u[3] - d[3]) / 1000.0
    x0, x1 = d[1], u[1]
    span = abs(x1 - x0) or 1
    half = next((e for e in during if abs(e[1] - x0) >= span / 2.0), during[-1])
    ts = [e[3] for e in during]
    dts = [b - a for a, b in zip(ts, ts[1:]) if b > a]
    return {
        "approach": len([e for e in m if e[3] < d[3]]),
        "dur": round(dur, 2),
        "samples": len(during),
        "rate_hz": round(len(during) / dur, 1) if dur else 0,
        "overshoot": round(max(e[1] for e in during) - x1, 1),
        "half_at": round((half[3] - d[3]) / max(1, u[3] - d[3]), 2),
        "dt_cv": round(statistics.pstdev(dts) / (statistics.mean(dts) or 1), 2) if len(dts) > 1 else 0,
        "hold_ms": u[3] - max(e[3] for e in during),
        "dy_range": round(max(e[2] for e in during) - min(e[2] for e in during), 1),
    }


def report():
    if not os.path.isdir(TRACES):
        print("no traces yet — run without --report first"); return 1
    groups = {}
    for name in sorted(os.listdir(TRACES)):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(TRACES, name)) as f:
            d = json.load(f)
        m = metrics(d["events"])
        if m:
            m["hit"] = d.get("hit")
            groups.setdefault(d.get("source", "?"), []).append(m)
    if not groups:
        print("no usable traces"); return 1

    keys = ["dur", "samples", "rate_hz", "approach", "overshoot", "half_at", "dt_cv",
            "hold_ms", "dy_range"]
    print("\n%-10s %s" % ("", "  ".join("%9s" % k for k in keys)))
    for src, rows in sorted(groups.items()):
        med = {k: statistics.median([r[k] for r in rows]) for k in keys}
        print("%-10s %s   (n=%d, hit=%d)"
              % (src, "  ".join("%9s" % round(med[k], 2) for k in keys),
                 len(rows), sum(1 for r in rows if r.get("hit"))))
    # Overshoot is BIMODAL — about half of all drags have none, the rest run 5-24px past — so
    # its median is a number no individual drag resembles, and comparing medians reports a 2.4x
    # gap between two distributions that are in fact the same shape. Split it instead.
    bimodal = {"overshoot"}
    if len(groups) > 1 and "human" in groups:
        print("\n  Where ours differs from a hand (median ratio; 1.00 = identical):")
        hum = {k: statistics.median([r[k] for r in groups["human"]]) for k in keys}
        for src, rows in sorted(groups.items()):
            if src == "human":
                continue
            med = {k: statistics.median([r[k] for r in rows]) for k in keys}
            flagged = False
            for k in keys:
                if k in bimodal or not hum[k]:
                    continue
                if abs(med[k] / hum[k] - 1) > 0.35:
                    flagged = True
                    print("    %-9s %-10s ours %-8s hand %-8s  (%.2fx)"
                          % (src, k, round(med[k], 2), round(hum[k], 2), med[k] / (hum[k] or 1)))
            if not flagged:
                print("    %-9s nothing outside 35%%" % src)
        print("\n  Overshoot, split (a median would misrepresent it):")
        for src, rows in sorted(groups.items()):
            vals = [r["overshoot"] for r in rows]
            big = [v for v in vals if v >= 1.5]
            print("    %-9s none %d/%d (%2.0f%%)   when it does: %s"
                  % (src, len(vals) - len(big), len(vals),
                     100.0 * (len(vals) - len(big)) / len(vals),
                     "%.1f-%.1f px" % (min(big), max(big)) if big else "-"))
    return 0


if __name__ == "__main__":
    sys.exit(report() if "--report" in sys.argv else (record() or 0))
