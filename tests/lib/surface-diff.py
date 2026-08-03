#!/usr/bin/env python3
"""Every property reachable from globalThis, in this browser and in a real one, diffed.

The bugs found so far — pressure 0 under a held button, ShiftLeft at location 0, a zero-size
window, integer-only coordinates — were each found by someone deciding to look at one particular
thing. That does not scale: there are tens of thousands of reachable properties and a detector
only needs one.

So: walk the whole graph instead. From globalThis, every own property name and symbol, its
descriptor SHAPE as well as its value, following values and prototypes, cycle-guarded and
bounded. Then run the identical walk in a browser driven by a hand and diff the two.

    python3 tests/lib/surface-diff.py --serve    # open the URL in YOUR normal browser
    python3 tests/lib/surface-diff.py --ours     # walk it in horse-browser
    python3 tests/lib/surface-diff.py --report   # diff

What makes the diff usable is the volatility pass: the page walks TWICE and anything that
disagrees with itself — timestamps, sizes, ids, anything live — is dropped before comparing.
Without that, the report is thousands of rows of noise and nobody reads it.

Three comparisons, in increasing order of how much they can tell you:

  --ours vs --plain   what horse-browser ADDS to a browser. Needs nobody, every row is our
                      own doing. Measured: 12 differences across 185,350 properties, all of
                      them environmental (link speed, heap size, navigation ids, window size).
  --ours vs --real    Chrome for Testing versus an everyday Chrome. Needs a human to click
                      once in their own browser. This is the gap the realness masks exist to
                      close, and some of it cannot be closed.

Two things learned building it, both of which made an early version report "identical" for
browsers that differed in every field a detector reads:

  Getters must be invoked against an INSTANCE. Nearly all of the interesting surface —
  navigator.*, screen.*, document.* — is accessors defined on a prototype, and reading them ON
  the prototype throws Illegal Invocation. A walk that treats prototypes as ordinary objects
  records THREW for the entire fingerprint and calls it a match.

  Aliases dedupe by identity, so a path is whichever name sorts first: navigator's properties
  appear under .clientInformation.* because that is the same object. Stable across browsers, so
  the diff is unaffected — surprising when reading it.

What this CANNOT cover, and needs separate probes: values that are computed rather than stored —
canvas pixels, WebGL's several hundred getParameter enums, AudioContext output, font metrics,
timing resolution. A property walk finds the API surface, not what the API renders.
"""
import http.server, json, os, subprocess, sys, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
OUT = os.path.join(HERE, "traces")
PORT = int(os.environ.get("HB_SURFACE_PORT") or 8779)

