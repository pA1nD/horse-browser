# horse-browser helpers for browser-harness.
#
# install.sh installs this file as <workspace>/horse_helpers.py and appends a
# load-once stub to agent_helpers.py (which browser-harness auto-loads on every
# call), so bh_open() is available on the first run — no agent has to install the
# recipe by hand. Updates overwrite horse_helpers.py only; anything a user keeps
# in agent_helpers.py itself is never touched.
#
# What they give you (pass CDP targetIds; the extension bridges to chrome tabIds):
#   bh_open(url)         open a tab WITHOUT raising the browser over your macOS app,
#                        drop it into this session's coloured group, return targetId.
#   bh_switch_tab(tid)   focus-safe tab switch (no Target.activateTarget / NSApp).
#   bh_list()            tabs in this session's group.
#
# Why bh_open instead of new_tab/goto_url: bare goto_url navigates whatever tab is
# focused — clobbering other agents' and humans' work. bh_open never does that.

import json
import os
from browser_harness.helpers import cdp as _real_cdp, _send, goto_url, wait_for_load, current_tab


# ── auto-home: group-aware attach, from the helper side ──────────────────────────
# A stock browser-harness daemon attaches to the FIRST real page in the whole (shared)
# browser at startup — possibly another session's tab. And if its current tab later goes
# away (closed) it can fall back onto a neighbour's tab again. So a raw cdp()/page_info()/
# js()/capture_screenshot() could read, act on, or screenshot another session's page. We
# can't patch the daemon, but browser-harness MERGES this file into its helpers namespace,
# so defining cdp() here overrides it — and every helper that routes through cdp (page_info,
# js, goto_url, new_tab, capture_screenshot, …) goes through ours. We use that to home onto
# THIS session's own tab whenever the daemon's current tab is foreign — recreating
# group-aware attach without forking browser-harness.
_homed = False
_guarding = False


def cdp(*args, **kwargs):
    global _homed, _guarding
    if not _homed:
        _homed = True              # set BEFORE homing so re-entrant cdp() is a plain passthrough
        try: _hb_home()
        except Exception: pass     # a home failure must never break cdp()
    # Continuous drift guard: the daemon can be knocked off its session MID-script too
    # (a stale target-destroyed event makes it fall back onto an arbitrary tab — possibly
    # a neighbour's, which reads as cross-session contamination under multi-agent load).
    # Before each session-scoped command, cheaply compare the daemon's current tab (a
    # local meta call, no Chrome roundtrip) against the tab this session last drove; on
    # drift, re-home. Same "foreign is never legitimate" policy as _hb_home, continuous.
    method = args[0] if args else kwargs.get("method")
    if not _guarding and isinstance(method, str) and not method.startswith("Target."):
        _guarding = True
        try: _hb_guard()
        except Exception: pass
        finally: _guarding = False
    return _real_cdp(*args, **kwargs)


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
    # foreign — a neighbour's tab, or the visible one a stock daemon attached to at startup
    # / fell back onto. Runs once per CLI process (the _homed gate). It's a NO-OP when we're
    # already on an own tab, so it never clobbers the agent's own current-tab choice; it only
    # acts when the current tab isn't ours (which is never a legitimate choice). This is what
    # stops a raw cdp / page_info / js / capture_screenshot from touching a neighbour's page.
    if not _session_id():
        return
    own = [t for t in bh_list() if t.get("targetId")]
    own_ids = {t["targetId"] for t in own}
    try:
        cur = (current_tab() or {}).get("targetId")
    except Exception:
        cur = None
    if cur and cur in own_ids:
        return                              # already on one of my own tabs — leave it be
    if not own:
        bh_open("about:blank")             # no tab yet → create + home a blank in my group
        return
    # Foreign: restore the EXACT tab I last drove (persisted on every bh_switch_tab). agents
    # drive tabs in the background, so lastAccessed doesn't track which tab is "current" — the
    # persisted id does. Only if that tab is truly gone do we fall back to the newest own tab.
    want = _hb_recall()
    if want and want not in own_ids:
        import sys
        print("\U0001F434 horse-browser: the tab you were driving has closed — switched to "
              "another of your tabs (bh_list() to see them)", file=sys.stderr)
        want = None
    if want not in own_ids:
        want = max(own, key=lambda t: t.get("lastAccessed") or 0)["targetId"]
    bh_switch_tab(want)


def _hb_current_file():
    return os.path.join(os.path.expanduser("~/.config/horse-browser/current"),
                        os.environ.get("BU_NAME", "default"))


