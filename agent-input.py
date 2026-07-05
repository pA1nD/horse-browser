# horse-browser Tier 2 — trusted, correct input (installed as horse_input.py).
#
# Loaded by horse_helpers.py, which execs this sibling — so every agent that drives
# the browser gets these by default. This is the AGENT LAYER of realness: input sent
# over CDP that fires the SAME events a real browser would, applied on every site.
#
# WHY IT'S NOT JUST STEALTH — it's correctness. Sites bind real logic to real events:
#   • keyup / keydown / input  → enable the submit button, fire autocomplete, validate,
#                                update React/Vue/Svelte controlled state.
#   • mousedown / pointerdown  → open menus, custom widgets, "close on outside mousedown".
# insertText sets the value but fires NO key events; `el.value=` fires nothing at all;
# `el.click()` fires only `click`, not the down/up/pointer chain. In every case the text
# or click *appears* to work while the page's logic never ran — a silent break that hits
# plain forms, not just defended ones. These verbs fire the real events, so pages behave.
# (Bot-detector realness rides along for free.) Human-like MOTION — bezier paths, warm-up,
# gaussian cadence — is the separate, gated Tier 3 layer; this file stays cheap and fast.
#
# Reach for these (they shadow the untrusted shortcuts):
#   click(css)              trusted mousedown->mouseup->click at the element's center
#   type_into(css, text)    focus + real per-char keyDown/keyUp (fires keyup/input/change)
#   type_text(text)         OVERRIDE of the stock insertText typer → real key events
#   press(name, times=1)    a trusted named key (Enter, Tab, Escape, Arrow*, Backspace)
#   press_hold(css, s)      trusted press-and-hold (Press & Hold challenges)
#   drag(css, to=/dx=/dy=)  trusted drag (slide-to-verify)
#   solve_challenge(act=1)  classify a challenge → solve the EASY ones (click/hold/drag),
#                           or return "escalate:<why>" for image/text/audio ones.
# Deliberate escape hatch:
#   insert_text_fast(text)  raw Input.insertText — fast, but fires NO key events; only for
#                           dumping into a plain <textarea> with no listeners.
#
# `cdp` is provided by horse_helpers.py (loaded first) — we drive everything through it.

import math as _im
import random as _ir
import time as _it
import json as _ij
import sys as _isys

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


def press_hold(css, seconds=6.0):
    """Trusted press-and-hold at `css` — for Press & Hold challenges (PerimeterX/DataDome).
    Holds the button down with tiny jitter for `seconds`, which real widgets require."""
    c = _center(css)
    if not c:
        raise RuntimeError("press_hold: no visible element " + css)
    x, y = c
    _move(x, y)
    cdp("Input.dispatchMouseEvent", type="mousePressed", x=x, y=y, button="left", clickCount=1)
    end = _it.time() + seconds
    while _it.time() < end:
        cdp("Input.dispatchMouseEvent", type="mouseMoved", x=x + _ir.uniform(-1.4, 1.4), y=y + _ir.uniform(-1.4, 1.4), button="left")
        _it.sleep(_ir.uniform(0.08, 0.2))
    cdp("Input.dispatchMouseEvent", type="mouseReleased", x=x, y=y, button="left", clickCount=1)


def drag(css, to=None, dx=None, dy=0):
    """Trusted drag from `css` — for slide-to-verify sliders. Give an absolute `to`=(x,y)
    target, or a relative `dx`/`dy`. Moves in small held-button steps (ease-in-out + tiny
    jitter) so the site sees a real pointer drag, not a teleport."""
    c = _center(css)
    if not c:
        raise RuntimeError("drag: no visible element " + css)
    x0, y0 = c
    x1, y1 = (to if to else (x0 + (dx or 0), y0 + dy))
    _move(x0, y0)
    cdp("Input.dispatchMouseEvent", type="mousePressed", x=x0, y=y0, button="left", clickCount=1)
    _it.sleep(_ir.uniform(0.05, 0.12))
    steps = max(14, int(_im.hypot(x1 - x0, y1 - y0) / 10))
    for i in range(1, steps + 1):
        t = i / steps
        e = t * t * (3 - 2 * t)                                   # smoothstep ease
        px = x0 + (x1 - x0) * e + (_ir.uniform(-1.0, 1.0) if i < steps else 0)
        py = y0 + (y1 - y0) * e + (_ir.uniform(-1.0, 1.0) if i < steps else 0)
        cdp("Input.dispatchMouseEvent", type="mouseMoved", x=px, y=py, button="left")
        _it.sleep(_ir.uniform(0.008, 0.022))
    cdp("Input.dispatchMouseEvent", type="mouseReleased", x=x1, y=y1, button="left", clickCount=1)
    _mouse["x"], _mouse["y"] = x1, y1


