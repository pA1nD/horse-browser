#!/usr/bin/env python3
# lane-hook.py — makes parallel Claude Code subagents invisible to each other in the
# browser. Agents never see or manage any of this; it's pure harness plumbing.
#
# PreToolUse (Bash): a subagent's tool calls carry its `agent_id` in the hook payload —
# the id is NOT in the subagent's env, which is byte-identical to the parent's (verified
# on Claude Code 2.1.201; hook payloads gained agent_id in 2.1.168). When such a call
# invokes horse-browser, rewrite it via updatedInput to `horse-browser --lane <agent_id>`:
# the subagent transparently gets its own daemon + tab group. No permissionDecision is
# emitted, so the (rewritten) command still goes through the normal permission flow.
#
# SubagentStop: the subagent is done — close its lane's tabs (direct CDP over a stdlib
# websocket; never launches a browser or daemon) and stop its lane daemon, so parallel
# fan-outs leave zero browser residue. Best-effort: any failure is swallowed, a hook
# must never break the agent loop.
import json
import os
import re
import signal
import subprocess
import sys

SAFE = re.compile(r"[A-Za-z0-9_-]{1,64}")
# horse-browser in command position — start of command, or after ; & | ( ` or a newline,
# optionally behind env-var assignments. Never a path segment like /path/to/horse-browser.
CMD_POS = re.compile(r"(^|[;&|(`]\s*|\n\s*)((?:[A-Za-z_][A-Za-z_0-9]*=\S*\s+)*)horse-browser(?=\s|$|;|<)")
STAMPED = re.compile(r"(horse-browser\s+--lane[= ])[A-Za-z0-9_-]+")


def pretooluse(d):
    aid = d.get("agent_id")
    ti = d.get("tool_input") or {}
    cmd = ti.get("command") or ""
    if not (aid and SAFE.fullmatch(aid) and d.get("tool_name") == "Bash"):
        return
    if STAMPED.search(cmd):
        # already stamped (the model copied an earlier rewritten command) — re-stamp
        # with the TRUE id, so a lane can never be spoofed or go stale
        new = STAMPED.sub(lambda m: m.group(1) + aid, cmd)
    else:
        new, n = CMD_POS.subn(lambda m: m.group(1) + m.group(2) + f"horse-browser --lane {aid}", cmd, count=1)
        if not n:
            return
    if new == cmd:
        return
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "updatedInput": {**ti, "command": new},
    }}))


def _port():
    p = os.environ.get("HORSE_BROWSER_PORT")
    if p:
        return p.strip()
    try:
        for line in open(os.path.expanduser("~/.config/horse-browser/config")):
            if line.startswith("PORT="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return "9223"


# ── minimal CDP-over-websocket client (stdlib only; same scheme as bin's gpu probe) ──
def _ws_connect(port, path):
    import base64
    import socket
    s = socket.create_connection(("127.0.0.1", int(port)), timeout=5)
    s.settimeout(5)
    key = base64.b64encode(os.urandom(16)).decode()
    s.sendall((f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
               "Upgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        c = s.recv(1024)
        if not c:
            raise ConnectionError("handshake closed")
        buf += c
    if b" 101 " not in buf.split(b"\r\n", 1)[0]:
        raise ConnectionError("no upgrade")
    return s


def _ws_send(s, obj):
    import struct
    d = json.dumps(obj).encode()
    n = len(d)
    m = os.urandom(4)
    h = bytearray([0x81])
    if n < 126:
        h.append(0x80 | n)
    elif n < 65536:
        h.append(0x80 | 126); h += struct.pack(">H", n)
    else:
        h.append(0x80 | 127); h += struct.pack(">Q", n)
    h += m
    s.sendall(bytes(h) + bytes(b ^ m[i % 4] for i, b in enumerate(d)))


def _ws_recv(s):
    import struct

    def rd(n):
        b = b""
        while len(b) < n:
            c = s.recv(n - len(b))
            if not c:
                raise ConnectionError("closed")
            b += c
        return b
    ln = rd(2)[1] & 0x7F
    if ln == 126:
        ln = struct.unpack(">H", rd(2))[0]
    elif ln == 127:
        ln = struct.unpack(">Q", rd(8))[0]
    return json.loads(rd(ln))


def close_lane_tabs(label):
    """Close every tab in the lane's group via the grouper extension — direct CDP,
    no daemon, and a no-op unless the browser is already up."""
    import urllib.request
    port = _port()
    info = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=3))
    path = "/" + info["webSocketDebuggerUrl"].split("/", 3)[3]
    s = _ws_connect(port, path)
    mid = [0]

    def call(method, params=None, session=None):
        mid[0] += 1
        m = {"id": mid[0], "method": method, "params": params or {}}
        if session:
            m["sessionId"] = session
        _ws_send(s, m)
        while True:
            r = _ws_recv(s)
            if r.get("id") == mid[0]:
                return r.get("result") or {}
    try:
        sw = next((t["targetId"] for t in call("Target.getTargets")["targetInfos"]
                   if t.get("type") == "service_worker"
                   and t.get("url", "").startswith("chrome-extension://")), None)
        if not sw:
            return
        sess = call("Target.attachToTarget", {"targetId": sw, "flatten": True})["sessionId"]
        r = call("Runtime.evaluate", {"expression": f"self.listTabs({json.dumps(label)})",
                                      "awaitPromise": True, "returnByValue": True}, session=sess)
        for t in (r.get("result") or {}).get("value") or []:
            if t.get("targetId"):
                call("Target.closeTarget", {"targetId": t["targetId"]})
    finally:
        s.close()


def subagentstop(d):
    aid = d.get("agent_id") or ""
    sid = d.get("session_id") or ""
    if not (aid and sid and SAFE.fullmatch(aid)):
        return
    # mirror bin/horse-browser's BU_NAME derivation exactly: hb-<last-12-of-tail>-<lane>
    tail = sid.rsplit("-", 1)[-1][-12:]
    bu = f"hb-{tail}-{aid}"
    runtime = os.path.expanduser("~/.config/browser-harness/runtime")
    pidf = os.path.join(runtime, f"bu-{bu}.pid")
    sockf = os.path.join(runtime, f"bu-{bu}.sock")
    if not (os.path.exists(pidf) or os.path.exists(sockf)):
        return  # this subagent never browsed — the common case, exit cheap
    try:
        close_lane_tabs(f"{sid}#{aid}")
    except Exception:
        pass
    try:
        pid = int(open(pidf).read().strip())
        # only kill a process that really is a browser-harness daemon (stale-pidfile guard)
        cmdline = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                                 capture_output=True, text=True, timeout=3).stdout
        if "browser_harness" in cmdline:
            os.kill(pid, signal.SIGTERM)
    except Exception:
        pass
    # the stock daemon never removes its pid/sock files (killed or self-exited) — tidy
    # them so the lane leaves zero residue; a dying daemon's own unlink is a no-op then
    for f in (pidf, sockf):
        try:
            os.unlink(f)
        except OSError:
            pass


def main():
    try:
        d = json.load(sys.stdin)
    except Exception:
        return
    ev = d.get("hook_event_name")
    if ev == "PreToolUse":
        pretooluse(d)
    elif ev == "SubagentStop":
        subagentstop(d)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
