#!/usr/bin/env python3
"""A real out-of-process iframe, on demand, with the control at a KNOWN position.

Why this exists: `_frame_control` claims it can read a control inside a cross-origin challenge
frame and convert its rect to page coordinates. That claim is only worth anything if it is
checked against ground truth — a live captcha gives you no ground truth at all (you cannot ask
DataDome where it put the button), and it changes under you.

So: serve a parent page on one site and the "challenge" on ANOTHER, with the control placed at
coordinates this file chose. Then the expected answer is known exactly, and the test can assert
pixels rather than vibes.

Two ports is NOT enough. Chrome's site isolation keys on SITE (scheme + eTLD+1), so
127.0.0.1:8001 and 127.0.0.1:8002 are cross-ORIGIN but same-site, and the frame stays in the
parent's process — no separate CDP target, which is the very thing under test. Distinct
hostnames mapped with --host-resolver-rules produce a genuine OOPIF.

    python3 oopif-fixture.py <parent-port> <frame-port> <kind> <x> <y> <w> <h>

Pass 0 for either port to have the OS choose one; the ports actually bound come back as
parent_port/frame_port in the JSON on stdout. Prefer that to picking a port yourself — see
_bind() for the race it avoids.

kind: checkbox | press-hold | slider | button | blocked | live-slider

"live-slider" is the only one that WORKS: a real slide-to-verify widget, driven by pointer
events, that decides whether it was solved and tells the parent to drop the frame. The static
kinds prove the solver can FIND a control; this one proves the gesture actually operates one and
that the clear is detected. It exists because live challenges cannot be used to iterate — a
failed slide on DataDome costs reputation, and on this address one failure took the site
straight from "challenge" to "hard block". Two sites burned that way bought this fixture.
"""
import sys, threading, http.server, json

PARENT_PORT, FRAME_PORT, KIND, CX, CY, CW, CH = (
    int(sys.argv[1]), int(sys.argv[2]), sys.argv[3],
    int(sys.argv[4]), int(sys.argv[5]), int(sys.argv[6]), int(sys.argv[7]))


