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
from browser_harness.helpers import cdp, _send, goto_url, wait_for_load


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


def bh_switch_tab(target_id):
    # Drop-in replacement for helpers.switch_tab that does NOT call
    # Target.activateTarget (which fires [NSApp activate] on macOS and yanks
    # the browser over your current app). Tab-strip activation goes through the
    # extension; focus emulation makes the page believe it's foregrounded.
    try: cdp("Runtime.evaluate", expression="if(document.title.startsWith('\U0001F434 '))document.title=document.title.slice(3)")
    except Exception: pass
    sid = cdp("Target.attachToTarget", targetId=target_id, flatten=True)["sessionId"]
    _send({"meta": "set_session", "session_id": sid, "target_id": target_id})
    cdp("Emulation.setFocusEmulationEnabled", enabled=True)
    try: cdp("Runtime.evaluate", expression="if(!document.title.startsWith('\U0001F434'))document.title='\U0001F434 '+document.title")
    except Exception: pass
    ext_call("activateTab", target_id)
    return sid


def bh_open(url):
    # background=True on createTarget keeps [NSApp activate] from firing.
    tid = cdp("Target.createTarget", url="about:blank", background=True)["targetId"]
    bh_switch_tab(tid)
    if url != "about:blank":
        goto_url(url)
    wait_for_load()
    if _label():
        ext_call("groupTab", tid, _label())
    return tid


def bh_list():
    return ext_call("listTabs", _label()) or [] if _label() else []
