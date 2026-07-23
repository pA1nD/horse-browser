"""horse-harness trusted input — real key/mouse events, easy-challenge gestures.

Formerly agent-input.py (installed as workspace horse_input.py); now a first-class
module. WHY IT'S NOT JUST STEALTH — it's correctness. Sites bind real logic to real
events (keyup/input enable submit buttons and update framework state; mousedown opens
menus). insertText sets the value but fires NO key events; `el.value=` fires nothing;
`el.click()` fires only `click`. These verbs fire the real events, so pages behave.
(Bot-detector realness rides along for free.)

  click(css)              trusted mousedown->mouseup->click at the element's center
  click_xy(x, y)          trusted click at viewport coords (shadow DOM / cross-iframe)
  type_into(css, text)    focus + real per-char keyDown/keyUp (fires keyup/input/change)
  type_text(text)         types the focused element with REAL key events
  press(name, times=1)    a trusted named key (Enter, Tab, Escape, Arrow*, Backspace)
  press_hold(css_or_xy,s) trusted press-and-hold (Press & Hold challenges)
  drag(css_or_xy, to=/dx=) trusted drag (slide-to-verify)
  solve_challenge(act=1)  classify a challenge -> solve the EASY ones, or escalate
  insert_text_fast(text)  escape hatch: raw Input.insertText, NO key events
"""
import math as _im
import random as _ir
import time as _it
import json as _ij
import sys as _isys

from .helpers import cdp

_mouse = {"x": 240.0, "y": 240.0}


def _eval(expr):
    return (cdp("Runtime.evaluate", expression=expr, returnByValue=True).get("result") or {}).get("value")


def _center(css):
    """Viewport-center (CSS px) of the first `css` match, scrolled into view; None if absent/hidden."""
    expr = ("(function(){var e=document.querySelector(" + _ij.dumps(css) + ");if(!e)return null;"
            "try{e.scrollIntoView({block:'center',inline:'center'});}catch(_){}"
            "var b=e.getBoundingClientRect();if(b.width===0&&b.height===0)return null;"
            "return [b.x+b.width/2,b.y+b.height/2];})()")
    return _eval(expr)


def _focus(css):
    return bool(_eval("(function(){var e=document.querySelector(" + _ij.dumps(css) + ");if(!e)return false;e.focus();return true;})()"))


