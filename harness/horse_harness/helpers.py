"""Browser control via CDP — the core verbs, including trusted input & challenges.

Core helpers live here (CDP, page, tabs, trusted input, challenge gestures).
Agent-editable helpers live in BH_AGENT_WORKSPACE/agent_helpers.py; installable
capability bundles in BH_AGENT_WORKSPACE/plugins/.
"""
import base64, json, math, os, sys, time, urllib.request
from pathlib import Path
from urllib.parse import urlparse

# aliases used by the trusted-input section below (merged from the former input.py)
import random as _ir, math as _im, time as _it, json as _ij, sys as _isys

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


# How long a verb waits on the daemon. 5s is right for a laptop, where the renderer answers a
# Runtime.evaluate in milliseconds and a longer wait would only delay noticing a wedged daemon.
# It is NOT right everywhere: on a headless host the browser runs on a software rasteriser
# (Xvfb is a software X server, so there is no GPU to reach), and a CPU-heavy page can peg the
# renderer long enough that even `document.readyState` misses the window. Measured on a
# fingerprinting page — every verb failed, and nothing in the message said "you may wait longer".
# So: same default, and a way to raise it where the hardware makes it wrong.
IPC_TIMEOUT = float(os.environ.get("HORSE_BROWSER_IPC_TIMEOUT") or 5.0)


def _send(req):
    c, token = ipc.connect(NAME, timeout=IPC_TIMEOUT)
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
    """Navigate this tab. A site skill for the destination surfaces via wait_for_load()."""
    return cdp("Page.navigate", url=url)

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


def all_tabs(include_chrome=True):
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

def close_tab(target=None):
    """Close a tab. If `target` is omitted, closes the currently attached tab.
    Accepts a raw targetId string or a dict from all_tabs()/current_tab()."""
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
    own_ids = {t.get("targetId") for t in list_tabs()} if _session_id() else set()
    if cur.get("url") and not cur["url"].startswith(INTERNAL) and \
       (not _session_id() or cur.get("targetId") in own_ids):
        return cur
    own = [t for t in list_tabs() if t.get("targetId")] if _session_id() else []
    if own:
        switch_tab(own[-1]["targetId"])
        return own[-1]
    return None

def _frame_is_mine(frame_id):
    """Is this frame a child of the tab THIS session drives?

    Asks the parent, not the frame. For an out-of-process iframe the CDP targetId IS the
    frameId, and DOM.getFrameOwner resolves a frameId to its owning <iframe> element — but only
    on the session of the page that actually contains it. Anything else raises. That makes it a
    straight ownership test, and unlike Page.getFrameTree it does not depend on whether a
    cross-process child happens to be listed in the parent's tree.
    """
    me = _hb_my_target()
    if not me:
        return None                                   # can't tell — caller decides
    sid = cdp("Target.attachToTarget", targetId=me, flatten=True)["sessionId"]
    cdp("DOM.getDocument", session_id=sid, depth=0)   # the DOM agent must be live for the lookup
    try:
        cdp("DOM.getFrameOwner", session_id=sid, frameId=frame_id)
        return True
    except Exception:
        return False


def iframe_target(url_substr):
    """First iframe target in THIS tab whose URL contains `url_substr`. Use with js(..., target_id=...).

    Scoped to our own tab on purpose. The browser is shared — a hundred subagents can be on
    the same site at once, and matching by URL alone across every target in the browser hands
    back whichever frame Chrome happened to list first. That is another agent's challenge (or
    our own stale tab's), and the tell is subtle: the frame reads as visibilityState "hidden"
    and its widget never renders, so a solver drags at coordinates from a page nobody is
    looking at. Fall back to the unscoped search only if the frame tree is unreadable."""
    fallback = None
    for t in cdp("Target.getTargets")["targetInfos"]:
        if t["type"] != "iframe" or url_substr not in t.get("url", ""):
            continue
        try:
            mine = _frame_is_mine(t["targetId"])
        except Exception:
            mine = None
        if mine is None:                       # ownership unknowable → old behaviour, unchanged
            return t["targetId"]
        if mine:
            return t["targetId"]
        fallback = fallback or t["targetId"]
    if fallback:
        _hb_warn_once("iframe-scope",
                      "iframe_target(%r): the only match lives in another tab — using it, but "
                      "coordinates from it will not line up with this session's page."
                      % url_substr)
    return fallback


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
        url = (current_tab() or {}).get("url") or ""
        _hb_hints(url)
        _emit_skill_hint(url)
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


def _hb_tabs_file():
    return os.path.join(os.path.expanduser("~/.config/horse-browser/tabs"),
                        os.environ.get("BU_NAME", "default"))


def _hb_tracked():
    try:
        v = json.loads(open(_hb_tabs_file()).read())
        return [t for t in v if isinstance(t, str)] if isinstance(v, list) else []
    except Exception:
        return []


def _hb_track(target_id):
    # The attached-mode tab registry. Driving a browser we didn't launch means no
    # extension, and without the extension there are no tab groups — so the browser
    # cannot tell us which tabs are this session's and we have to remember it ourselves.
    # Ordered oldest→newest (re-touching a tab moves it to the end) so list_tabs() can
    # synthesise the recency that lastAccessed carries in owned mode. Written in owned
    # mode too and simply ignored on read there: the tab group is still the truth.
    if not target_id:
        return
    _hb_registry_update(lambda ids: [t for t in ids if t != target_id] + [target_id])



_HB_WARNED = set()


def _hb_warn_once(topic, msg):
    """Say a degradation out loud, once per process. Silence about a fallback is how a
    downgrade becomes permanent — nobody fixes what nobody sees."""
    if topic in _HB_WARNED:
        return
    _HB_WARNED.add(topic)
    try:
        print("\U0001F434 horse-browser: " + msg, file=sys.stderr)
    except Exception:
        pass