WALK = r"""
(function(){
  var MAX_ENTRIES = 200000, MAX_DEPTH = 6;

  // The descriptor's SHAPE, not just what it holds. A mask usually gets the value right and the
  // shape wrong: a data property where the browser has an accessor, enumerable where the browser
  // is not, or a getter whose toString() no longer says [native code].
  function flags(d){
    return (d.enumerable?'e':'-') + (d.configurable?'c':'-') + (d.writable?'w':'-')
         + (d.get?'g':'-') + (d.set?'s':'-');
  }
  function fnSig(f){
    var s = '';
    try { s = Function.prototype.toString.call(f); } catch(e){ return 'fn:untostringable'; }
    var native = /\{\s*\[native code\]\s*\}/.test(s);
    var name = ''; try { name = f.name; } catch(e){}
    var len = ''; try { len = f.length; } catch(e){}
    return 'fn:' + (native ? 'native' : 'js') + ':' + name + '/' + len;
  }
  function summarize(v){
    var t = typeof v;
    if (v === null) return 'null';
    if (t === 'undefined') return 'undefined';
    if (t === 'function') return fnSig(v);
    if (t === 'number' || t === 'boolean') return t + ':' + String(v);
    if (t === 'bigint') return 'bigint:' + String(v);
    if (t === 'string') return 'string(' + v.length + '):' + (v.length > 120 ? v.slice(0,120)+'…' : v);
    if (t === 'symbol') return 'symbol:' + String(v);
    var tag = ''; try { tag = Object.prototype.toString.call(v); } catch(e){ tag = '[object ?]'; }
    var ctor = ''; try { ctor = v.constructor && v.constructor.name; } catch(e){}
    return 'object:' + tag + (ctor ? '/' + ctor : '');
  }
  function recursable(v){
    var t = typeof v;
    return (t === 'object' || t === 'function') && v !== null;
  }

  // The EFFECTIVE property set of an object: its own properties plus everything it inherits,
  // each tagged with where it was defined. This is the whole point. Nearly all of the
  // interesting surface — navigator.*, screen.*, document.* — is accessors defined on a
  // prototype, and reading them ON that prototype throws Illegal Invocation. A walk that
  // enumerates prototypes as ordinary objects therefore records "THREW:TypeError" for the
  // entire fingerprint and reports two browsers as identical while they differ in every field
  // a detector actually reads. Getters must be invoked against an INSTANCE.
  function effective(obj){
    var out = [], seen = {}, o = obj, up = 0;
    while (o && up < 8) {
      var names = [];
      try { names = Object.getOwnPropertyNames(o); } catch(e){}
      try { names = names.concat(Object.getOwnPropertySymbols(o).map(String)); } catch(e){}
      for (var i = 0; i < names.length; i++) {
        var name = names[i];
        if (Object.prototype.hasOwnProperty.call(seen, name)) continue;   // shadowing: nearest wins
        seen[name] = 1;
        var d = null;
        try { d = Object.getOwnPropertyDescriptor(o, name); } catch(e){}
        if (d) out.push([name, d, up]);
      }
      try { o = Object.getPrototypeOf(o); } catch(e){ break; }
      up++;
    }
    out.sort(function(a, b){ return a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0; });
    return out;
  }

  function walk(){
    var out = {}, seen = new WeakSet(), q = [[globalThis, '', 0]], n = 0;
    seen.add(globalThis);
    while (q.length && n < MAX_ENTRIES) {
      var item = q.shift(), obj = item[0], path = item[1], depth = item[2];
      var props = effective(obj);
      for (var i = 0; i < props.length && n < MAX_ENTRIES; i++) {
        var name = props[i][0], d = props[i][1], up = props[i][2];
        var p = path + '.' + name, v, note;
        // Read the getter with the instance as receiver — that is what a page gets. Some still
        // throw by design (a cross-origin window, a permission-gated API), and "it threw" is
        // itself a fact worth comparing.
        try {
          v = d.get ? d.get.call(obj) : d.value;
          note = flags(d) + '@' + up + ' ' + summarize(v);
        } catch(e){
          v = undefined; note = flags(d) + '@' + up + ' THREW:' + (e && e.name);
        }
        out[p] = note; n++;
        if (depth < MAX_DEPTH && recursable(v)) {
          try { if (!seen.has(v)) { seen.add(v); q.push([v, p, depth + 1]); } } catch(e){}
        }
      }
    }
    return out;
  }

  // Twice, so anything that disagrees with ITSELF can be dropped before any cross-browser
  // comparison happens. This is the difference between a report someone reads and 20k rows of
  // clocks and sizes.
  var a = walk(), b = walk(), volatile_ = {};
  Object.keys(a).forEach(function(k){ if (a[k] !== b[k]) volatile_[k] = 1; });
  Object.keys(b).forEach(function(k){ if (!(k in a)) volatile_[k] = 1; });
  return {surface: a, volatile: volatile_,
          ua: navigator.userAgent, entries: Object.keys(a).length};
})()
"""