def _hb_remember(target_id):
    # Persist the tab this session is currently driving, so _hb_home can put us back on the
    # SAME tab (not a guess) after the daemon drifts onto a foreign tab.
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
    # group's codename (emoji + colour + last-4) itself. Passing the whole id (not just
    # the last-4) lets any companion tool that renders the same codename — a terminal
    # statusline, a dashboard — match this group byte-for-byte.
    #
    # bin/horse-browser resolves the agent system's identity (integrations/*/detect.sh)
    # and exports HORSE_SESSION, plus HORSE_LANE when this call belongs to a subagent
    # lane (own daemon + own group; injected by the harness, invisible to agents). The
    # CLAUDE_CODE_SESSION_ID fallback covers direct browser-harness calls that bypass
    # the launcher — it mirrors integrations/claude-code/detect.sh.
    sid = os.environ.get("HORSE_SESSION") or os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    lane = os.environ.get("HORSE_LANE", "")
    return f"{sid}#{lane}" if sid and lane else sid


def bh_switch_tab(target_id):
    # Drop-in replacement for helpers.switch_tab that does NOT call
    # Target.activateTarget (which fires [NSApp activate] on macOS and yanks
    # the browser over your current app) AND does NOT change the window's visible
    # tab: focus emulation lets us drive it fully in the background (navigate,
    # click, screenshot), so concurrent agents never flip each other's view. The
    # horse-browser monitor surfaces the busiest tabs for follow-along.
    try: cdp("Runtime.evaluate", expression="if(document.title.startsWith('\U0001F434 '))document.title=document.title.slice(3)")
    except Exception: pass
    sid = cdp("Target.attachToTarget", targetId=target_id, flatten=True)["sessionId"]
    _send({"meta": "set_session", "session_id": sid, "target_id": target_id})
    _hb_remember(target_id)   # remember the tab I'm now driving, so a drift can restore THIS one
    cdp("Emulation.setFocusEmulationEnabled", enabled=True)
    try: cdp("Runtime.evaluate", expression="if(!document.title.startsWith('\U0001F434'))document.title='\U0001F434 '+document.title")
    except Exception: pass
    return sid


_hb_reported = False  # surface the group's open tabs once per browser-harness process


def _tab_report(tabs):
    # Once per process, tell the agent which tabs its session group has open — on
    # stderr, so it never pollutes a script's stdout. Always reports the count; when
    # a lot have gone idle, adds a nudge to close what's no longer needed (agents
    # accumulate tabs otherwise). Tune via HORSE_BROWSER_TAB_NUDGE (default 5) and
    # HORSE_BROWSER_TAB_IDLE_MIN (default 10); set TAB_NUDGE to a huge number to mute.
    global _hb_reported
    if _hb_reported:
        return
    _hb_reported = True
    try:
        import time, sys
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
    # Reuse a blank tab already in MY group (e.g. the one the daemon opened on
    # attach, when this session had no tab yet) instead of leaving it stray; else
    # mint a fresh background tab. background=True keeps [NSApp activate] from firing.
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
    return ext_call("listTabs", _session_id()) or [] if _session_id() else []


# ── screenshots: a unique file per call ──────────────────────────────────────────
# Stock capture_screenshot() defaults every call — from EVERY session's daemon — to
# the ONE shared file <tmp>/shot.png. With concurrent sessions, a neighbour overwrites
# it between this session writing the shot and the agent Reading the path it printed,
# so the "screenshot" shows another session's tab. Same namespace-merge trick as cdp()
# above: defining capture_screenshot here replaces the stock one everywhere. Each call
# mints its own file (named after the session's daemon lane) and returns that path, so
# the agent's existing print-the-path-then-Read flow just works.
from browser_harness.helpers import capture_screenshot as _real_capture_screenshot
from browser_harness import _ipc as _bh_ipc

_hb_shots_swept = False


def _hb_sweep_shots(max_age_s=86400):
    # Unique names accumulate where the single shot.png didn't — drop day-old ones.
    # Once per process, and only in processes that actually screenshot.
    global _hb_shots_swept
    if _hb_shots_swept:
        return
    _hb_shots_swept = True
    import time
    now = time.time()
    try:
        for e in os.scandir(_bh_ipc._TMP):
            if e.name.startswith("shot-") and e.name.endswith(".png") and now - e.stat().st_mtime > max_age_s:
                try: os.remove(e.path)
                except OSError: pass
    except OSError:
        pass


def _hb_my_target():
    """The CDP targetId of the tab THIS session is driving — the one a screenshot means.
    The tab last driven (persisted on every bh_switch_tab) is authoritative; fall back to
    whatever the daemon currently reports."""
    tid = _hb_recall()
    if tid:
        return tid
    try:
        return (current_tab() or {}).get("targetId")
    except Exception:
        return None