def _hb_registry_lock(path):
    """Exclusive hold on one registry, across processes. Returns an fd to close, or None.

    os.replace() stops a reader seeing a torn file; it does NOT stop two writers from both
    reading [X], appending different tabs, and one replacement silently winning. Same lane,
    two processes — the daemon claims a tab it minted while a client claims one it opened —
    and the loser's tab stays open, invisible to list_tabs() and to every registry-based
    reaper. Lock the whole read-modify-write, not just the write.
    """
    try:
        import fcntl
    except ImportError:
        _hb_warn_once("registry-nolock",
                      "no fcntl on this platform — registry writes are unlocked, so two "
                      "processes on one lane can lose a tab claim")
        return None
    try:
        # Leading dot: the launcher's reaper globs TABS/* to find registries, and glob("*")
        # skips dotfiles. A lock or temp file it COULD see would be read as a dead session's
        # registry and its tabs closed.
        d, n = os.path.dirname(path), os.path.basename(path)
        fd = os.open(os.path.join(d, "." + n + ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd
    except OSError as e:
        # Unlocked beats not writing at all — but this silently re-enables the lost-update
        # bug the lock exists to prevent, so it must never pass unremarked.
        _hb_warn_once("registry-nolock",
                      "cannot lock the tab registry (%s) — writes are unlocked, so two "
                      "processes on one lane can lose a tab claim" % e)
        return None


def _hb_registry_unlock(fd):
    if fd is None:
        return
    try:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        os.close(fd)
    except OSError:
        pass


def _hb_registry_update(mutate):
    """Read → mutate → replace, under the lock. THE only way this file is written."""
    f = _hb_tabs_file()
    try:
        os.makedirs(os.path.dirname(f), exist_ok=True)
    except Exception:
        return
    fd = _hb_registry_lock(f)
    try:
        ids = mutate(_hb_tracked())[-64:]
        tmp = os.path.join(os.path.dirname(f), ".%s.%d.tmp" % (os.path.basename(f), os.getpid()))
        with open(tmp, "w") as h:
            h.write(json.dumps(ids))
        os.replace(tmp, f)
    except Exception:
        pass
    finally:
        _hb_registry_unlock(fd)


def _hb_remember(target_id):
    # Persist the tab this session is currently driving, so _hb_home (and the daemon's
    # own bound-tab attach) can put us back on the SAME tab after any drift.
    try:
        f = _hb_current_file()
        os.makedirs(os.path.dirname(f), exist_ok=True)
        open(f, "w").write(target_id or "")
    except Exception:
        pass
    _hb_track(target_id)   # every tab we drive is one of ours — see _hb_track


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
    own = [t for t in list_tabs() if t.get("targetId")]
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
        if cur and ((ct.get("url") or "") in ("", "about:blank")
                    or (ct.get("url") or "").startswith(
                        ("chrome://newtab", "chrome://new-tab-page", "edge://newtab", "about:newtab"))):
            ext_call("groupTab", cur, _session_id())      # paint only; _hb_remember owns it
            _hb_remember(cur)
        else:
            open_tab("about:blank")         # no adoptable blank → create + home one in my group
        return
    # Foreign: restore the EXACT tab I last drove (persisted on every switch_tab).
    # Only if that tab is truly gone do we fall back to the newest own tab.
    want = _hb_recall()
    if want and want not in own_ids:
        print("\U0001F434 horse-browser: the tab you were driving has closed — switched to "
              "another of your tabs (list_tabs() to see them)", file=sys.stderr)
        want = None
    if want not in own_ids:
        want = max(own, key=lambda t: t.get("lastAccessed") or 0)["targetId"]
    switch_tab(want)


_EXT_IS_MINE = "typeof self.groupTab==='function'&&typeof self.activateTab==='function'"
_EXT_NOT_MINE = "__not_horse_sw__"


def ext_call(fn, *args):
    """Call one of OUR extension's SW functions. Returns the deserialised JS value, or None
    when our extension isn't on this browser.

    Not "the first chrome-extension:// worker": every Chrome carries component extensions of
    its own, and a bare profile already has Google's running before anyone installs anything.
    Each candidate is asked whether it is ours in the same round trip — the guard
    tools/sw_eval.py has always used — so `fn` never runs in a stranger's worker.
    """
    # Unattended: the launcher did not load the extension, so there is nothing to find. Say so
    # up front rather than walking every service worker and attaching to each — that is a
    # target list plus an attach/detach per candidate, on a path that runs for every tab we
    # mint and every re-home.
    if os.environ.get("HORSE_BROWSER_UNATTENDED"):
        return None
    cands = [t["targetId"] for t in cdp("Target.getTargets")["targetInfos"]
             if t.get("type") == "service_worker"
             and t.get("url", "").startswith("chrome-extension://")]
    a = ", ".join(json.dumps(x) for x in args)
    guarded = f"({_EXT_IS_MINE}) ? (self.{fn}({a})) : '{_EXT_NOT_MINE}'"
    for sw in cands:
        s = cdp("Target.attachToTarget", targetId=sw, flatten=True)["sessionId"]
        try:
            v = cdp("Runtime.evaluate", session_id=s, expression=guarded,
                    awaitPromise=True, returnByValue=True)["result"].get("value")
        finally:
            cdp("Target.detachFromTarget", sessionId=s)
        if v != _EXT_NOT_MINE:
            return v
    return None


def _session_id():
    # The FULL identity string — passed to the extension so the browser derives the tab
    # group's codename (emoji + colour + last-4) itself. bin/horse-browser resolves the
    # agent system's identity (integrations/*/detect.sh) and exports HORSE_SESSION, plus
    # HORSE_LANE for a subagent lane. The CLAUDE_CODE_SESSION_ID fallback covers direct
    # calls that bypass the launcher.
    sid = os.environ.get("HORSE_SESSION") or os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    lane = os.environ.get("HORSE_LANE", "")
    return f"{sid}#{lane}" if sid and lane else sid


def switch_tab(target):
    """Make `target` the tab this session drives — focus-safe. Accepts a targetId string or a
    current_tab()/all_tabs() dict. Attaches it (`Target.attachToTarget flatten`), binds the daemon
    session to it, and enables `Emulation.setFocusEmulationEnabled` so it drives live in the
    background. It does NOT call `Target.activateTarget` and does NOT change which tab is visible in
    the window, so concurrent agents and the operator never see their view flip."""
    target_id = (target.get("targetId") or target.get("target_id")) if isinstance(target, dict) else target
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
        # Only speak up when there's something to act on — too many tabs, or idle ones. The common
        # case (a handful of live tabs) stays silent: no per-invocation noise.
        if len(tabs) < nudge_at and stale == 0:
            return
        line = (f"\U0001F434 horse-browser: {len(tabs)} tab(s) open in your group "
                f"({stale} idle ≥{int(idle_min)}m)")
        if stale > 0:
            line += (" — close what you don't need: "
                     "cdp('Target.closeTarget', targetId=t['targetId']) per tab (ids via list_tabs())")
        print(line, file=sys.stderr)
    except Exception:
        pass


def open_tab(url):
    """Open `url` in this session's coloured tab group WITHOUT raising the browser
    over the operator's app. Reuses a blank tab already in the group when possible.
    Returns the CDP targetId."""
    tid = None
    if _session_id():
        tabs = list_tabs()
        _tab_report(tabs)   # once per call: surface the group's tabs (+ nudge if hoarding)
        for t in tabs:
            if (t.get("url") or "") in ("", "about:blank") and t.get("targetId"):
                tid = t["targetId"]
                break
    created = tid is None
    if created:
        tid = cdp("Target.createTarget", url="about:blank", background=True)["targetId"]
        _hb_track(tid)      # claim it in the SAME breath as creating it — see below
    try:
        # Record, then paint, then navigate. The order is the invariant: a tab is ours
        # the moment the registry says so, so an open that dies mid-flight (navigation
        # error, session killed) still leaves a tab list_tabs() and the reaper can see,
        # instead of an untracked about:blank nobody will ever close.
        if _session_id() and created:
            ext_call("groupTab", tid, _session_id())
        switch_tab(tid)
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


def list_tabs():
    """This session's tabs (targetId, url, title, lastAccessed, …).

    Answered from the registry — always, extension or not. The tab group is a rendering
    of this list, never its source: asking the browser which tabs are grouped under our
    codename would give a second answer that can disagree with the first, and every way
    it disagrees is a bug (a groupTab that quietly failed, a tab an operator dragged out
    of the group, a browser with no extension at all). One truth, one place.
    """
    tracked = _hb_tracked()
    if not tracked:
        return []
    live = {t["targetId"]: t for t in cdp("Target.getTargets")["targetInfos"]
            if t.get("type") == "page"}
    out = [{"targetId": tid, "tabId": None, "url": live[tid].get("url"),
            "title": live[tid].get("title"), "lastAccessed": i + 1,
            "discarded": None, "audible": None, "active": None}
           for i, tid in enumerate(tracked) if tid in live]
    if len(out) != len(tracked):            # forget the ids whose tabs are gone
        # Through the same locked updater as every other write, and re-filtered against the
        # registry as it is INSIDE the lock — not against `tracked`, which was read before the
        # live-target round trip. A tab claimed during that window is not in `out`, and a plain
        # overwrite would drop it: a live tab, open and unreachable. The prune only ever
        # removes ids this call proved dead.
        gone = {t for t in tracked} - {t["targetId"] for t in out}
        _hb_registry_update(lambda ids: [t for t in ids if t not in gone])
    return out


def realness_ok():
    """Is the current tab presenting as real Chrome? -> {"ok", "why", "brands", "ua_major"}.

    Worth calling the moment a site starts behaving as though it can tell. The mask fails
    SILENTLY and it fails OPEN: with our extension loaded it is applied browser-wide by a
    content script and is effectively always on, but on a browser we did not launch the daemon
    applies it per session — so a daemon that died, or a tab opened outside the harness, is
    bare, and nothing announces that. This is the check that makes it loud.

    Covers the JS half (navigator.userAgentData, navigator.webdriver). The WIRE half — the
    sec-ch-ua request header — cannot be read back from inside the page; nothing exposes a
    document's own request headers. To verify it, hit a reflector and read the body.
    """
    r = js("(()=>{const u=navigator.userAgentData;"
           "const m=navigator.userAgent.match(/Chrome\\/(\\d+)/);"
           "return {brands: u ? u.brands.map(b=>b.brand+' '+b.version) : null,"
           " ua_major: m ? m[1] : null, webdriver: navigator.webdriver === true,"
           " secure: window.isSecureContext === true};})()") or {}
    brands, major = r.get("brands"), r.get("ua_major")
    why = []
    if brands is None and not r.get("secure"):
        # navigator.userAgentData only exists in a secure context, so on a data:/file:/plain
        # http: page its absence says nothing about the mask. Report unknown, not broken —
        # "the mask is missing" would send you chasing a bug that isn't there.
        return {"ok": None, "why": "insecure context — navigator.userAgentData is not exposed "
                                   "here, so realness cannot be read; check on an https: page",
                "brands": None, "ua_major": major}
    if brands is None:
        why.append("no navigator.userAgentData (pre-Chromium-90 or a non-Chromium engine)")
    else:
        if not any(b.startswith("Google Chrome") for b in brands):
            why.append("brands do not claim Google Chrome — the mask is not applied here")
        if major and not any(b == f"Google Chrome {major}" for b in brands):
            why.append(f"brand version disagrees with UA major {major} — a mismatch sites flag")
    if r.get("webdriver"):
        why.append("navigator.webdriver is true")
    return {"ok": not why, "why": "; ".join(why), "brands": brands, "ua_major": major}


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
# This is the in-flow channel for PER-PAGE CONTEXT the operator has and the model can't
# know — creds ready to auth for this site, "move human-like / go slow here", a "read-only,
# don't submit" rule. New context = a new hook, zero core code. (Deliberately NOT used to
# re-teach generic sharp edges — the always-on rule does that.) See memory
# in-flow-context-channel + hints-d-plug-point.
_hb_hinted = None            # per-SESSION {host: last-shown epoch}, persisted; re-surfaced after a TTL
_hb_hint_hooks_cache = None


def _hb_hinted_path():
    # Keyed by the session (BU_NAME is already filesystem-safe), so dedupe survives across the
    # many one-shot `horse-browser` invocations that make up one agent session.
    sess = os.environ.get("BU_NAME") or os.environ.get("HORSE_SESSION") or "default"
    safe = "".join(c if (c.isalnum() or c in "_.-") else "_" for c in sess)[:64]
    return str(ipc._TMP / ("hinted-" + safe + ".json"))


def _hb_load_hinted():
    global _hb_hinted
    if _hb_hinted is None:
        try:
            with open(_hb_hinted_path()) as f:
                data = json.load(f)
            _hb_hinted = data if isinstance(data, dict) else {}   # {host: last-shown epoch}
        except Exception:
            _hb_hinted = {}
    return _hb_hinted


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
    if not host:
        return
    hinted = _hb_load_hinted()
    ttl = float(os.environ.get("HORSE_BROWSER_HINT_TTL", "7200"))   # re-surface a host's hint after 2h
    if time.time() - hinted.get(host, 0) < ttl:   # shown recently → stay quiet. NOT once-forever:
        return                                     # a long session would forget, so re-show every ttl.
    hinted[host] = time.time()
    try:
        os.makedirs(str(ipc._TMP), exist_ok=True)
        with open(_hb_hinted_path(), "w") as f:
            json.dump(hinted, f)
    except Exception:
        pass
    import subprocess
    for hook in _hb_hint_hooks():
        try:
            out = subprocess.run([hook, url], capture_output=True, text=True, timeout=2.5).stdout.strip()
        except Exception:
            continue
        for line in out.splitlines():
            print(line if line.startswith("\U0001F434") else "\U0001F434 horse-browser: " + line,
                  file=sys.stderr)


# ── site skills: a per-host playbook the agent reads on arrival ───────────────────
# domain-skills/<domain.tld>/*.md in the agent workspace. On navigation we tell the agent
# when the host HAS a skill (read it), and — if it doesn't — nudge once after a few visits to
# write one for reusable per-site work. Host is normalised to domain.tld so www/subdomain/path
# don't fragment it (the same key logins should match on). Per-session + deduped, like hints.
_hb_skill_state = None

def _registrable_host(url):
    """Site key for skills (and the key logins should match on): domain.tld — protocol, www,
    subdomain and path ignored. Deliberately coarse (last two labels); over-match beats miss."""
    h = (urlparse(url).hostname or "").lower().strip(".")
    parts = h.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else h

def _hb_skill_path():
    sess = os.environ.get("BU_NAME") or os.environ.get("HORSE_SESSION") or "default"
    safe = "".join(c if (c.isalnum() or c in "_.-") else "_" for c in sess)[:64]
    return str(ipc._TMP / ("skills-" + safe + ".json"))

def _hb_load_skill_state():
    global _hb_skill_state
    if _hb_skill_state is None:
        try:
            with open(_hb_skill_path()) as f:
                data = json.load(f)
            _hb_skill_state = data if isinstance(data, dict) else {}
        except Exception:
            _hb_skill_state = {}
    return _hb_skill_state

def _hb_skill_hint(url):
    """Return site-skill hint lines for this navigation, updating per-session visit state.
    Skill present → announce on the first visit; none → nudge to write one on the 3rd."""
    if not (url or "").startswith("http"):
        return []
    host = _registrable_host(url)
    if not host:
        return []
    state = _hb_load_skill_state()
    rec = state.setdefault(host, {"n": 0, "said": False})
    rec["n"] += 1
    lines = []
    if not rec["said"]:
        skills = sorted(p.name for p in (AGENT_WORKSPACE / "domain-skills" / host).glob("*.md"))
        if skills:
            rec["said"] = True
            lines = ["\U0001F434 %s has a site skill: %s — read it before working this site."
                     % (host, ", ".join(skills[:4]))]
        elif rec["n"] >= 3:
            rec["said"] = True
            lines = ["\U0001F434 no site skill for %s yet — building reusable helpers for it? "
                     "save one at domain-skills/%s/<name>.md." % (host, host)]
    try:
        os.makedirs(str(ipc._TMP), exist_ok=True)
        with open(_hb_skill_path(), "w") as f:
            json.dump(state, f)
    except Exception:
        pass
    return lines

def _emit_skill_hint(url):
    for line in _hb_skill_hint(url):
        print(line, file=sys.stderr)


# ═══ trusted input & challenges — real key/mouse events, easy-challenge gestures ═══
# The DEFAULT input verbs (merged from the former input.py, itself once the workspace horse_input.py).
# Sites bind real logic to real events (keyup/input enable submit, mousedown opens menus); el.value=,
# el.click() and Input.insertText fire nothing — these fire the real chain, so pages behave.
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


_debug_click_counter = 0


def _debug_click(x, y):
    """--debug-clicks (BH_DEBUG_CLICKS): draw a red target at (x, y) on a screenshot, so you can
    eyeball where a trusted click landed. Best-effort; needs pillow."""
    global _debug_click_counter
    try:
        from PIL import Image, ImageDraw
        dpr = js("window.devicePixelRatio") or 1
        path = capture_screenshot(str(ipc._TMP / f"debug_click_{_debug_click_counter}.png"))
        img = Image.open(path); draw = ImageDraw.Draw(img)
        px, py, r = int(x * dpr), int(y * dpr), int(15 * dpr)
        draw.ellipse([px - r, py - r, px + r, py + r], outline="red", width=int(3 * dpr))
        draw.line([px - r - int(5 * dpr), py, px + r + int(5 * dpr), py], fill="red", width=int(2 * dpr))
        draw.line([px, py - r - int(5 * dpr), px, py + r + int(5 * dpr)], fill="red", width=int(2 * dpr))
        img.save(path)
        print(f"[debug_click] saved {path} (x={x}, y={y}, dpr={dpr})")
    except Exception as e:
        print(f"[debug_click] overlay failed: {e}")
    _debug_click_counter += 1


def click_xy(x, y):
    """Trusted left click at viewport coords — the full mousedown/mouseup/click (+pointer)
    chain, so the page reacts exactly as it would to a person (unlike el.click())."""
    if os.environ.get("BH_DEBUG_CLICKS"):
        _debug_click(x, y)
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


def hover(css):
    """Trusted hover — move the real cursor to the centre of `css`, so hover-reveal menus,
    tooltips and :hover styles fire. js() cannot dispatch a trusted mousemove."""
    c = _center(css)
    if not c:
        raise RuntimeError("hover: no visible element " + css)
    _move(c[0], c[1])


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


def _sstep(t):
    return t * t * (3 - 2 * t)


def _approach(x1, y1):
    """Move to (x1,y1) the way a hand arrives: along a slightly bowed path, decelerating.

    A single teleporting mouseMoved is a tell on its own — the pointer materialises on the
    control with no history. Anything scoring a gesture has the whole trace, and the approach
    is part of it.
    """
    x0, y0 = _mouse.get("x", x1), _mouse.get("y", y1)
    dist = _im.hypot(x1 - x0, y1 - y0)
    if dist < 4:
        return
    n = max(6, min(28, int(dist / 22)))
    bow = _ir.uniform(-0.06, 0.06) * dist          # a hand does not travel a straight line
    for i in range(1, n + 1):
        e = _sstep(i / n)
        px = x0 + (x1 - x0) * e - (y1 - y0) / (dist or 1) * bow * _im.sin(_im.pi * e)
        py = y0 + (y1 - y0) * e + (x1 - x0) / (dist or 1) * bow * _im.sin(_im.pi * e)
        cdp("Input.dispatchMouseEvent", type="mouseMoved", x=px, y=py)
        _it.sleep(_ir.uniform(0.006, 0.018))
    # The final step already lands exactly on (x1,y1) — sin(pi) zeroes the bow — so sending it
    # again would put two identical samples back to back, which no hand produces.
    _mouse["x"], _mouse["y"] = x1, y1


def drag(target, to=None, dx=None, dy=0):
    """Trusted drag from `target` (CSS selector or (x, y) start coords) — for slide-to-verify.
    Give an absolute `to`=(x,y) target, or a relative `dx`/`dy`.

    The shape of the motion is the point, not just its endpoints. A slide-to-verify widget
    scores the trace, and the machine tells are all in the middle of it: a pointer that
    materialises on the handle with no approach, a symmetric velocity curve, evenly spaced
    samples, and an arrival that is dead-on the target to the pixel and releases instantly.
    Landing the right coordinates with that profile still fails, and each failure costs
    reputation on the address — which is how a working solver turns a challenge into a block.

    So: approach the handle, dwell, accelerate faster than we decelerate, wander a little
    (less as we arrive, as a hand does), overshoot slightly, correct back, and hold before
    letting go.
    """
    c = _pt(target)
    if not c:
        raise RuntimeError("drag: no visible element " + str(target))
    x0, y0 = c
    x1, y1 = (to if to else (x0 + (dx or 0), y0 + dy))
    span = _im.hypot(x1 - x0, y1 - y0)
    _approach(x0, y0)
    _it.sleep(_ir.uniform(0.05, 0.15))                    # settle before pressing
    cdp("Input.dispatchMouseEvent", type="mousePressed", x=x0, y=y0, button="left", buttons=1, clickCount=1)
    _it.sleep(_ir.uniform(0.06, 0.14))                    # a hand does not move the instant it presses

    # Each dispatch is a round trip, so wall-clock runs well past the sleeps: 46 steps put a
    # 280px drag at 3.7s, which is slower than a hand, not more human than one.
    steps = max(14, min(34, int(span / 9)))
    ov = _ir.uniform(3.0, 11.0) if span > 40 else 0.0     # overshoot, then come back
    ovx = (x1 - x0) / (span or 1) * ov
    ovy = (y1 - y0) / (span or 1) * ov
    hesitate = sorted(_ir.sample(range(3, steps - 2), min(2, max(0, steps - 6)))) if steps > 8 else []
    for i in range(1, steps + 1):
        t = i / steps
        # peak velocity early (t**0.78 skews smoothstep left) — acceleration is brisk, the
        # settle is long. A symmetric curve is the single loudest tell in a synthetic drag.
        e = _sstep(t ** 0.78)
        wob = (1.0 - t) * 1.4                             # precision improves on approach
        px = x0 + (x1 + ovx - x0) * e + _ir.uniform(-wob, wob)
        py = y0 + (y1 + ovy - y0) * e + _ir.uniform(-wob, wob)
        cdp("Input.dispatchMouseEvent", type="mouseMoved", x=px, y=py, buttons=1)
        _it.sleep(_ir.uniform(0.004, 0.016))
        if i in hesitate:
            _it.sleep(_ir.uniform(0.04, 0.09))            # a glance at the target
    if ov:                                                # corrective sub-movement back onto it
        n = _ir.randint(2, 3)
        for j in range(1, n + 1):
            k = j / (n + 1.0)                             # approaches the target, never lands on it
            cdp("Input.dispatchMouseEvent", type="mouseMoved", buttons=1,
                x=x1 + ovx * (1 - k) + _ir.uniform(-0.6, 0.6),
                y=y1 + ovy * (1 - k) + _ir.uniform(-0.6, 0.6))
            _it.sleep(_ir.uniform(0.015, 0.04))
    cdp("Input.dispatchMouseEvent", type="mouseMoved", x=x1, y=y1, buttons=1)
    _it.sleep(_ir.uniform(0.08, 0.22))                    # hold, then let go
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


# (No insert_text_fast wrapper: for a listener-free <textarea> where speed matters, call
# cdp("Input.insertText", text=…) directly — it's one raw call, and type_text/type_into
# above carry the "why real events matter" knowledge. Retired 2026-07-24.)


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
      for(var i=0;i<fr.length;i++){var f=fr[i],s=f.src||'',ti=(f.title||'').toLowerCase(),id=(f.id||'');
        var vendor=null;
        for(var j=0;j<pats.length;j++){ if(s.indexOf(pats[j][1])>=0){ vendor=pats[j][0]; break; } }
        // A full-page CF interstitial's Turnstile iframe often has an EMPTY/relative src but a
        // telltale title ("…Cloudflare…") or id (cf-chl-widget-*) — src-only matching misses it,
        // so solve_challenge would wrongly return 'none' on an interstitial. Catch it by title/id.
        if(!vendor && (ti.indexOf('cloudflare')>=0 || /^cf-chl/.test(id))) vendor='cloudflare';
        if(!vendor && ti.indexOf('hcaptcha')>=0) vendor='hcaptcha';
        if(vendor){
          var b=f.getBoundingClientRect();
          if(b.width>40&&b.height>20&&getComputedStyle(f).visibility!=='hidden')
            return {vendor:vendor,x:Math.round(b.x),y:Math.round(b.y),w:Math.round(b.width),h:Math.round(b.height)};
        }}
      return null;})()""")


_SAME_ORIGIN_DOC = """(function(frag){
  var fr=[].slice.call(document.querySelectorAll('iframe'));
  for(var i=0;i<fr.length;i++){
    if((fr[i].src||'').indexOf(frag)<0) continue;
    try { if(fr[i].contentDocument) return fr[i].contentDocument; } catch(e) {}
  }
  return null;})(%s)"""


def _same_origin_frame(frag):
    """Can this challenge frame's document be read straight from the parent?"""
    try:
        return bool(_eval("!!" + (_SAME_ORIGIN_DOC % json.dumps(frag))))
    except Exception:
        return False


