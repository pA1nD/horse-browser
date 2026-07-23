"""Browser control via CDP.

Core helpers live here. Agent-editable helpers live in
BH_AGENT_WORKSPACE/agent_helpers.py.
"""
import base64, json, math, os, sys, time, urllib.request
from pathlib import Path
from urllib.parse import urlparse

from . import _ipc as ipc
from . import paths


CORE_DIR = Path(__file__).resolve().parent
REPO_ROOT = CORE_DIR.parent.parent
AGENT_WORKSPACE = paths.workspace_dir()


def _load_env():
    paths = [REPO_ROOT / ".env", AGENT_WORKSPACE / ".env"]
    for p in paths:
        if not p.exists():
            continue
        _load_env_file(p)


def _load_env_file(p):
    for line in p.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()

NAME = os.environ.get("BU_NAME", "default")
SOCK = ipc.sock_addr(NAME)
INTERNAL = ("chrome://", "chrome-untrusted://", "devtools://", "chrome-extension://", "about:")


def _send(req):
    c, token = ipc.connect(NAME, timeout=5.0)
    try:
        r = ipc.request(c, token, req)
    finally:
        c.close()
    if "error" in r: raise RuntimeError(r["error"])
    return r


# ── auto-home: group-aware attach, from the helper side ──────────────────────────
# The daemon binds to this session's own tab (see daemon.attach_first_page), but a
# client-side guard stays as defense in depth: if the daemon's current tab is ever
# foreign — a neighbour's tab on a shared browser — home back onto one of OUR tabs
# before acting. Cheap (a local meta call), continuous, and it means a raw cdp()/
# page_info()/js()/capture_screenshot() can never read or act on another session's page.
_homed = False
_guarding = False


def cdp(method, session_id=None, **params):
    """Raw CDP. cdp('Page.navigate', url='...'), cdp('DOM.getDocument', depth=-1)."""
    global _homed, _guarding
    if not _homed:
        _homed = True              # set BEFORE homing so re-entrant cdp() is a plain passthrough
        try: _hb_home()
        except Exception: pass     # a home failure must never break cdp()
    if not _guarding and not method.startswith("Target."):
        _guarding = True
        try: _hb_guard()
        except Exception: pass
        finally: _guarding = False
    return _send({"method": method, "params": params, "session_id": session_id}).get("result", {})


def drain_events():  return _send({"meta": "drain_events"})["events"]


def _js_snippet(expression, limit=160):
    snippet = expression.strip().replace("\n", "\\n")
    return snippet[:limit - 3] + "..." if len(snippet) > limit else snippet


def _js_exception_description(result, details):
    desc = result.get("description")
    exc = details.get("exception") if details else None
    if not desc and isinstance(exc, dict):
        desc = exc.get("description")
        if desc is None and "value" in exc:
            desc = str(exc["value"])
        if desc is None:
            desc = exc.get("className")
    if not desc and details:
        desc = details.get("text")
    return desc or "JavaScript evaluation failed"


def _decode_unserializable_js_value(value):
    if value == "NaN":
        return math.nan
    if value == "Infinity":
        return math.inf
    if value == "-Infinity":
        return -math.inf
    if value == "-0":
        return -0.0
    if value.endswith("n"):
        return int(value[:-1])
    return value


def _runtime_value(response, expression):
    result = response.get("result", {})
    details = response.get("exceptionDetails")
    if details or result.get("subtype") == "error":
        desc = _js_exception_description(result, details)
        if details:
            line = details.get("lineNumber")
            col = details.get("columnNumber")
            loc = f" at line {line}, column {col}" if line is not None and col is not None else ""
        else:
            loc = ""
        raise RuntimeError(f"JavaScript evaluation failed{loc}: {desc}; expression: {_js_snippet(expression)}")
    if "value" in result:
        return result["value"]
    if "unserializableValue" in result:
        return _decode_unserializable_js_value(result["unserializableValue"])
    return None


def _runtime_evaluate(expression, session_id=None, await_promise=False):
    try:
        r = cdp("Runtime.evaluate", session_id=session_id, expression=expression, returnByValue=True, awaitPromise=await_promise)
    except TimeoutError as e:
        raise RuntimeError(f"Runtime.evaluate timed out; expression: {_js_snippet(expression)}") from e
    return _runtime_value(r, expression)