def capture_screenshot(path=None, full=False, max_dim=None):
    """Save a PNG of this session's tab to a per-call unique file; returns the path.
    Args as stock browser-harness: full=capture beyond viewport, max_dim=downscale.

    Reliable for backgrounded, never-window-visible tabs under heavy multi-agent load,
    WITHOUT ever changing the window's visible tab — no hijack, no cross-session race. Two
    things make it work:
      - the launcher runs Chrome with --disable-renderer-backgrounding et al., so a
        backgrounded tab's renderer stays live and paints instead of throttling to a 2x2
        surface (the root cause of the degenerate-screenshot failures under load);
      - we capture over a DEDICATED flat session attached to OUR target, so N agents
        capturing at once never contend and never drift. Stock browser-harness captures on
        the daemon's SHARED session, which drifts onto the wrong or an uncomposited tab.
    (Raising the tab window-visible would force a paint too, but hijacks the viewer's visible
    tab — rejected. A screencast also forces a paint and is what the tab-monitor uses; it's
    safe for other sessions' current_tab (verified — it does not), just unnecessary once the
    launch flags keep renderers live, and it adds event-draining + per-frame cost. So neither
    is needed here.)"""
    if path is None:
        _hb_sweep_shots()
        import tempfile
        fd, path = tempfile.mkstemp(
            prefix=f"shot-{os.environ.get('BU_NAME', 'default')}-",
            suffix=".png", dir=str(_bh_ipc._TMP))
        os.close(fd)

    target = _hb_my_target()
    if not target:                      # no identity / no tab — fall back to stock capture
        return _real_capture_screenshot(path, full=full, max_dim=max_dim) or path

    import base64, time
    sid = None
    data = None
    try:
        sid = cdp("Target.attachToTarget", targetId=target, flatten=True)["sessionId"]
        cdp("Page.enable", session_id=sid)
        # NB: do NOT setFocusEmulationEnabled here — it leaks into the browser's global focus
        # state and makes OTHER sessions' current_tab() resolve to this tab (observed as
        # cross-session contamination under load). Background renderers are kept live at the
        # browser level instead, by the launcher's --disable-renderer-backgrounding et al.

        # Compositor-surface read on our own session. With the launcher's anti-throttle
        # flags keeping every renderer live, this rasters a backgrounded tab reliably; a
        # couple of quick retries cover a tab still warming up right after open.
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
                import sys
                print(f"CAPFALLBACK target={target[:8]}", file=sys.stderr, flush=True)
            return _real_capture_screenshot(path, full=full, max_dim=max_dim) or path
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


def _struct_png_dims(raw):
    import struct
    try:
        return struct.unpack(">II", raw[16:24])
    except Exception:
        return (0, 0)


# ── page hints: a generic per-navigation plug point ──────────────────────────────
# Anything EXECUTABLE in ~/.config/horse-browser/hints.d/ is called on navigation as
#     <hook> <url>          (env carries HORSE_SESSION / HORSE_LANE; 2.5s cap)
# and whatever it prints is surfaced to the agent — once per host per process, like
# the tab nudge. horse-browser knows nothing about the data source: one operator's
# hook curls their credential index ("this site has a stored login"), another's
# checks an internal wiki or a CRM. Hooks that are empty, slow, or failing are
# silently skipped — a hint must never break or stall browsing.
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
    import sys
    for hook in _hb_hint_hooks():
        try:
            out = subprocess.run([hook, url], capture_output=True, text=True, timeout=2.5).stdout.strip()
        except Exception:
            continue
        for line in out.splitlines():
            print(line if line.startswith("\U0001F434") else "\U0001F434 horse-browser: " + line,
                  file=sys.stderr)


# Fire hints at the "I just navigated" moment: wrapping wait_for_load catches both
# bh_open (which calls it) and the bh_switch_tab → goto_url → wait_for_load flow.
# Same namespace-merge trick as cdp()/capture_screenshot() above.
from browser_harness.helpers import wait_for_load as _real_wait_for_load


def wait_for_load(*args, **kwargs):
    r = _real_wait_for_load(*args, **kwargs)
    try:
        _hb_hints((current_tab() or {}).get("url") or "")
    except Exception:
        pass
    return r


# ── Tier 2 trusted-input layer — shipped as the sibling horse_input.py ───────────
# horse-browser splits its managed helpers by concern: THIS file drives tabs (focus-safe
# open/switch/list, per-call screenshots); horse_input.py does trusted, correct INPUT
# (real click/key events, easy-challenge gestures). We exec the sibling here so the single
# "do not edit" loader stub in agent_helpers.py bootstraps both. `_hb_path` is the path to
# THIS file, set by that stub; horse_input.py sits next to it. Missing/failed → skipped, so
# tab-driving still works even if the input file didn't ship.
try:
    _hb_input = os.path.join(os.path.dirname(_hb_path), "horse_input.py")
    exec(compile(open(_hb_input).read(), _hb_input, "exec"))
except Exception as _hb_input_err:
    import sys as _hb_isys
    print("horse-browser: couldn't load horse_input.py (%r) — re-run horse-browser's install.sh" % (_hb_input_err,), file=_hb_isys.stderr)