# The scan, as a function OF the document to scan. Two documents can hold a challenge and they
# must score identically: an out-of-process frame's own document (reached by attaching to its
# target), and a same-origin frame's document reached straight from the parent. Sharing one body
# is the point — a second copy would drift, and the drift would only ever show up live.
_CONTROL_SCAN = r"""(function(doc){
          var out=[], boxes=[], all=doc.querySelectorAll('*');
          for(var i=0;i<all.length && i<4000;i++){
            var e=all[i], b=e.getBoundingClientRect();
            if(b.width<12||b.height<12||b.width>900||b.height>400) continue;
            var st=getComputedStyle(e);
            if(st.visibility==='hidden'||st.display==='none'||parseFloat(st.opacity||'1')<0.25) continue;
            var role=(e.getAttribute('role')||'').toLowerCase(), tag=e.tagName.toLowerCase();
            var t=((e.innerText||e.getAttribute('aria-label')||'')+'').trim().toLowerCase().slice(0,60);
            var cls=((e.className&&e.className.baseVal!==undefined?e.className.baseVal:e.className)||'')+'';
            var id=(e.id||'')+'';
            var grab=(st.cursor==='grab'||st.cursor==='grabbing');
            // The handle, not its icon: a grab cursor is inherited, so the glyph inside the
            // handle carries it too. The outermost element with the cursor is the thing you
            // grab; anything nested inside it is part of the same control.
            if(grab && e.parentElement &&
               getComputedStyle(e.parentElement).cursor===st.cursor) continue;
            var kind=null, score=0;
            if(role==='checkbox'||(tag==='input'&&e.type==='checkbox')){ kind='checkbox'; score=100; }
            // A grab cursor beats every other signal, and that ordering is the whole point.
            // Text and class names identify the WIDGET; the cursor identifies the GRIP. On a
            // live DataDome slider the text sits on a 338x121 wrapper while the handle is a
            // wordless 63x40 div, so ranking text first started the drag 140px right of the
            // handle, on nothing. Scored above the challenge-furniture bonus below, which the
            // wrapper also collects (90+15=105) — that is exactly how it kept winning.
            else if(grab){ kind='slider'; score=130; }
            else if(/press|hold/.test(t)){ kind='press-hold'; score=95; }
            else if(/slider|slide|drag/.test(cls+' '+id+' '+t)){ kind='slider'; score=90; }
            else if(tag==='button'||role==='button'){ kind='button'; score=60; }
            else if(st.cursor==='pointer'){ kind='button'; score=40; }
            if(!kind) continue;
            if(/captcha|challenge|verify|human/.test(cls+' '+id)) score+=15;
            // A block page's only control is "contact support" — reachable, clickable, and
            // completely useless. Measured on a live DataDome hard block: it was the single
            // interactive element in the frame, so it won by default and the solver went off
            // to click it. Push it below everything; if it is the best we have, there is no
            // challenge here to solve.
            if(/support|contact|help|customer|servicio|kundendienst|assistance/.test(
                 t+' '+cls+' '+id)) score-=200;
            out.push({kind:kind,score:score,x:b.x,y:b.y,w:b.width,h:b.height,grab:grab,
                      why:(t||cls||id).slice(0,40)});
            // A slide-to-verify widget draws its DESTINATION as a second box the same shape as
            // the handle, further right. Knowing it turns the drag from "shove it far enough
            // right and hope the latch catches" into an aimed gesture that lands where the
            // widget wants it. Collected for every candidate; only used if a handle wins.
            boxes.push({x:b.x,y:b.y,w:b.width,h:b.height});
          }
          // Tie-break on the SMALLER element, not the larger. A slider handle lives inside a
          // track that shares its class prefix, so "biggest wins" returned the wrapper: a
          // 280x100 container at (960,254) instead of the 63x40 grab handle at (820,305).
          // Dragging the wrapper's centre starts the gesture in the middle of the track.
          out.sort(function(a,b){return b.score-a.score || (a.w*a.h)-(b.w*b.h);});
          var top=out[0];
          if(top&&top.kind==='slider'){
            var dest=null;
            for(var k=0;k<boxes.length;k++){ var c=boxes[k];
              if(Math.abs(c.h-top.h)<=2 && Math.abs(c.w-top.w)<=4 &&
                 Math.abs(c.y-top.y)<=4 && c.x>top.x+top.w/2 &&
                 (!dest||c.x>dest.x)) dest=c; }
            if(dest){ top.dest_x=dest.x+dest.w/2; top.dest_y=dest.y+dest.h/2; }
          }
          // Is this a BLOCK rather than a challenge? Vendors say so plainly in the body —
          // DataDome sets dd-response-page--hard-block and prints the offending IP. There is
          // nothing to solve on such a page, and sending the agent to look at a screenshot of
          // one wastes a round trip and misreports the situation: the address is refused, and
          // that is an operator fact, not a perception problem.
          var b=doc.body, bc=((b&&b.className)||'')+' '+((b&&b.getAttribute('data-dd-response'))||'');
          var bt=((b&&b.innerText)||'').toLowerCase();
          var blocked = /hard-block|blocked|access denied/.test(bc)
                     || /(^|\W)(access denied|acceso denegado|uso indebido|you have been blocked|zugriff verweigert)/.test(bt);
          return {best: out[0]||null, blocked: !!blocked};})"""