def _wrap_js_function(expression):
    return f"(function(){{{expression}}})()"


def _is_illegal_return_error(exc):
    return "Illegal return statement" in str(exc)


# --- navigation / page ---
def goto_url(url):
    r = cdp("Page.navigate", url=url)
    if os.environ.get("BH_DOMAIN_SKILLS") != "1":
        return r
    d = (AGENT_WORKSPACE / "domain-skills" / (urlparse(url).hostname or "").removeprefix("www.").split(".")[0])
    return {**r, "domain_skills": sorted(p.name for p in d.rglob("*.md"))[:10]} if d.is_dir() else r

def page_info():
    """{url, title, w, h, sx, sy, pw, ph} — viewport + scroll + page size.

    If a native dialog (alert/confirm/prompt/beforeunload) is open, returns
    {dialog: {type, message, ...}} instead — the page's JS thread is frozen
    until the dialog is handled (see interaction-skills/dialogs.md)."""
    dialog = _send({"meta": "pending_dialog"}).get("dialog")
    if dialog:
        return {"dialog": dialog}
    expression = "JSON.stringify({url:location.href,title:document.title,w:innerWidth,h:innerHeight,sx:scrollX,sy:scrollY,pw:document.documentElement.scrollWidth,ph:document.documentElement.scrollHeight})"
    return json.loads(_runtime_evaluate(expression))

# --- input ---
_debug_click_counter = 0

def click_at_xy(x, y, button="left", clicks=1):
    if os.environ.get("BH_DEBUG_CLICKS"):
        global _debug_click_counter
        try:
            from PIL import Image, ImageDraw
            dpr = js("window.devicePixelRatio") or 1
            path = capture_screenshot(str(ipc._TMP / f"debug_click_{_debug_click_counter}.png"))
            img = Image.open(path)
            draw = ImageDraw.Draw(img)
            px, py = int(x * dpr), int(y * dpr)
            r = int(15 * dpr)
            draw.ellipse([px - r, py - r, px + r, py + r], outline="red", width=int(3 * dpr))
            draw.line([px - r - int(5 * dpr), py, px + r + int(5 * dpr), py], fill="red", width=int(2 * dpr))
            draw.line([px, py - r - int(5 * dpr), px, py + r + int(5 * dpr)], fill="red", width=int(2 * dpr))
            img.save(path)
            print(f"[debug_click] saved {path} (x={x}, y={y}, dpr={dpr})")
        except Exception as e:
            print(f"[debug_click] overlay failed: {e}")
        _debug_click_counter += 1
    cdp("Input.dispatchMouseEvent", type="mousePressed", x=x, y=y, button=button, clickCount=clicks)
    cdp("Input.dispatchMouseEvent", type="mouseReleased", x=x, y=y, button=button, clickCount=clicks)

def fill_input(selector, text, clear_first=True, timeout=0.0):
    """Fill a framework-managed input (React controlled, Vue v-model, Ember tracked).

    type_text() uses Input.insertText which bypasses framework event listeners and leaves
    submit buttons disabled. This helper focuses the element, clears it, types via real
    key events, then fires synthetic input+change events so the framework sees the update.

    Raises RuntimeError if the element is not found. Pass timeout>0 to wait for
    late-rendered elements (e.g. after a route change) before typing.
    """
    if timeout > 0:
        if not wait_for_element(selector, timeout=timeout):
            raise RuntimeError(f"fill_input: element not found: {selector!r}")
    focused = js(
        f"(()=>{{const e=document.querySelector({json.dumps(selector)});"
        f"if(!e)return false;e.focus();return true;}})()"
    )
    if not focused:
        raise RuntimeError(f"fill_input: element not found: {selector!r}")
    if clear_first:
        # Dispatch select-all directly — NOT via press_key, which always emits a
        # `char` event for single-char keys. With Ctrl/Cmd held, that `char`
        # makes Chrome treat the input as a printable "a" instead of firing the
        # select-all shortcut, leaving the field uncleared.
        mods = 4 if sys.platform == "darwin" else 2  # Cmd on macOS, Ctrl elsewhere
        select_all = {"key": "a", "code": "KeyA", "modifiers": mods,
                      "windowsVirtualKeyCode": 65, "nativeVirtualKeyCode": 65}
        cdp("Input.dispatchKeyEvent", type="rawKeyDown", **select_all)
        cdp("Input.dispatchKeyEvent", type="keyUp", **select_all)
        press_key("Backspace")
    for ch in text:
        press_key(ch)
    js(
        f"(()=>{{const e=document.querySelector({json.dumps(selector)});"
        f"if(!e)return;"
        f"e.dispatchEvent(new Event('input',{{bubbles:true}}));"
        f"e.dispatchEvent(new Event('change',{{bubbles:true}}));}})();"
    )