def _bind(port):
    """Bind NOW, in the main thread, and hand back the socket with the port it really got.

    Two bugs live where this used to be. The bind happened inside the serving thread, so a
    failure killed that thread silently while main went on to print the fixture JSON — the
    caller saw a ready fixture with nothing listening, Chrome got chrome-error://chromewebdata,
    and every assertion after it was measured against a blank page. And the port came from a
    caller that had bound port 0, read the number, and closed it: on a busy machine (a browser
    per agent, a CDP websocket per session) the OS hands that same port to an outgoing
    connection before this process can claim it.

    Pass 0 to close both: the OS picks, and we never let go of the socket between picking and
    serving, so there is no window to lose. A bind that does fail now raises here — a loud,
    correct death before anything depends on it.
    """
    class _Unset(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", port), _Unset)
    return srv, srv.server_address[1]


# Bound before the page bodies below are built: the parent's HTML embeds the frame's real port.
PARENT_SRV, PARENT_PORT = _bind(PARENT_PORT)
FRAME_SRV, FRAME_PORT = _bind(FRAME_PORT)

# The frame URL carries "captcha-delivery" on purpose: that is the fragment _xorigin_challenge
# recognises as DataDome and the one _frame_control re-attaches by. Serving it at a neutral
# path would test a shortcut instead of the code path that actually runs on a live challenge.
#
# The iframe sits at a deliberately non-zero offset in the parent: a solver that forgets to add
# the frame's own position still passes when the frame is at (0,0), and fails here.
FRAME_X, FRAME_Y = 137, 211

PARENT = """<!doctype html><meta charset=utf-8><title>fixture parent</title>
<body style="margin:0;height:2000px">
<div style="height:%dpx"></div>
<iframe id="ch" src="http://frame.test:%d/captcha-delivery/x" title="Verifying you are human"
        style="margin-left:%dpx;width:400px;height:300px;border:0"></iframe>
<script>
// A solved challenge disappears — that is what "cleared" means to the detector, so the fixture
// has to do it too, and by the same route the real one does: a message from the frame.
addEventListener('message', function(e){
  if(e.data === 'dd-solved'){ var f=document.getElementById('ch'); if(f) f.remove(); }
});
</script>
</body>""" % (FRAME_Y, FRAME_PORT, FRAME_X)

# One control, at coordinates we chose, inside a page on a different site. Everything else on
# the page is decoy: bigger boxes, other cursors, similar class names — a solver that simply
# takes "the biggest element" or "the first div" gets them and fails.
# Class names taken from the REAL vendors, not invented. The first version of this fixture
# used names this repo made up, so the scorer was only ever tested against its own assumptions
# — and against a live DataDome page it picked the "contact support" link, the one control on a
# hard-block page. A fixture that agrees with you teaches you nothing.
CONTROLS = {
    "checkbox":   '<div role="checkbox" aria-label="I am human" class="recaptcha-checkbox"',
    "press-hold": '<div id="px-captcha" ',
    "slider":     '<div class="sl-h" ',
    "button":     '<button class="verify-btn" ',
    # a hard block: text, an IP, and support — no solvable control anywhere
    "blocked":    '<button class="captcha__contact_support__submit" ',
}
LABEL = {"checkbox": "", "press-hold": "Press &amp; Hold", "slider": "", "button": "Verify",
         "blocked": "Contact customer service"}
# A handle INSIDE a track that shares its class prefix — the real DataDome shape. The scorer
# used to return the wrapper here (its tie-break preferred the LARGER element), which starts the
# drag in the middle of the track instead of on the handle. Rendered separately so the control
# template keeps a fixed argument order.
WRAPPER = ('<div class="captcha__human__slider sliderTrack" style="position:absolute;'
           'left:%dpx;top:%dpx;width:280px;height:100px;background:#eee">'
           'slide right to secure your access</div>' % (max(0, CX - 40), max(0, CY - 30))
           if KIND == "slider" else "")
# A working slider: press the handle, drag it, release near the target. Pointer events, capture,
# and a tolerance — the mechanics a real widget uses. It does not score the MOTION (only the
# vendor can do that); it proves the gesture lands, moves the handle, and that the clear is seen.
LIVE = """
<div class="captcha__human__slider sliderTrack" style="position:absolute;left:%dpx;top:%dpx;
     width:300px;height:56px;background:#eee">slide right to secure your access</div>
<div class="sliderTarget" style="position:absolute;left:%dpx;top:%dpx;width:%dpx;height:%dpx;
     background:#cfe3f5"></div>
<div class="sl-h" style="position:absolute;left:%dpx;top:%dpx;width:%dpx;height:%dpx;
     background:#4a90d9;cursor:grab"></div>
<script>
var h=document.querySelector('.sl-h'), t=document.querySelector('.sliderTarget');
var x0=%d, tx=%d, down=false, sx=0, ox=0;
h.addEventListener('pointerdown', function(e){
  down=true; sx=e.clientX; ox=parseInt(h.style.left,10); h.setPointerCapture(e.pointerId);
});
addEventListener('pointermove', function(e){
  if(!down) return;
  var nx=Math.max(x0, Math.min(tx+40, ox + (e.clientX-sx)));
  h.style.left=nx+'px';
});
addEventListener('pointerup', function(e){
  if(!down) return; down=false;
  var landed=parseInt(h.style.left,10);
  if(Math.abs(landed-tx) <= 8){ document.body.className='dd-response-page--success';
    parent.postMessage('dd-solved','*'); }
  else { h.style.left=x0+'px'; document.body.setAttribute('data-miss', String(landed-tx)); }
});
</script>
""" % (max(0, CX - 20), max(0, CY - 8), CX + 222, CY, CW, CH, CX, CY, CW, CH, CX, CX + 222)

BODY_ATTR = ('class="dd-response-page--hard-block" data-dd-response="hard-block"'
             if KIND == "blocked" else "")
BLOCK_TEXT = ("<p>Se ha detectado un uso indebido</p><p>IP: 203.0.113.9</p>"
              if KIND == "blocked" else "")

if KIND == "live-slider":
    FRAME = ("""<!doctype html><meta charset=utf-8><title>challenge</title>
<body style="margin:0;font:14px sans-serif">
  <div style="position:absolute;left:5px;top:5px;width:390px;height:60px;background:#eee">decoy banner</div>
""" + LIVE + "</body>")
else:
    FRAME = """<!doctype html><meta charset=utf-8><title>challenge</title>
<body %s style="margin:0;font:14px sans-serif">%s%s
  <div style="position:absolute;left:5px;top:5px;width:390px;height:60px;background:#eee">decoy banner, bigger than the control</div>
  <div style="position:absolute;left:20px;top:240px;width:360px;height:40px;background:#f6f6f6">decoy footer</div>
  <span style="position:absolute;left:10px;top:80px;cursor:pointer">decoy pointer text</span>
  %s style="position:absolute;left:%dpx;top:%dpx;width:%dpx;height:%dpx;background:#4a90d9;cursor:%s">%s</%s>
</body>""" % (BODY_ATTR, BLOCK_TEXT, WRAPPER, CONTROLS[KIND], CX, CY, CW, CH,
              "grab" if KIND == "slider" else "pointer", LABEL[KIND],
              "button" if KIND in ("button", "blocked") else "div")


def serve(srv, body_for):
    """Attach the real handler to an ALREADY-BOUND server and run it."""
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            b = body_for(self.path).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        def log_message(self, *a):
            pass
    srv.RequestHandlerClass = H
    srv.serve_forever()


threading.Thread(target=serve, args=(PARENT_SRV, lambda p: PARENT), daemon=True).start()
threading.Thread(target=serve, args=(FRAME_SRV, lambda p: FRAME), daemon=True).start()

# The answer, so the test never has to recompute it: page coords of the control's CENTRE.
# parent_port/frame_port are the ports actually bound — the caller must use these rather
# than whatever it asked for, which is the whole point of passing 0.
print(json.dumps({
    "parent": "http://parent.test:%d/" % PARENT_PORT,
    "parent_port": PARENT_PORT, "frame_port": FRAME_PORT,
    "frame_x": FRAME_X, "frame_y": FRAME_Y,
    "expect_x": FRAME_X + CX + CW // 2,
    "expect_y": FRAME_Y + CY + CH // 2,
    "kind": KIND,
}), flush=True)
threading.Event().wait()
