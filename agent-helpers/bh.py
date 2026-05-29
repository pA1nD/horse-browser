# test-brave helpers for browser-harness.
#
# Copy (or symlink) this into browser-harness's auto-loaded workspace file:
#   ~/Developer/browser-harness/agent-workspace/agent_helpers.py
# It is loaded on every browser-harness call, giving you bh_open / bh_switch_tab
# / bh_list. Open tabs with bh_open(url), never raw new_tab(url) / goto_url(url).

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
    # Brave over your current app). Tab-strip activation goes through the
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