def _frame_control(xo):
    """The actual control INSIDE a cross-origin challenge frame, in PAGE coordinates.

    The premise this replaces — "the DOM can't be read" — is false. A cross-origin iframe is
    its own CDP target: iframe_target() attaches to it and js(..., target_id=) evaluates
    inside it. Verified reading 29 nodes of a google.com reCAPTCHA frame from a page on
    another domain. What was actually missing was using it.

    Without this the solvers guess: the PerimeterX press-hold hardcodes
    (0.486*innerWidth, 0.553*innerHeight) of the frame, which is right until a layout changes.
    Reading the control's own rect is exact, and it also tells us WHICH control it is instead
    of inferring from the vendor name.

    Returns {"kind","x","y","w","h","why"} in page coords, or None when the frame genuinely
    cannot be read (sandboxed, or nothing control-shaped inside) — vision still covers that.
    """
    if not xo:
        return None
    # Match the frame by the same URL fragment the vendor was recognised by, so we attach to
    # the frame we measured rather than the first challenge-ish frame on the page.
    frag = {"cloudflare": "challenges.cloudflare.com", "datadome": "captcha-delivery",
            "hcaptcha": "hcaptcha.com", "perimeterx": "perimeterx", "arkose": "arkoselabs",
            "recaptcha": "recaptcha/api2"}.get(xo.get("vendor"))
    if not frag:
        return None
    tid = iframe_target(frag)
    if not tid and not _same_origin_frame(frag):
        # No target, and not readable from here either. Two different reasons a challenge frame
        # has no target of its own, and only one of them is hopeless: a Cloudflare interstitial
        # widget with no src, and — the case this used to give up on — a challenge served from
        # the SAME site as the page hosting it. Site isolation keys on site, so google.com's
        # reCAPTCHA inside a google.com page stays in the parent's process and never becomes a
        # target; being same-ORIGIN its document is readable directly. Measured on Google's own
        # reCAPTCHA demo, where this returned None and sent a perfectly readable checkbox to
        # vision.
        return None
    # Generic, not a per-vendor selector table: score every visibly interactive element and
    # take the best. A table would need editing every time a vendor reskins; shape and role
    # are what actually identify a control, and they are what the vendor cannot change without
    # changing the control.
    try:
        got = (js(_CONTROL_SCAN + "(document)", target_id=tid) if tid
               else js(_CONTROL_SCAN + "(" + (_SAME_ORIGIN_DOC % json.dumps(frag)) + ")"))
    except Exception:
        return None
    if not got:
        return None
    if got.get("blocked") and not (got.get("best") or {}).get("kind") in ("checkbox", "press-hold", "slider"):
        # A block page with no solvable control. Say so; do not invent a gesture for it.
        return {"kind": "blocked", "why": "vendor block page", "x": 0, "y": 0, "w": 0, "h": 0}
    got = got.get("best")
    if not got:
        return None
    # Frame-local → page. The frame's own viewport origin IS the iframe element's top-left in
    # the parent, which _xorigin_challenge already measured.
    out = {"kind": got["kind"], "why": got.get("why") or "",
           "x": int(xo["x"] + got["x"] + got["w"] / 2),
           "y": int(xo["y"] + got["y"] + got["h"] / 2),
           "w": int(got["w"]), "h": int(got["h"])}
    if got.get("dest_x") is not None:
        out["dest_x"] = int(xo["x"] + got["dest_x"])
        out["dest_y"] = int(xo["y"] + got["dest_y"])
    return out


