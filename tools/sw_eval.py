#!/usr/bin/env python3
"""Run an expression inside the horse-browser extension's service worker.

The extension is the only thing that can act on a *running* browser's tabs, groups
and storage, and its service worker is reachable like any other CDP target. That
makes this the launcher's one channel INTO a live browser — used to reap dead
sessions' tabs and to tell the extension which debug port it is living on.

Stdlib only (hand-rolled websocket framing): the launcher must work on a machine
where nothing but python3 is installed.

  CLI:  sw_eval.py <port> <expression> [wait_s]   → prints the JSON result, if any
  API:  evaluate(port, expr, wait_s=0) -> value | None
"""
import base64
import json
import os
import socket
import struct
import sys
import time
import urllib.request


def http_json(port, path):
    return json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=4))


def ws_connect(port, ws_url):
    path = "/" + ws_url.split("://", 1)[1].split("/", 1)[1]
    s = socket.create_connection(("127.0.0.1", int(port)), timeout=5)
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


def ws_send(s, payload):
    d = payload.encode(); n = len(d); m = os.urandom(4); h = bytearray([0x81])
    if n < 126: h.append(0x80 | n)
    elif n < 65536: h.append(0x80 | 126); h += struct.pack(">H", n)
    else: h.append(0x80 | 127); h += struct.pack(">Q", n)
    h += m
    s.sendall(bytes(h) + bytes(b ^ m[i % 4] for i, b in enumerate(d)))


def ws_recv(s):
    def rd(n):
        b = b""
        while len(b) < n:
            c = s.recv(n - len(b))
            if not c:
                raise ConnectionError("closed")
            b += c
        return b
    _b0, b1 = rd(2); ln = b1 & 0x7f
    if ln == 126: ln = struct.unpack(">H", rd(2))[0]
    elif ln == 127: ln = struct.unpack(">Q", rd(8))[0]
    return rd(ln)


def service_worker(port, wait_s=0):
    """The extension's service-worker target, or None. An MV3 worker can be asleep or
    still registering right after launch, so callers that need it may wait for it."""
    deadline = time.monotonic() + max(0, wait_s)
    while True:
        try:
            for t in http_json(port, "/json/list"):
                if (t.get("type") == "service_worker"
                        and t.get("url", "").startswith("chrome-extension://")
                        and t.get("webSocketDebuggerUrl")):
                    return t
        except Exception:
            pass
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.5)


def evaluate(port, expr, wait_s=0, timeout=8):
    """Evaluate `expr` in the extension service worker; return its value (None on any
    failure — every caller here is best-effort and must never break the launcher)."""
    sw = service_worker(port, wait_s)
    if not sw:
        return None
    try:
        s = ws_connect(port, sw["webSocketDebuggerUrl"])
        ws_send(s, json.dumps({"id": 1, "method": "Runtime.evaluate",
                               "params": {"expression": expr, "awaitPromise": True,
                                          "returnByValue": True}}))
        s.settimeout(timeout)
        for _ in range(200):
            m = json.loads(ws_recv(s))
            if m.get("id") == 1:
                return ((m.get("result") or {}).get("result") or {}).get("value")
    except Exception:
        return None
    return None


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__.strip(), file=sys.stderr)
        sys.exit(2)
    val = evaluate(sys.argv[1], sys.argv[2], float(sys.argv[3]) if len(sys.argv) > 3 else 0)
    if val is not None:
        print(val if isinstance(val, str) else json.dumps(val))