# ── keyboard ─────────────────────────────────────────────────────────────────────
_PUNCT = {'/': ('Slash', 191), '.': ('Period', 190), ',': ('Comma', 188), '-': ('Minus', 189),
          ' ': ('Space', 32), ';': ('Semicolon', 186), ':': ('Semicolon', 186), "'": ('Quote', 222),
          '"': ('Quote', 222), '@': ('Digit2', 50), '_': ('Minus', 189), '=': ('Equal', 187),
          '+': ('Equal', 187), '(': ('Digit9', 57), ')': ('Digit0', 48), '!': ('Digit1', 49),
          '?': ('Slash', 191), '#': ('Digit3', 51)}
_SPECIAL = {'Enter': ('Enter', 13, '\r'), 'Tab': ('Tab', 9, '\t'), 'Backspace': ('Backspace', 8, ''),
            'Escape': ('Escape', 27, ''), 'Delete': ('Delete', 46, ''), 'Space': ('Space', 32, ' '),
            'ArrowDown': ('ArrowDown', 40, ''), 'ArrowUp': ('ArrowUp', 38, ''),
            'ArrowLeft': ('ArrowLeft', 37, ''), 'ArrowRight': ('ArrowRight', 39, '')}


def _keyinfo(ch):
    if ch.isalpha():
        u = ch.upper()
        return (ch, 'Key' + u, ord(u))
    if ch.isdigit():
        return (ch, 'Digit' + ch, ord(ch))
    if ch in _PUNCT:
        code, vk = _PUNCT[ch]
        return (ch, code, vk)
    return (ch, '', 0)


def _key(ch):
    key, code, vk = _keyinfo(ch)
    base = dict(key=key, code=code, windowsVirtualKeyCode=vk, nativeVirtualKeyCode=vk)
    # keyDown WITH text makes Chrome actually insert the char (fires a native `input`
    # event); keyUp without text. Real, fully-formed events → site keyup/input listeners fire.
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
  // HARD first — if a perception challenge is present, don't attempt a gesture.
  const hardTxt = /select all|click each|images? (with|containing)|type the (characters|text)|what does this say|rotate|listen and|audio challenge/;
  if (hardTxt.test(txt) || q('table.rc-imageselect-table') || q('.geetest_item_wrap')) return {kind:'hard', why:'image/text/audio challenge'};
  // Press & Hold (PerimeterX / DataDome)
  if (/press\s*&?\s*and?\s*hold|press and hold/.test(txt) || q('#px-captcha') || q('[id*="px-captcha"]'))
    { const el = q('#px-captcha [role=button]') || q('#px-captcha') || q('[id*="press"]'); return {kind:'hold', sel: el ? _sel(el) : '#px-captcha', why:'press & hold'}; }
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


def solve_challenge(act=True, hold_seconds=6.0):
    """Detect a challenge and, if it's EASY (a trusted gesture — checkbox click, press-&-hold,
    slide-to-verify), solve it; return a short status string. For HARD challenges (identify
    images, read text, rotate, audio) it does NOT guess — it returns 'escalate:<why>' so you
    stop and ask the operator. Returns 'none' if no challenge is found. act=False = classify
    only (don't perform the gesture)."""
    d = _eval(_DETECT_JS) or {"kind": "none"}
    kind, sel, why = d.get("kind"), d.get("sel"), d.get("why", "")
    if kind == "none":
        return "none"
    if kind == "hard":
        return "escalate:" + why + " — needs perception; ask the operator, don't guess"
    if not act:
        return "easy:%s (%s) sel=%s" % (kind, why, sel)
    try:
        if kind == "hold":
            press_hold(sel, seconds=hold_seconds)
            return "solved:hold — press-held %s (verify it cleared; retry once, else escalate)" % sel
        if kind == "drag":
            if not sel:
                return "easy:drag (%s) — found a slider but couldn't pin a selector; drag it by hand with drag(sel, dx=<track width>)" % why
            c = _center(sel)
            if c:
                drag(sel, dx=320)                                # slide well to the right; simple sliders latch at the end
                return "solved:drag — slid %s right (verify; if it snapped back, escalate)" % sel
            return "easy:drag — slider not locatable; escalate if it blocks you"
        if kind == "checkbox":
            return ("easy:checkbox (%s) — it's in a cross-origin iframe. Screenshot, read the checkbox pixel, "
                    "then click_xy(x, y) (a trusted click passes through the iframe). If an image grid appears "
                    "after, that's the HARD kind — escalate." % why)
    except Exception as e:
        return "escalate:gesture failed (%r) — ask the operator" % (e,)
    return "none"