def slider_gap(background, piece, step=2, refine=True):
    """Where does `piece` belong in `background`? → {"x","y","score"} in BACKGROUND pixels.

    For slide-to-verify puzzles (GeeTest and friends), which hand you two images: a background
    with a notch cut out of it, and the cut-out piece. Finding the notch is the whole task —
    the drag itself is already human-shaped (see drag()).

    Normalized cross-correlation, not an edge heuristic. Measured on 25 synthetic puzzles with
    known answers: NCC gives median 0px error and 96% within 2px, where hand-rolled
    edge-energy scoring gave a median of 50-69px — it kept locking onto the wrong rail. The
    method matters more than the tuning here, so this uses the one that is correct rather than
    the one that is clever.

    Inputs are PNG bytes, base64 strings, file paths, or PIL Images. Needs pillow; without it
    this raises rather than guessing, because a wrong drag is worse than no drag — it burns the
    attempt and, on a challenge that scores you, some reputation with it.

    score is the correlation at the winner, in [-1, 1]. Below ~0.6 do not trust it; a puzzle
    whose piece is mostly flat colour correlates with everything.
    """
    try:
        from PIL import Image
    except ImportError:
        raise RuntimeError("slider_gap needs pillow: horse-browser harness-setup, or "
                           "pip install pillow into harness/.venv")
    import base64 as _b64, io as _io

    def _img(v):
        if hasattr(v, "convert"):
            return v.convert("L")
        if isinstance(v, str):
            if v.startswith("data:"):
                v = v.split(",", 1)[1]
            if os.path.exists(v):
                return Image.open(v).convert("L")
            v = _b64.b64decode(v)
        return Image.open(_io.BytesIO(v)).convert("L")

    B, P = _img(background), _img(piece)
    W, H = B.size
    pw, ph = P.size
    if pw > W or ph > H:
        raise ValueError("piece (%dx%d) is larger than the background (%dx%d)" % (pw, ph, W, H))
    bp, pp = B.load(), P.load()

    def _corr(ox, oy, st):
        ys = range(0, ph, st); xs = range(0, pw, st)
        pv = [pp[i, j] for j in ys for i in xs]
        bv = [bp[ox + i, oy + j] for j in ys for i in xs]
        n = len(pv)
        pm, bm = sum(pv) / n, sum(bv) / n
        pd = [v - pm for v in pv]
        bd = [v - bm for v in bv]
        den = (sum(v * v for v in pd) ** 0.5) * (sum(v * v for v in bd) ** 0.5)
        return (sum(a * b for a, b in zip(pd, bd)) / den) if den > 0 else -1.0

    # Coarse over the whole image, then refine locally. A single fine pass over every offset
    # is minutes in pure Python (no numpy here); coarse-then-fine is sub-second and lands on
    # the same answer because the correlation surface has one clear peak.
    best, bx, by = -2.0, 0, 0
    for oy in range(0, H - ph + 1, 4):
        for ox in range(0, W - pw + 1, 2):
            r = _corr(ox, oy, max(step, 3))
            if r > best:
                best, bx, by = r, ox, oy
    if refine:
        for oy in range(max(0, by - 6), min(H - ph, by + 6) + 1):
            for ox in range(max(0, bx - 6), min(W - pw, bx + 6) + 1):
                r = _corr(ox, oy, 1)
                if r > best:
                    best, bx, by = r, ox, oy
    return {"x": bx, "y": by, "score": round(best, 4)}


