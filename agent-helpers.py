# horse-browser helpers for browser-harness.
#
# install.sh appends these into browser-harness's agent-workspace/agent_helpers.py
# (which auto-loads on every browser-harness call), so bh_open() is available on
# the first run — no agent has to install the recipe by hand.
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


def cdp(*args, **kwargs):
    global _homed
    if not _homed:
        _homed = True              # set BEFORE homing so re-entrant cdp() is a plain passthrough
        try: _hb_home()
        except Exception: pass     # a home failure must never break cdp()
    return _real_cdp(*args, **kwargs)


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


def _label():
    return os.environ.get("CLAUDE_CODE_SESSION_ID", "")[-4:]


def _session_id():
    # The FULL session id — passed to the extension so the browser derives the tab
    # group's codename (emoji + colour + last-4) itself. Passing the whole id (not just
    # the last-4) lets any companion tool that renders the same codename — a terminal
    # statusline, a dashboard — match this group byte-for-byte.
    return os.environ.get("CLAUDE_CODE_SESSION_ID", "")


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