PAGE = """<!doctype html><meta charset=utf-8><title>surface capture</title>
<style>
 :root{color-scheme:light dark} body{margin:0;font:15px/1.6 system-ui,sans-serif;padding:30px 34px}
 h1{font-size:18px;margin:0 0 6px} p{margin:0 0 18px;opacity:.75;max-width:60ch}
 button{padding:10px 18px;font:15px system-ui;border-radius:6px;cursor:pointer}
 #s{margin-left:14px;opacity:.8}
</style>
<body>
<h1>Capture this browser's API surface</h1>
<p>Click once. It walks every property reachable from <code>globalThis</code>, twice, and sends
the result to the local script. Nothing leaves this machine.</p>
<button id=go>Capture</button><span id=s></span>
<script>
document.getElementById('go').addEventListener('click', function(){
  var s = document.getElementById('s');
  s.textContent = 'walking…';
  setTimeout(function(){
    var r = %s;
    fetch('/save?tag=' + (new URLSearchParams(location.search).get('tag') || 'real'),
          {method:'POST', headers:{'content-type':'application/json'}, body: JSON.stringify(r)})
      .then(function(){ s.textContent = 'sent ' + r.entries + ' properties — done'; })
      .catch(function(e){ s.textContent = 'failed: ' + e; });
  }, 30);
});
</script>
""" % WALK


def serve():
    os.makedirs(OUT, exist_ok=True)

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            b = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers(); self.wfile.write(b)

        def do_POST(self):
            tag = self.path.split("tag=")[-1] or "real"
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            p = os.path.join(OUT, "surface-%s.json" % tag)
            with open(p, "wb") as f:
                f.write(raw)
            d = json.loads(raw)
            print("  saved %s — %d properties, %d volatile"
                  % (os.path.basename(p), d.get("entries", 0), len(d.get("volatile", {}))), flush=True)
            self.send_response(204); self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", PORT), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def load(tag):
    p = os.path.join(OUT, "surface-%s.json" % tag)
    return json.load(open(p)) if os.path.exists(p) else None


# Differences that are expected and say nothing about automation: the window's own size and
# position, the page's own URL, and anything naming the profile or the port.
BORING = ("innerWidth", "innerHeight", "outerWidth", "outerHeight", "screenX", "screenY",
          "pageXOffset", "pageYOffset", "scrollX", "scrollY", "devicePixelRatio",
          "location", "origin", "document", "history", "performance", "crypto")


def report(baseline="real"):
    ours, real = load("ours"), load(baseline)
    if not ours:
        print("no 'ours' capture — run --ours first"); return 1
    if not real:
        print("no '%s' capture — run --plain, or --serve and open the page in your own browser"
              % baseline); return 1
    a, b = ours["surface"], real["surface"]
    vol = set(ours.get("volatile", {})) | set(real.get("volatile", {}))
    print("\n  ours : %s" % ours.get("ua", "")[:96])
    print("  real : %s" % real.get("ua", "")[:96])
    print("  %d vs %d properties, %d volatile and excluded" % (len(a), len(b), len(vol)))

    only_ours = sorted(k for k in a if k not in b and k not in vol)
    only_real = sorted(k for k in b if k not in a and k not in vol)
    changed = sorted(k for k in a if k in b and k not in vol and a[k] != b[k])

    def boring(k):
        return any(("." + w) in k for w in BORING)

    def show(title, keys, fmt):
        keys = [k for k in keys if not boring(k)]
        print("\n  %s (%d)" % (title, len(keys)))
        for k in keys[:60]:
            print("    %s" % fmt(k))
        if len(keys) > 60:
            print("    … and %d more" % (len(keys) - 60))

    show("only in horse-browser", only_ours, lambda k: "%s  %s" % (k, a[k][:70]))
    show("only in the real browser", only_real, lambda k: "%s  %s" % (k, b[k][:70]))
    show("different", changed,
         lambda k: "%s\n      ours %s\n      real %s" % (k, a[k][:80], b[k][:80]))
    return 0