# ── mouse ────────────────────────────────────────────────────────────────────────
def _move(x, y):
    cdp("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y)
    _mouse["x"], _mouse["y"] = x, y


def click_xy(x, y):
    """Trusted left click at viewport coords — the full mousedown/mouseup/click (+pointer)
    chain, so the page reacts exactly as it would to a person (unlike el.click())."""
    _move(x, y)
    _it.sleep(_ir.uniform(0.02, 0.06))
    cdp("Input.dispatchMouseEvent", type="mousePressed", x=x, y=y, button="left", clickCount=1)
    _it.sleep(_ir.uniform(0.03, 0.08))
    cdp("Input.dispatchMouseEvent", type="mouseReleased", x=x, y=y, button="left", clickCount=1)


def click(css):
    """Trusted click at the center of `css`. Never el.click() — this fires the whole
    event chain (mousedown/mouseup/pointer/click) so menus, widgets and validation run."""
    c = _center(css)
    if not c:
        raise RuntimeError("click: no visible element " + css)
    click_xy(c[0], c[1])


def _pt(target):
    """Resolve a gesture target to viewport (x, y): a CSS selector (its centre) OR an (x, y)
    tuple you read off a screenshot — the tuple is how you drive a control sealed inside a
    cross-origin iframe, where querySelector can't reach but CDP input still lands on the pixel."""
    if isinstance(target, (tuple, list)) and len(target) == 2:
        return float(target[0]), float(target[1])
    c = _center(target)
    return (float(c[0]), float(c[1])) if c else None


def press_hold(target, seconds=6.0):
    """Trusted press-and-hold at `target` (CSS selector or (x, y) coords) — for Press & Hold
    challenges (PerimeterX/DataDome). A REAL hold is almost perfectly STILL: measured against a
    human, the hand emits only a handful of pointermove events over the whole hold, at wildly
    irregular times (~5 moves, inter-event timing CV ~1.1). A continuous jitter loop is the
    opposite — a metronomic ~15 Hz stream (CV ~0.06) that reads as a machine instantly. So we
    press, then STAY PUT, with just a few small, irregularly-timed micro-nudges; the pressed
    bitmask stays held on every event so the widget never cancels the hold."""
    c = _pt(target)
    if not c:
        raise RuntimeError("press_hold: no visible element " + str(target))
    x, y = c
    _move(x, y)
    cdp("Input.dispatchMouseEvent", type="mousePressed", x=x, y=y, button="left", buttons=1, clickCount=1)
    t0 = _it.time(); end = t0 + seconds
    nudges = sorted(_ir.uniform(0.4, max(0.5, seconds - 0.3)) for _ in range(_ir.randint(2, 4)))
    for nt in nudges:                                          # a few irregular micro-tremors, else still
        dt = (t0 + nt) - _it.time()
        if dt > 0:
            _it.sleep(dt)
        cdp("Input.dispatchMouseEvent", type="mouseMoved", x=x + _ir.uniform(-2.0, 2.0), y=y + _ir.uniform(-2.0, 2.0), buttons=1)
    rem = end - _it.time()
    if rem > 0:
        _it.sleep(rem)
    cdp("Input.dispatchMouseEvent", type="mouseReleased", x=x, y=y, button="left", buttons=0, clickCount=1)


def drag(target, to=None, dx=None, dy=0):
    """Trusted drag from `target` (CSS selector or (x, y) start coords) — for slide-to-verify.
    Give an absolute `to`=(x,y) target, or a relative `dx`/`dy`. Moves in small held-button
    steps (ease-in-out + tiny jitter, pressed bitmask held) so the site sees a real drag."""
    c = _pt(target)
    if not c:
        raise RuntimeError("drag: no visible element " + str(target))
    x0, y0 = c
    x1, y1 = (to if to else (x0 + (dx or 0), y0 + dy))
    _move(x0, y0)
    cdp("Input.dispatchMouseEvent", type="mousePressed", x=x0, y=y0, button="left", buttons=1, clickCount=1)
    _it.sleep(_ir.uniform(0.05, 0.12))
    steps = max(14, int(_im.hypot(x1 - x0, y1 - y0) / 10))
    for i in range(1, steps + 1):
        t = i / steps
        e = t * t * (3 - 2 * t)                                   # smoothstep ease
        px = x0 + (x1 - x0) * e + (_ir.uniform(-1.0, 1.0) if i < steps else 0)
        py = y0 + (y1 - y0) * e + (_ir.uniform(-1.0, 1.0) if i < steps else 0)
        cdp("Input.dispatchMouseEvent", type="mouseMoved", x=px, y=py, buttons=1)
        _it.sleep(_ir.uniform(0.008, 0.022))
    cdp("Input.dispatchMouseEvent", type="mouseReleased", x=x1, y=y1, button="left", buttons=0, clickCount=1)
    _mouse["x"], _mouse["y"] = x1, y1


# ── keyboard ─────────────────────────────────────────────────────────────────────
# printable symbol -> (code, windowsVirtualKeyCode, needs_shift) on a US-QWERTY layout, so a
# shifted char ('$', '@', '?', an uppercase letter) is typed with a real Shift keydown held —
# event.shiftKey is then true, exactly as a physical keyboard produces it. Typing '$' or 'A'
# with shiftKey=false is physically impossible and is a tell that input inspectors flag.
_SYM = {'`': ('Backquote', 192, False),    '~': ('Backquote', 192, True),
        '-': ('Minus', 189, False),        '_': ('Minus', 189, True),
        '=': ('Equal', 187, False),        '+': ('Equal', 187, True),
        '[': ('BracketLeft', 219, False),  '{': ('BracketLeft', 219, True),
        ']': ('BracketRight', 221, False), '}': ('BracketRight', 221, True),
        '\\': ('Backslash', 220, False),   '|': ('Backslash', 220, True),
        ';': ('Semicolon', 186, False),    ':': ('Semicolon', 186, True),
        "'": ('Quote', 222, False),        '"': ('Quote', 222, True),
        ',': ('Comma', 188, False),        '<': ('Comma', 188, True),
        '.': ('Period', 190, False),       '>': ('Period', 190, True),
        '/': ('Slash', 191, False),        '?': ('Slash', 191, True),
        '!': ('Digit1', 49, True),  '@': ('Digit2', 50, True),  '#': ('Digit3', 51, True),
        '$': ('Digit4', 52, True),  '%': ('Digit5', 53, True),  '^': ('Digit6', 54, True),
        '&': ('Digit7', 55, True),  '*': ('Digit8', 56, True),  '(': ('Digit9', 57, True),
        ')': ('Digit0', 48, True),  ' ': ('Space', 32, False)}
_SPECIAL = {'Enter': ('Enter', 13, '\r'), 'Tab': ('Tab', 9, '\t'), 'Backspace': ('Backspace', 8, ''),
            'Escape': ('Escape', 27, ''), 'Delete': ('Delete', 46, ''), 'Space': ('Space', 32, ' '),
            'ArrowDown': ('ArrowDown', 40, ''), 'ArrowUp': ('ArrowUp', 38, ''),
            'ArrowLeft': ('ArrowLeft', 37, ''), 'ArrowRight': ('ArrowRight', 39, '')}


def _keyinfo(ch):
    if ch.isalpha():
        u = ch.upper()
        return (ch, 'Key' + u, ord(u), ch.isupper())          # Shift for capitals
    if ch.isdigit():
        return (ch, 'Digit' + ch, ord(ch), False)
    if ch in _SYM:
        code, vk, shift = _SYM[ch]
        return (ch, code, vk, shift)
    return (ch, '', 0, False)


def _key(ch):
    key, code, vk, shift = _keyinfo(ch)
    base = dict(key=key, code=code, windowsVirtualKeyCode=vk, nativeVirtualKeyCode=vk)
    # keyDown WITH text makes Chrome actually insert the char (fires a native `input`
    # event); keyUp without text. Real, fully-formed events → site keyup/input listeners fire.
    if shift:
        # hold a real Shift keydown around the char so event.shiftKey is true, like a keyboard
        sk = dict(key='Shift', code='ShiftLeft', windowsVirtualKeyCode=16, nativeVirtualKeyCode=16)
        cdp("Input.dispatchKeyEvent", type="keyDown", modifiers=8, **sk)
        cdp("Input.dispatchKeyEvent", type="keyDown", text=ch, modifiers=8, **base)
        cdp("Input.dispatchKeyEvent", type="keyUp", modifiers=8, **base)
        cdp("Input.dispatchKeyEvent", type="keyUp", **sk)
    else:
        cdp("Input.dispatchKeyEvent", type="keyDown", text=ch, **base)
        cdp("Input.dispatchKeyEvent", type="keyUp", **base)


def press(name, times=1):
    """Press a named key with a real, trusted key event: Enter, Tab, Escape, Backspace,
    Delete, Space, Arrow{Up,Down,Left,Right}."""
    code, vk, txt = _SPECIAL[name]
    base = dict(key=code, code=code, windowsVirtualKeyCode=vk, nativeVirtualKeyCode=vk)
    for _ in range(times):
        cdp("Input.dispatchKeyEvent", type="keyDown", **(dict(base, text=txt) if txt else base))
        cdp("Input.dispatchKeyEvent", type="keyUp", **base)
        _it.sleep(0.03)


def _clear_focused():
    mods = 4 if _isys.platform == "darwin" else 2                 # Cmd on macOS, Ctrl elsewhere
    sa = dict(key='a', code='KeyA', windowsVirtualKeyCode=65, nativeVirtualKeyCode=65, modifiers=mods)
    cdp("Input.dispatchKeyEvent", type="rawKeyDown", **sa)
    cdp("Input.dispatchKeyEvent", type="keyUp", **sa)
    press("Delete")


def type_into(css, text, per=0.0, clear=False, enter=False):
    """Type `text` into `css` with REAL per-char key events so keyup/input/change fire —
    enabling submit buttons, triggering autocompletes, updating framework state. Fast by
    default (per=0); pass per>0 for a light cadence, or use the Tier 3 human_* helpers for
    full human timing. clear=True empties the field first; enter=True presses Enter after."""
    if not _focus(css):
        raise RuntimeError("type_into: no element " + css)
    if clear:
        _clear_focused()
    for ch in text:
        _key(ch)
        if per:
            _it.sleep(per)
    if enter:
        press("Enter")


def type_text(text):
    """OVERRIDE of the stock browser-harness typer. Stock type_text used Input.insertText,
    which sets the value but fires NO key events — so keyup/input listeners never run and
    the page silently misbehaves (submit stays disabled, autocomplete dead, React state
    stale). This types the currently-focused element with REAL key events instead."""
    for ch in text:
        _key(ch)


def insert_text_fast(text):
    """The old fast path: Input.insertText in one shot. Fires NO key events — use ONLY for
    dumping into a plain <textarea> with no keyup/input listeners, where speed matters."""
    cdp("Input.insertText", text=text)


# ── easy-challenge solving — a gesture, never perception ───────────────────────────
# Classify what's on the page. EASY = something a trusted gesture clears with no
# understanding of content (checkbox, press-&-hold, slide-to-verify). HARD = anything
# needing to perceive content (pick images, read distorted text, rotate, audio) — we
# NEVER guess at those; we say escalate. Detection is heuristic (best-effort DOM sniff).
_DETECT_JS = r"""
(() => {
  const q = (s) => document.querySelector(s);
  const txt = (document.body ? document.body.innerText : '').toLowerCase();
  const seen = (...ss) => ss.find(s => q(s));
  // An image/interactive challenge popup that's OPEN — reCAPTCHA bframe or hCaptcha
  // challenge iframe. It's a top-document iframe (cross-origin, can't read inside) but
  // we can see it's visibly expanded. That means a checkbox already escalated to the
  // perception kind → hard, escalate (don't re-report the checkbox behind it).
  const pop = q('iframe[src*="recaptcha/api2/bframe"], iframe[src*="hcaptcha.com/captcha"][title*="hallenge"], iframe[title*="recaptcha challenge"]');
  if (pop) { const pb = pop.getBoundingClientRect(); if (pb.height > 120 && pb.width > 120 && getComputedStyle(pop).visibility !== 'hidden') return {kind:'hard', why:'image challenge popup is open'}; }
  // HARD first — if a perception challenge is present, don't attempt a gesture.
  const hardTxt = /select all|click each|images? (with|containing)|type the (characters|text)|what does this say|rotate|listen and|audio challenge/;
  if (hardTxt.test(txt) || q('table.rc-imageselect-table') || q('.geetest_item_wrap')) return {kind:'hard', why:'image/text/audio challenge'};
  // Press & Hold (PerimeterX / DataDome). The button is usually inside a CROSS-ORIGIN iframe
  // (unselectable), and #px-captcha is the MODAL — its center sits ~90px ABOVE the button, so a
  // selector-hold on the container misses. PX centers the modal; the button is at ~(0.486w,
  // 0.553h) of the viewport — hold THERE (verified live). Same-origin button → use its rect.
  if (/press\s*&?\s*and?\s*hold|press and hold/.test(txt) || q('#px-captcha') || q('[id*="px-captcha"]')) {
    const el = q('#px-captcha [role=button]');
    if (el) { const r = el.getBoundingClientRect(); return {kind:'hold', sel:_sel(el), xy:[Math.round(r.left+r.width/2), Math.round(r.top+r.height/2)], why:'press & hold'}; }
    return {kind:'hold', sel:'#px-captcha', xy:[Math.round(0.486*innerWidth), Math.round(0.553*innerHeight)], why:'press & hold (button in sealed iframe → coord)'};
  }
  // Slider / slide-to-verify
  const slider = q('.slider, [class*="slide"] [class*="btn"], [class*="drag"][class*="btn"], .yidun_slider, .nc_iconfont');
  if (/slide to|drag the slider|slide right|slide to verify/.test(txt) || slider)
    return {kind:'drag', sel: slider ? _sel(slider) : null, why:'slide to verify'};
  // Checkbox captchas (reCAPTCHA / hCaptcha / Turnstile) — usually a cross-origin iframe.
  if (q('iframe[src*="recaptcha/api2/anchor"]') || q('iframe[title*="hCaptcha"]') || q('iframe[src*="challenges.cloudflare.com"]') || q('.cf-turnstile') || q('.g-recaptcha') || q('.h-captcha'))
    return {kind:'checkbox', why:'checkbox captcha (in an iframe — click its coords)'};
  function _sel(e){ if(e.id) return '#'+CSS.escape(e.id); if(e.className && typeof e.className==='string'){const c=e.className.trim().split(/\s+/)[0]; if(c) return e.tagName.toLowerCase()+'.'+CSS.escape(c);} return e.tagName.toLowerCase(); }
  return {kind:'none'};
})()
"""


def _vision_shot():
    """Capture the viewport to a UNIQUE png (never the shared shot.png) and return its path, so
    the driving agent can Read it and locate the sealed control by eye."""
    try:
        import os as _os, base64 as _b64
        d = _os.path.expanduser("~/.config/browser-harness/tmp")
        _os.makedirs(d, exist_ok=True)
        p = _os.path.join(d, "challenge-%d.png" % int(_it.time() * 1000))
        data = (cdp("Page.captureScreenshot", format="png") or {}).get("data")
        if data:
            open(p, "wb").write(_b64.b64decode(data))
            return p
    except Exception:
        pass
    return None


def _xorigin_challenge():
    """A visible cross-origin challenge iframe (DOM sealed) → {vendor,x,y,w,h} or None. We can
    read the iframe ELEMENT's rect from the top document even though its contents are off-limits;
    that rect tells the agent where on screen to look."""
    return _eval(r"""(function(){
      var pats=[['cloudflare','challenges.cloudflare.com'],['datadome','captcha-delivery'],
                ['hcaptcha','hcaptcha.com'],['perimeterx','perimeterx'],['arkose','arkoselabs'],
                ['recaptcha','recaptcha/api2/bframe'],['recaptcha','recaptcha/api2/anchor']];
      var fr=[].slice.call(document.querySelectorAll('iframe'));
      for(var i=0;i<fr.length;i++){var f=fr[i],s=f.src||'';
        for(var j=0;j<pats.length;j++){ if(s.indexOf(pats[j][1])>=0){
          var b=f.getBoundingClientRect();
          if(b.width>40&&b.height>20&&getComputedStyle(f).visibility!=='hidden')
            return {vendor:pats[j][0],x:Math.round(b.x),y:Math.round(b.y),w:Math.round(b.width),h:Math.round(b.height)};
        }}}
      return null;})()""")


# per-vendor READING hint for the agent's eyes — a starting guess, NEVER authoritative coords
# (widgets get redesigned; you always look at the screenshot and verify the result instead).
_VISION_HINT = {
    "cloudflare": "a checkbox near the left of the widget — click_xy(x, y) it",
    "datadome": "slide-to-verify: read the handle (left of the track) and the track's right end, then drag((hx, hy), to=(ex, ey))",
    "perimeterx": "a Press & Hold button — press_hold((x, y), 6)",
    "hcaptcha": "a checkbox — click_xy(x, y); if an image grid opens after, that's the HARD kind → escalate",
    "recaptcha": "a checkbox — click_xy(x, y); if an image grid opens after, that's HARD → escalate",
    "arkose": "a FunCaptcha (pick/rotate images) — HARD; escalate, don't guess",
}


def _vision_handoff(xo, kind_hint=None):
    shot = _vision_shot()
    rect = ("iframe at rect(x=%d, y=%d, w=%d, h=%d)" % (xo["x"], xo["y"], xo["w"], xo["h"])) if xo else "the challenge widget"
    vendor = (xo or {}).get("vendor") or (kind_hint or "unknown")
    hint = _VISION_HINT.get((xo or {}).get("vendor"), "read the control (checkbox / slider / press-hold) and act by its coordinates")
    return ("vision:%s — challenge sealed in a cross-origin iframe; the DOM can't be read, but CDP "
            "input passes through by coordinate. %s. Hint: %s. Screenshot: %s. Act by COORDS "
            "(click_xy / press_hold((x,y),s) / drag((x,y),to=(x2,y2))), then confirm with "
            "challenge_cleared(); if it didn't clear, re-screenshot and adjust."
            % (vendor, rect, hint, shot or "capture one with capture_screenshot()"))


def challenge_cleared():
    """Close the loop after a vision-driven gesture: read the top-document side-effects a
    cross-origin challenge leaves when it passes. 'cleared:<signal>' or 'still:<why>'."""
    r = _eval(r"""(function(){
      var cf=document.querySelector('input[name=cf-turnstile-response]');
      var rc=document.querySelector('textarea[name=g-recaptcha-response],#g-recaptcha-response');
      var f=document.querySelector('iframe[src*="challenges.cloudflare.com"],iframe[src*="captcha-delivery"],iframe[src*="perimeterx"],iframe[src*="hcaptcha.com/captcha"],iframe[src*="recaptcha/api2/bframe"],#px-captcha');
      return {cf: cf?(cf.value||'').length:-1, rc: rc?(rc.value||'').length:-1, iframe: !!f};
    })()""") or {}
    if (r.get("cf") or -1) > 20:
        return "cleared:turnstile token present (%d chars)" % r["cf"]
    if (r.get("rc") or -1) > 20:
        return "cleared:recaptcha token present (%d chars)" % r["rc"]
    if not r.get("iframe"):
        return "cleared:challenge iframe gone"
    return ("still:challenge iframe present, no token yet — after a Press & Hold, PX shows a "
            "'●●●' spinner for a few seconds before it proceeds, so poll a few more seconds; "
            "if still stuck, re-screenshot and adjust, or escalate")


def solve_challenge(act=True, hold_seconds=7.0):
    """Detect a challenge and clear it. A same-document gesture (Press & Hold on #px-captcha, a
    slider element we can select) is solved directly and verified. Anything sealed in a
    cross-origin iframe (Turnstile, DataDome, hCaptcha) can't be reached by querySelector, so
    VISION is primary: this returns a 'vision:...' brief with a screenshot + the iframe rect —
    you read the control and act by coordinates, then confirm with challenge_cleared(). HARD
    perception (identify images, read text, rotate, audio) returns 'escalate:'. 'none' if clear.
    act=False = classify only."""
    d = _eval(_DETECT_JS) or {"kind": "none"}
    kind, sel, why = d.get("kind"), d.get("sel"), d.get("why", "")
    if kind == "hard":
        return "escalate:" + why + " — needs perception; ask the operator, don't guess"
    if not act:
        if kind != "none":
            return "easy:%s (%s) sel=%s" % (kind, why, sel)
        xo = _xorigin_challenge()
        return ("easy:cross-origin %s challenge (sealed iframe) — solve by vision" % xo["vendor"]) if xo else "none"
    # a gesture we can aim (a selectable element, or a coord DETECT computed) → do it, then verify
    if kind in ("hold", "drag") and (sel or d.get("xy")):
        try:
            if kind == "hold":
                target = tuple(d["xy"]) if d.get("xy") else sel
                press_hold(target, seconds=hold_seconds)
                # PX turns the button solid blue with a "●●●" processing spinner for a few seconds
                # AFTER a good hold, before the page proceeds — poll, don't judge the first read.
                for _ in range(9):
                    _it.sleep(1.0)
                    res = challenge_cleared()
                    if res.startswith("cleared"):
                        return "solved:hold %s — %s" % (target, res)
                # held but didn't clear → hand to vision so the agent can look and adjust by eye
                return _vision_handoff(_xorigin_challenge(), "perimeterx")
            drag(sel, dx=320)                                    # simple sliders latch at the right
            _it.sleep(2.0)
            return "solved:drag %s — %s" % (sel, challenge_cleared())
        except Exception as e:
            return "escalate:gesture failed (%r) — ask the operator" % (e,)
    # sealed cross-origin (checkbox iframe, a slider/hold we couldn't select, or one DETECT
    # missed) → hand to vision; never guess pixels from a table.
    xo = _xorigin_challenge()
    if xo or kind in ("checkbox", "hold", "drag"):
        return _vision_handoff(xo, kind)
    return "none"