_KEYS = {  # key → (windowsVirtualKeyCode, code, text)
    "Enter": (13, "Enter", "\r"), "Tab": (9, "Tab", "\t"), "Backspace": (8, "Backspace", ""),
    "Escape": (27, "Escape", ""), "Delete": (46, "Delete", ""), " ": (32, "Space", " "),
    "ArrowLeft": (37, "ArrowLeft", ""), "ArrowUp": (38, "ArrowUp", ""),
    "ArrowRight": (39, "ArrowRight", ""), "ArrowDown": (40, "ArrowDown", ""),
    "Home": (36, "Home", ""), "End": (35, "End", ""),
    "PageUp": (33, "PageUp", ""), "PageDown": (34, "PageDown", ""),
}
def press_key(key, modifiers=0):
    """Modifiers bitfield: 1=Alt, 2=Ctrl, 4=Meta(Cmd), 8=Shift.
    Special keys (Enter, Tab, Arrow*, Backspace, etc.) carry their virtual key codes
    so listeners checking e.keyCode / e.key all fire."""
    vk, code, text = _KEYS.get(key, (ord(key[0]) if len(key) == 1 else 0, key, key if len(key) == 1 else ""))
    base = {"key": key, "code": code, "modifiers": modifiers, "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk}
    shortcut_modifiers = modifiers & (1 | 2 | 4)  # Alt/Ctrl/Meta turn single keys into shortcuts.
    printable_char = len(key) == 1 and bool(text) and not shortcut_modifiers
    cdp("Input.dispatchKeyEvent", type="keyDown", **base, **({} if printable_char or not text else {"text": text}))
    if printable_char:
        cdp("Input.dispatchKeyEvent", type="char", text=text, **{k: v for k, v in base.items() if k != "text"})
    cdp("Input.dispatchKeyEvent", type="keyUp", **base)

def scroll(x, y, dy=-300, dx=0):
    cdp("Input.dispatchMouseEvent", type="mouseWheel", x=x, y=y, deltaX=dx, deltaY=dy)


# --- visual ---
# The public capture_screenshot lives in the horse layer below (per-call unique file,
# fresh per-target CDP session). This is the plain daemon-session capture it falls
# back to when there's no session identity / no bound tab.
def _capture_screenshot_stock(path=None, full=False, max_dim=None):
    path = path or str(ipc._TMP / "shot.png")
    r = cdp("Page.captureScreenshot", format="png", captureBeyondViewport=full)
    open(path, "wb").write(base64.b64decode(r["data"]))
    if max_dim:
        from PIL import Image
        img = Image.open(path)
        if max(img.size) > max_dim:
            img.thumbnail((max_dim, max_dim))
            img.save(path)
    return path


# --- tabs ---
def _is_agent_startup_placeholder(title, url):
    url = str(url or "")
    return str(title or "").startswith("Starting agent ") and (
        url in ("", "about:blank") or url.startswith("about:blank#")
    )


def list_tabs(include_chrome=True):
    out = []
    for t in cdp("Target.getTargets")["targetInfos"]:
        if t["type"] != "page": continue
        url = t.get("url", "")
        if _is_agent_startup_placeholder(t.get("title", ""), url): continue
        if not include_chrome and url.startswith(INTERNAL): continue
        out.append({
            "targetId": t["targetId"],
            "target_id": t["targetId"],
            "title": t.get("title", ""),
            "url": url,
        })
    return out

def current_tab():
    r = _send({"meta": "current_tab"})
    return {
        "targetId": r["targetId"],
        "target_id": r["targetId"],
        "url": r["url"],
        "title": r["title"],
    }

def _mark_tab():
    """Prepend horse emoji to tab title so the user can see which tab the agent controls."""
    try: cdp("Runtime.evaluate", expression="if(!document.title.startsWith('\U0001F434'))document.title='\U0001F434 '+document.title")
    except Exception: pass

def switch_tab(target):
    """Focus-safe tab switch. Accepts a targetId string or a current_tab()/list_tabs()
    dict; switches the DRIVEN tab without Target.activateTarget — no [NSApp activate],
    no visible-tab flip, so concurrent agents and the operator are never disturbed."""
    target_id = (target.get("targetId") or target.get("target_id")) if isinstance(target, dict) else target
    bh_switch_tab(target_id)
    return target_id

def new_tab(url="about:blank"):
    """Focus-safe: opens into this session's tab group in the background, like bh_open."""
    return bh_open(url)

def close_tab(target=None):
    """Close a tab. If `target` is omitted, closes the currently attached tab.
    Accepts a raw targetId string or a dict from list_tabs()/current_tab()."""
    target_id = (target.get("targetId") or target.get("target_id")) if isinstance(target, dict) else target
    if target_id is None:
        target_id = current_tab()["targetId"]
    cdp("Target.closeTarget", targetId=target_id)


def ensure_real_tab():
    """Focus-safe: keep the current tab if it's a real (non-internal) OWN tab;
    otherwise home onto one of this session's tabs via the focus-safe switch."""
    try:
        cur = current_tab() or {}
    except Exception:
        cur = {}
    own_ids = {t.get("targetId") for t in bh_list()} if _session_id() else set()
    if cur.get("url") and not cur["url"].startswith(INTERNAL) and \
       (not _session_id() or cur.get("targetId") in own_ids):
        return cur
    own = [t for t in bh_list() if t.get("targetId")] if _session_id() else []
    if own:
        bh_switch_tab(own[-1]["targetId"])
        return own[-1]
    return None

def iframe_target(url_substr):
    """First iframe target whose URL contains `url_substr`. Use with js(..., target_id=...)."""
    for t in cdp("Target.getTargets")["targetInfos"]:
        if t["type"] == "iframe" and url_substr in t.get("url", ""):
            return t["targetId"]
    return None


# --- utility ---
def wait(seconds=1.0):
    time.sleep(seconds)

def wait_for_load(timeout=15.0):
    """Poll document.readyState == 'complete' or timeout. Fires hints.d page hints
    (the "I just navigated" plug point) before returning."""
    deadline = time.time() + timeout
    ok = False
    while time.time() < deadline:
        if js("document.readyState") == "complete":
            ok = True
            break
        time.sleep(0.3)
    try:
        _hb_hints((current_tab() or {}).get("url") or "")
    except Exception:
        pass
    return ok

def wait_for_element(selector, timeout=10.0, visible=False):
    """Poll until querySelector(selector) exists in the DOM, or timeout.

    wait_for_load() misses SPAs — the document is 'complete' before the framework renders.
    Use this after actions that trigger async rendering (route changes, data fetches).
    Set visible=True to also require the element to be non-hidden and in-layout.
    Returns True if found, False on timeout.
    """
    if visible:
        # checkVisibility walks the ancestor chain and respects display:none /
        # visibility:hidden / opacity:0 on parents, which a getComputedStyle
        # check on the element alone misses (it returns the descendant's own
        # style, not the inherited "is this rendered" state). Falls back to
        # the per-element CSS check on older Chrome that lacks checkVisibility.
        check = (
            f"(()=>{{const e=document.querySelector({json.dumps(selector)});"
            f"if(!e)return false;"
            f"if(typeof e.checkVisibility==='function')"
            f"return e.checkVisibility({{checkOpacity:true,checkVisibilityCSS:true}});"
            f"const s=getComputedStyle(e);"
            f"return s.display!=='none'&&s.visibility!=='hidden'&&s.opacity!=='0'}})()"
        )
    else:
        check = f"!!document.querySelector({json.dumps(selector)})"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if js(check): return True
        time.sleep(0.3)
    return False

def wait_for_network_idle(timeout=10.0, idle_ms=500):
    """Wait until all in-flight requests finish and no Network.* events arrive for idle_ms ms.

    Useful after form submits, SPA route transitions, and any action that triggers
    XHR/fetch without a visible DOM change. Builds on drain_events() — no daemon changes.
    Returns True if idle window reached, False on timeout.

    Events are filtered to the active session — a previously-attached background
    tab (e.g. a polling/SSE page the agent switched away from) keeps emitting
    Network events into the daemon's global event buffer; without this filter
    they would poison the idle check on the current tab.
    """
    deadline = time.time() + timeout
    last_activity = time.time()
    inflight = set()
    active_session = _send({"meta": "session"}).get("session_id")
    while time.time() < deadline:
        for e in drain_events():
            if e.get("session_id") != active_session:
                continue
            method = e.get("method", "")
            params = e.get("params", {})
            if method == "Network.requestWillBeSent":
                inflight.add(params.get("requestId"))
                last_activity = time.time()
            elif method in ("Network.loadingFinished", "Network.loadingFailed"):
                inflight.discard(params.get("requestId"))
                last_activity = time.time()
            elif method.startswith("Network."):
                last_activity = time.time()
        if not inflight and (time.time() - last_activity) * 1000 >= idle_ms:
            return True
        time.sleep(0.1)
    return False

def js(expression, target_id=None):
    """Run JS in the attached tab (default) or inside an iframe target (via iframe_target()).

    Expressions are evaluated as-is first. If Chrome reports an illegal top-level
    `return`, the snippet is retried inside a function wrapper, so both
    `document.title` and `const x = 1; return x` work without mis-wrapping nested
    functions that contain their own returns.
    """
    sid = cdp("Target.attachToTarget", targetId=target_id, flatten=True)["sessionId"] if target_id else None
    try:
        return _runtime_evaluate(expression, session_id=sid, await_promise=True)
    except RuntimeError as e:
        if _is_illegal_return_error(e):
            return _runtime_evaluate(_wrap_js_function(expression), session_id=sid, await_promise=True)
        raise


_KC = {"Enter": 13, "Tab": 9, "Escape": 27, "Backspace": 8, " ": 32, "ArrowLeft": 37, "ArrowUp": 38, "ArrowRight": 39, "ArrowDown": 40}


def dispatch_key(selector, key="Enter", event="keypress"):
    """Dispatch a DOM KeyboardEvent on the matched element.

    Use this when a site reacts to synthetic DOM key events on an element more reliably
    than to raw CDP input events.
    """
    kc = _KC.get(key, ord(key) if len(key) == 1 else 0)
    js(
        f"(()=>{{const e=document.querySelector({json.dumps(selector)});if(e){{e.focus();e.dispatchEvent(new KeyboardEvent({json.dumps(event)},{{key:{json.dumps(key)},code:{json.dumps(key)},keyCode:{kc},which:{kc},bubbles:true}}));}}}})()"
    )

def upload_file(selector, path):
    """Set files on a file input via CDP DOM.setFileInputFiles. `path` is an absolute filepath (use tempfile.mkstemp if needed)."""
    doc = cdp("DOM.getDocument", depth=-1)
    nid = cdp("DOM.querySelector", nodeId=doc["root"]["nodeId"], selector=selector)["nodeId"]
    if not nid: raise RuntimeError(f"no element for {selector}")
    cdp("DOM.setFileInputFiles", files=[path] if isinstance(path, str) else list(path), nodeId=nid)

def http_get(url, headers=None, timeout=20.0):
    """Pure HTTP — no browser. Use for static pages / APIs. Wrap in ThreadPoolExecutor for bulk."""
    import gzip
    h = {"User-Agent": "Mozilla/5.0", "Accept-Encoding": "gzip"}
    if headers: h.update(headers)
    with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
        data = r.read()
        if r.headers.get("Content-Encoding") == "gzip": data = gzip.decompress(data)
        return data.decode()


# ═══ horse layer — session-scoped tabs, focus-safe verbs, reliable screenshots ═══
# Formerly agent-helpers.py, merged into the workspace via a loader stub; now folded
# here so there is exactly ONE definition of every verb, in one namespace.

def _hb_current_file():
    return os.path.join(os.path.expanduser("~/.config/horse-browser/current"),
                        os.environ.get("BU_NAME", "default"))


def _hb_remember(target_id):
    # Persist the tab this session is currently driving, so _hb_home (and the daemon's
    # own bound-tab attach) can put us back on the SAME tab after any drift.
    try:
        f = _hb_current_file()
        os.makedirs(os.path.dirname(f), exist_ok=True)
        open(f, "w").write(target_id or "")
    except Exception:
        pass


def _hb_recall():
    try:
        return (open(_hb_current_file()).read().strip() or None)
    except Exception:
        return None


def _hb_guard():
    want = _hb_recall()
    if not want:
        return
    try:
        cur = (current_tab() or {}).get("targetId")
    except Exception:
        return
    if cur != want:
        _hb_home()


def _hb_home():
    # Home the daemon onto one of THIS session's own tabs whenever its current tab is
    # foreign. The daemon's bound-tab attach makes foreign attaches structurally rare;
    # this client-side guard is defense in depth. NO-OP when already on an own tab.
    if not _session_id():
        return
    own = [t for t in bh_list() if t.get("targetId")]
    own_ids = {t["targetId"] for t in own}
    try:
        ct = current_tab() or {}
    except Exception:
        ct = {}
    cur = ct.get("targetId")
    if cur and cur in own_ids:
        return                              # already on one of my own tabs — leave it be
    if not own:
        # No own tab yet. If the daemon is sitting on a fresh, ungrouped about:blank —
        # the one attach_first_page mints when it finds no bound tab — ADOPT it into
        # my group rather than leaking it and creating a second blank. Else make one.
        if cur and (ct.get("url") or "") in ("", "about:blank"):
            ext_call("groupTab", cur, _session_id())
            _hb_remember(cur)
        else:
            bh_open("about:blank")         # no adoptable blank → create + home one in my group
        return
    # Foreign: restore the EXACT tab I last drove (persisted on every bh_switch_tab).
    # Only if that tab is truly gone do we fall back to the newest own tab.
    want = _hb_recall()
    if want and want not in own_ids:
        print("\U0001F434 horse-browser: the tab you were driving has closed — switched to "
              "another of your tabs (bh_list() to see them)", file=sys.stderr)
        want = None
    if want not in own_ids:
        want = max(own, key=lambda t: t.get("lastAccessed") or 0)["targetId"]
    bh_switch_tab(want)


def ext_call(fn, *args):
    """Call an extension SW function. Returns the deserialised JS value,
    or None if the extension's service worker isn't registered."""
    sw = next((t["targetId"] for t in cdp("Target.getTargets")["targetInfos"]
               if t.get("type") == "service_worker"
               and t.get("url", "").startswith("chrome-extension://")), None)
    if sw is None:
        return None
    s = cdp("Target.attachToTarget", targetId=sw, flatten=True)["sessionId"]
    a = ", ".join(json.dumps(x) for x in args)
    try:
        return cdp("Runtime.evaluate", session_id=s,
                   expression=f"self.{fn}({a})",
                   awaitPromise=True, returnByValue=True)["result"].get("value")
    finally:
        cdp("Target.detachFromTarget", sessionId=s)


def _session_id():
    # The FULL identity string — passed to the extension so the browser derives the tab
    # group's codename (emoji + colour + last-4) itself. bin/horse-browser resolves the
    # agent system's identity (integrations/*/detect.sh) and exports HORSE_SESSION, plus
    # HORSE_LANE for a subagent lane. The CLAUDE_CODE_SESSION_ID fallback covers direct
    # calls that bypass the launcher.
    sid = os.environ.get("HORSE_SESSION") or os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    lane = os.environ.get("HORSE_LANE", "")
    return f"{sid}#{lane}" if sid and lane else sid


def bh_switch_tab(target_id):
    # Switch the DRIVEN tab without Target.activateTarget AND without changing the
    # window's visible tab: focus emulation lets us drive it fully in the background
    # (navigate, click, screenshot), so concurrent agents never flip each other's view.
    try: cdp("Runtime.evaluate", expression="if(document.title.startsWith('\U0001F434 '))document.title=document.title.slice(3)")
    except Exception: pass
    sid = cdp("Target.attachToTarget", targetId=target_id, flatten=True)["sessionId"]
    _send({"meta": "set_session", "session_id": sid, "target_id": target_id})
    _hb_remember(target_id)   # remember the tab I'm now driving, so a drift can restore THIS one
    cdp("Emulation.setFocusEmulationEnabled", enabled=True)
    _mark_tab()
    return sid


_hb_reported = False  # surface the group's open tabs once per CLI process


def _tab_report(tabs):
    # Once per process, tell the agent which tabs its session group has open — on
    # stderr, so it never pollutes a script's stdout. Tune via HORSE_BROWSER_TAB_NUDGE
    # (default 5) and HORSE_BROWSER_TAB_IDLE_MIN (default 10).
    global _hb_reported
    if _hb_reported:
        return
    _hb_reported = True
    try:
        if not tabs:
            return
        idle_min = float(os.environ.get("HORSE_BROWSER_TAB_IDLE_MIN", "10"))
        nudge_at = int(os.environ.get("HORSE_BROWSER_TAB_NUDGE", "5"))
        now = time.time() * 1000
        stale = sum(1 for t in tabs
                    if t.get("lastAccessed") and (now - t["lastAccessed"]) >= idle_min * 60000)
        line = (f"\U0001F434 horse-browser: {len(tabs)} tab(s) open in your group "
                f"({stale} idle ≥{int(idle_min)}m)")
        if stale > nudge_at:
            line += (" — close what you don't need: "
                     "cdp('Target.closeTarget', targetId=t['targetId']) per tab (ids via bh_list())")
        print(line, file=sys.stderr)
    except Exception:
        pass


def bh_open(url):
    """Open `url` in this session's coloured tab group WITHOUT raising the browser
    over the operator's app. Reuses a blank tab already in the group when possible.
    Returns the CDP targetId."""
    tid = None
    if _session_id():
        tabs = bh_list()
        _tab_report(tabs)   # once per call: surface the group's tabs (+ nudge if hoarding)
        for t in tabs:
            if (t.get("url") or "") in ("", "about:blank") and t.get("targetId"):
                tid = t["targetId"]
                break
    created = tid is None
    if created:
        tid = cdp("Target.createTarget", url="about:blank", background=True)["targetId"]
    try:
        # group BEFORE navigating: an open that fails (or a session killed
        # mid-navigation) still lands in the session group, so bh_list()-based
        # cleanup can see it instead of leaking an ungrouped about:blank tab.
        if _session_id() and created:
            ext_call("groupTab", tid, _session_id())
        bh_switch_tab(tid)
        if url != "about:blank":
            goto_url(url)
        wait_for_load()
    except Exception:
        # don't leak a tab WE created (never close a reused, pre-existing one)
        if created:
            try: cdp("Target.closeTarget", targetId=tid)
            except Exception: pass
        raise
    return tid


def bh_list():
    """Tabs in this session's group (targetId, url, title, lastAccessed, …)."""
    return ext_call("listTabs", _session_id()) or [] if _session_id() else []


# ── screenshots: a unique file per call, captured on a fresh per-target session ──
_hb_shots_swept = False


def _hb_sweep_shots(max_age_s=86400):
    # Unique names accumulate where a single shot.png didn't — drop day-old ones.
    global _hb_shots_swept
    if _hb_shots_swept:
        return
    _hb_shots_swept = True
    now = time.time()
    try:
        for e in os.scandir(ipc._TMP):
            if e.name.startswith("shot-") and e.name.endswith(".png") and now - e.stat().st_mtime > max_age_s:
                try: os.remove(e.path)
                except OSError: pass
    except OSError:
        pass


def _hb_my_target():
    """The CDP targetId of the tab THIS session is driving — the one a screenshot means."""
    tid = _hb_recall()
    if tid:
        return tid
    try:
        return (current_tab() or {}).get("targetId")
    except Exception:
        return None


def _struct_png_dims(raw):
    import struct
    try:
        return struct.unpack(">II", raw[16:24])
    except Exception:
        return (0, 0)


def capture_screenshot(path=None, full=False, max_dim=None):
    """Save a PNG of this session's tab to a per-call unique file; returns the path.
    full=capture beyond viewport, max_dim=downscale (needs pillow).

    Reliable for backgrounded, never-window-visible tabs under heavy multi-agent load,
    WITHOUT ever changing the window's visible tab. Two things make it work:
      - the launcher runs Chrome with --disable-renderer-backgrounding et al., so a
        backgrounded tab's renderer stays live and paints instead of throttling to a
        2x2 surface;
      - we capture over a DEDICATED flat session attached to OUR target, so N agents
        capturing at once never contend and never drift. (Capturing on the daemon's
        shared session drifts onto the wrong or an uncomposited tab.)
    NB: do NOT setFocusEmulationEnabled here — it leaks into the browser's global focus
    state and makes OTHER sessions' current_tab() resolve to this tab."""
    if path is None:
        _hb_sweep_shots()
        import tempfile
        fd, path = tempfile.mkstemp(
            prefix=f"shot-{os.environ.get('BU_NAME', 'default')}-",
            suffix=".png", dir=str(ipc._TMP))
        os.close(fd)

    target = _hb_my_target()
    if not target:                      # no identity / no tab — fall back to stock capture
        return _capture_screenshot_stock(path, full=full, max_dim=max_dim) or path

    sid = None
    data = None
    try:
        sid = cdp("Target.attachToTarget", targetId=target, flatten=True)["sessionId"]
        cdp("Page.enable", session_id=sid)
        # Compositor-surface read on our own session; a couple of quick retries cover
        # a tab still warming up right after open.
        for _ in range(5):
            try:
                r = cdp("Page.captureScreenshot", session_id=sid, format="png",
                        captureBeyondViewport=full, fromSurface=True)
                raw = base64.b64decode(r.get("data") or "")
                if raw[:8] == b"\x89PNG\r\n\x1a\n":
                    w, h = _struct_png_dims(raw)
                    if w > 4 and h > 4:
                        data = raw
                        break
            except Exception:
                pass
            time.sleep(0.12)

        if data is None:                 # degenerate even so (essentially never) — stock
            if os.environ.get("HB_CAP_DEBUG"):
                print(f"CAPFALLBACK target={target[:8]}", file=sys.stderr, flush=True)
            return _capture_screenshot_stock(path, full=full, max_dim=max_dim) or path
        with open(path, "wb") as f:
            f.write(data)
    finally:
        if sid:
            try: cdp("Target.detachFromTarget", sessionId=sid)
            except Exception: pass

    if max_dim:
        try:
            from PIL import Image
            img = Image.open(path)
            if max(img.size) > max_dim:
                img.thumbnail((max_dim, max_dim))
                img.save(path)
        except Exception:
            pass
    return path


# ── page hints: a generic per-navigation plug point ──────────────────────────────
# Anything EXECUTABLE in ~/.config/horse-browser/hints.d/ is called on navigation as
#     <hook> <url>          (env carries HORSE_SESSION / HORSE_LANE; 2.5s cap)
# and whatever it prints is surfaced to the agent — once per host per process.
# A hint must never break or stall browsing: empty/slow/failing hooks are skipped.
_hb_hinted = set()
_hb_hint_hooks_cache = None


def _hb_hint_hooks():
    global _hb_hint_hooks_cache
    if _hb_hint_hooks_cache is None:
        d = os.path.expanduser("~/.config/horse-browser/hints.d")
        try:
            _hb_hint_hooks_cache = sorted(
                e.path for e in os.scandir(d) if e.is_file() and os.access(e.path, os.X_OK))
        except OSError:
            _hb_hint_hooks_cache = []
    return _hb_hint_hooks_cache


def _hb_hints(url):
    if not (url or "").startswith("http") or not _hb_hint_hooks():
        return
    from urllib.parse import urlsplit
    host = urlsplit(url).hostname or ""
    if not host or host in _hb_hinted:
        return
    _hb_hinted.add(host)
    import subprocess
    for hook in _hb_hint_hooks():
        try:
            out = subprocess.run([hook, url], capture_output=True, text=True, timeout=2.5).stdout.strip()
        except Exception:
            continue
        for line in out.splitlines():
            print(line if line.startswith("\U0001F434") else "\U0001F434 horse-browser: " + line,
                  file=sys.stderr)