def frame_images(target_id, min_side=40):
    """Every <img>/<canvas> inside a frame, as base64 PNG → [{"tag","w","h","b64"}].

    The slider puzzle's two images live inside the challenge frame, so getting them needs the
    same attach the control-reading does. Canvases are read with toDataURL; images are drawn
    onto a canvas first, which also sidesteps their crossOrigin state.
    """
    return js(r"""(function(){
      var out=[], els=document.querySelectorAll('img,canvas');
      for(var i=0;i<els.length;i++){
        var e=els[i], w=e.naturalWidth||e.width, h=e.naturalHeight||e.height;
        if(!w||!h||w<%d||h<%d) continue;
        try{
          var c=e.tagName.toLowerCase()==='canvas'?e:(function(){
            var t=document.createElement('canvas'); t.width=w; t.height=h;
            t.getContext('2d').drawImage(e,0,0); return t;})();
          out.push({tag:e.tagName.toLowerCase(), w:w, h:h,
                    b64:c.toDataURL('image/png').split(',')[1]});
        }catch(err){ /* tainted canvas — skip, the caller falls back to a screenshot */ }
      }
      return out;})()""" % (min_side, min_side), target_id=target_id) or []


def _overlay_over(xo):
    """A consent/cookie backdrop (OneTrust etc.) stacked over the challenge eats clicks: a
    click_xy at the challenge coords hits the overlay, not the control (seen live on nestle).
    If the topmost element at the challenge center isn't the challenge iframe, describe the
    obstruction so the agent dismisses it first. Returns {tag,id,z,txt} or None."""
    if not xo:
        return None
    cx = xo["x"] + xo["w"] // 2
    cy = xo["y"] + xo["h"] // 2
    return _eval(r"""(function(cx,cy){
      var el=document.elementFromPoint(cx,cy); if(!el||el.tagName==='IFRAME') return null;
      var r=el.getBoundingClientRect(), cs=getComputedStyle(el), z=parseInt(cs.zIndex)||0;
      var big=r.width*r.height > 0.15*innerWidth*innerHeight;
      if(!(big || z>=1000 || cs.position==='fixed' || cs.position==='sticky')) return null;
      return {tag:el.tagName.toLowerCase(), id:(el.id||el.className||'').toString().slice(0,60),
              z:z, txt:(el.innerText||'').replace(/\s+/g,' ').trim().slice(0,80)};
    })(%d,%d)""" % (cx, cy))


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
    ov = _overlay_over(xo)
    warn = ""
    if ov:
        warn = ("BLOCKED FIRST: a '%s' overlay (%s%s) covers the challenge center — a click at "
                "these coords hits the overlay, not the control. Dismiss it first (accept/reject "
                "the cookie/consent banner), then re-run solve_challenge(). "
                % (ov.get("txt") or ov.get("tag"), ov.get("id") or ov.get("tag"),
                   (" z=%s" % ov["z"]) if ov.get("z") else ""))
    return ("vision:%s — %schallenge sealed in a cross-origin iframe; the DOM can't be read, but CDP "
            "input passes through by coordinate. %s. Hint: %s. Screenshot: %s. Act by COORDS "
            "(click_xy / press_hold((x,y),s) / drag((x,y),to=(x2,y2))), then confirm with "
            "challenge_cleared(); if it didn't clear, re-screenshot and adjust."
            % (vendor, warn, rect, hint, shot or "capture one with capture_screenshot()"))


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
    # A challenge widget may never RENDER in a hidden tab: DataDome sits on its loader and
    # never draws the slider while document.visibilityState is "hidden". Every tab this tool
    # opens is backgrounded on purpose — that is the promise it makes to an operator's
    # keyboard. On an unattended browser there is no keyboard to protect, so foreground the
    # page and let the widget exist. (Page.setWebLifecycleState does NOT do this; it changes
    # the frozen/active lifecycle, not visibilityState. Only a real foreground does.)
    if os.environ.get("HORSE_BROWSER_UNATTENDED"):
        try:
            cdp("Page.bringToFront")
        except Exception:
            pass
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
    # Cross-origin, but not out of reach: the frame is its own CDP target, so read the control
    # from INSIDE it and act on its real rect. Only when that fails is this genuinely a
    # perception problem, and only then does vision earn its handoff.
    xo = _xorigin_challenge()
    fc = _frame_control(xo)
    if fc and fc["kind"] == "blocked":
        # Not solvable by anyone — the address is refused, not the browser doubted. Saying
        # "vision:" here would send the agent to squint at a dead end and would tell the
        # operator the wrong thing about why the site did not open.
        return ("blocked:%s — this is a block page, not a challenge: no solvable control in the "
                "frame. Nothing to solve; the egress address is refused here. Try another exit "
                "or leave this site alone." % xo.get("vendor", "vendor"))
    if fc:
        try:
            if fc["kind"] == "press-hold":
                press_hold((fc["x"], fc["y"]), seconds=hold_seconds)
                for _ in range(9):
                    _it.sleep(1.0)
                    res = challenge_cleared()
                    if res.startswith("cleared"):
                        return "solved:frame-hold (%d,%d) %s — %s" % (fc["x"], fc["y"], fc["why"], res)
            elif fc["kind"] == "slider":
                # A puzzle slider has a RIGHT answer, and dragging to the end is not it. If the
                # frame gives us the two images (background with the notch, and the cut-out
                # piece), solve for the offset; only fall back to a full-width drag for the
                # plain "slide to the end" kind, where any travel past the latch works.
                dx = None
                try:
                    tid = iframe_target({"cloudflare": "challenges.cloudflare.com",
                                         "datadome": "captcha-delivery", "hcaptcha": "hcaptcha.com",
                                         "perimeterx": "perimeterx", "arkose": "arkoselabs",
                                         "recaptcha": "recaptcha/api2"}.get(xo.get("vendor"), ""))
                    imgs = sorted(frame_images(tid), key=lambda i: -(i["w"] * i["h"])) if tid else []
                    if len(imgs) >= 2:
                        bg, pc = imgs[0], imgs[-1]
                        # Only when the pair actually LOOKS like background + puzzle piece.
                        # Not every slider is a puzzle: DataDome's common one is slide-to-the-end
                        # with no piece at all, and running a correlation over whatever two
                        # images the frame happened to contain hung the solve indefinitely —
                        # slider_gap runs in this process, so no IPC timeout can rescue it.
                        piecey = (pc["w"] <= bg["w"] * 0.5 and pc["h"] <= bg["h"] * 1.2
                                  and pc["w"] * pc["h"] <= 40000
                                  and bg["w"] * bg["h"] <= 1200000)
                        if piecey:
                            hit = slider_gap(bg["b64"], pc["b64"])
                            if hit["score"] >= 0.6:
                                # Background pixels → page pixels: the image is drawn across the
                                # frame's width, so scale before trusting the offset.
                                scale = xo["w"] / float(bg["w"]) if bg["w"] else 1.0
                                dx = int(hit["x"] * scale)
                except Exception:
                    dx = None                                    # unreadable → fall through
                if dx:
                    how = "puzzle-offset %dpx" % dx
                    drag((fc["x"], fc["y"]), dx=dx)
                elif fc.get("dest_x"):
                    how = "aimed at destination (%d,%d)" % (fc["dest_x"], fc["dest_y"])
                    # Aim at the destination the widget drew for us rather than shoving right
                    # and trusting the latch. On a full-page frame the old fallback computed
                    # 0.72 x 1665px = 1199px of travel for a 217px track.
                    drag((fc["x"], fc["y"]), to=(fc["dest_x"], fc["dest_y"]))
                else:
                    how = "no destination found — full-width shove"
                    drag((fc["x"], fc["y"]), dx=max(180, int(min(xo["w"], 420) * 0.72)))
                _it.sleep(2.0)
                res = challenge_cleared()
                if res.startswith("cleared"):
                    return "solved:frame-slide from (%d,%d), %s — %s" % (
                        fc["x"], fc["y"], how, res)
            else:                                   # checkbox / button
                click_xy(fc["x"], fc["y"])
                for _ in range(6):
                    _it.sleep(1.0)
                    res = challenge_cleared()
                    if res.startswith("cleared"):
                        return "solved:frame-click (%d,%d) %s — %s" % (fc["x"], fc["y"], fc["why"], res)
        except Exception as e:
            return _vision_handoff(xo, kind) + (" [frame-control attempt failed: %r]" % (e,))
        # Located and acted on, but it did not clear — that IS a perception problem now (an
        # image grid behind the checkbox, a puzzle needing the gap found). Do NOT reuse the
        # sealed-iframe brief here: it opens with "the DOM can't be read", which is false in
        # exactly this branch — we just read it, and told the agent coordinates that came from
        # reading it. An agent that is handed accurate coordinates under a claim they could not
        # have been obtained has no reason to trust them.
        shot = _vision_shot()
        return ("vision:%s — the control WAS read from the frame: %s at (%d,%d)%s, and acting on "
                "it did not clear the challenge. That is a second stage (an image grid behind the "
                "checkbox, a puzzle whose gap must be found), and it needs perception, not another "
                "gesture. Look at the screenshot%s, then act by coordinate; confirm with "
                "challenge_cleared()."
                % (xo.get("vendor", "challenge") if xo else kind, fc["kind"], fc["x"], fc["y"],
                   " (%dx%d)" % (fc["w"], fc["h"]) if fc.get("w") else "",
                   " " + shot if shot else ""))
    if xo or kind in ("checkbox", "hold", "drag"):
        # Before sending anyone to look at a widget: is it already satisfied? A managed challenge
        # (Turnstile, reCAPTCHA v3) can pass on its own within a second or two of load, leaving a
        # token behind and a widget still on the page. Measured on Cloudflare's own demo, where
        # this returned a vision brief for a challenge that had already issued a 21-char token.
        # Escalating a solved challenge is worse than not solving one — it spends an agent's turn
        # and reports the wrong thing about the page.
        res = challenge_cleared()
        if res.startswith("cleared"):
            return "none:" + res
        return _vision_handoff(xo, kind)
    return "none"


# --- deprecated aliases: renamed verbs kept as warn-once shims, so already-running agent sessions
# (which loaded the OLD rule at start) and any un-migrated script get the new verb + a one-line
# notice instead of a NameError. NOT listed by `verbs --json`. Remove a release after the fleet migrates.
_RENAMED = {"bh_open": "open_tab", "bh_list": "list_tabs", "bh_switch_tab": "switch_tab",
            "new_tab": "open_tab", "click_at_xy": "click_xy", "fill_input": "type_into", "press_key": "press"}
_renames_warned = set()


def _renamed_alias(old, new):
    target = globals()[new]
    def _shim(*a, **k):
        if old not in _renames_warned:
            _renames_warned.add(old)
            print("\U0001F434 horse-browser: %s() was renamed to %s() \u2014 update your script "
                  "(deprecated alias, will be removed)." % (old, new), file=sys.stderr)
        return target(*a, **k)
    _shim.__name__ = old
    _shim.__doc__ = "DEPRECATED \u2014 renamed to %s()." % new
    _shim._renamed_to = new
    return _shim


for _o, _n in _RENAMED.items():
    globals()[_o] = _renamed_alias(_o, _n)