def drive_ours():
    srv = serve()
    script = """
import json, time
open_tab("http://127.0.0.1:%d/?tag=ours"); wait_for_load(); time.sleep(0.5)
click("#go")
time.sleep(6)
print("STATUS", js("document.getElementById('s').textContent"))
for t in list_tabs():
    try: cdp("Target.closeTarget", targetId=t["targetId"])
    except Exception: pass
""" % PORT
    r = subprocess.run([os.path.join(ROOT, "bin", "horse-browser")], input=script,
                       text=True, capture_output=True, timeout=300)
    for line in (r.stdout or "").splitlines():
        if line.startswith("STATUS"):
            print("  horse-browser:", line[7:])
    time.sleep(1.0)
    srv.shutdown()
    return report()


def drive_plain():
    """The same Chrome, launched plainly: no extension, no CDP driving the page.

    This is the more useful of the two comparisons and it needs nobody. diff(ours, plain) is
    exactly what horse-browser ADDS to a browser — every row is our responsibility and our bug.
    diff(plain, real) is a different question: how Chrome for Testing differs from an everyday
    Chrome, which is what the realness masks exist to close and which no amount of care on our
    side can fully erase.
    """
    import glob, shutil, tempfile
    srv = serve()
    cfg = os.path.expanduser("~/.config/horse-browser/config")
    binp = ""
    for line in open(cfg):
        if line.startswith("BROWSER_BIN="):
            binp = line.split("=", 1)[1].strip().strip('"')
    if not binp or not os.path.exists(binp):
        print("no Chrome binary in %s" % cfg); return 1
    prof = tempfile.mkdtemp(prefix="hb-plain.")
    port = PORT + 100
    args = [binp, "--remote-debugging-port=%d" % port, "--user-data-dir=" + prof,
            "--no-first-run", "--no-default-browser-check",
            "--disable-renderer-backgrounding", "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows", "about:blank"]
    app = binp.split("/Contents/MacOS/")[0]
    if sys.platform == "darwin" and app != binp and os.path.isdir(app):
        subprocess.run(["open", "-g", "-n", "-a", app, "--args"] + args[1:], check=False)
    else:
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        try:
            import urllib.request
            urllib.request.urlopen("http://127.0.0.1:%d/json/version" % port, timeout=1)
            break
        except Exception:
            time.sleep(0.5)
    script = """
import json, time
open_tab("http://127.0.0.1:%d/?tag=plain"); wait_for_load(); time.sleep(0.5)
click("#go")
time.sleep(6)
print("STATUS", js("document.getElementById('s').textContent"))
""" % PORT
    env = dict(os.environ, BU_CDP_URL="http://127.0.0.1:%d" % port, BU_NAME="hb-plain",
               HORSE_SESSION="plain", PYTHONPATH=os.path.join(ROOT, "harness"))
    r = subprocess.run([os.path.join(ROOT, "harness", ".venv", "bin", "python"),
                        "-m", "horse_harness.run"], input=script, text=True,
                       capture_output=True, timeout=300, env=env)
    for line in (r.stdout or "").splitlines():
        if line.startswith("STATUS"):
            print("  plain chrome:", line[7:])
    time.sleep(1.0)
    srv.shutdown()
    subprocess.run("pkill -f 'user-data-dir=%s'" % prof, shell=True)
    time.sleep(1); shutil.rmtree(prof, ignore_errors=True)
    return 0


if __name__ == "__main__":
    if "--plain" in sys.argv:
        sys.exit(drive_plain())
    if "--ours" in sys.argv:
        sys.exit(drive_ours())
    if "--serve" in sys.argv:
        serve()
        print("\n  Open  http://127.0.0.1:%d/  in your NORMAL browser and click Capture.\n"
              "  Ctrl-C when done, then: python3 tests/lib/surface-diff.py --report\n" % PORT)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n  stopped")
        sys.exit(0)
    sys.exit(report("plain" if "--vs-plain" in sys.argv else "real"))
