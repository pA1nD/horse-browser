"""CDP WS holder + IPC relay (Unix socket on POSIX, TCP loopback on Windows). One daemon per BU_NAME.

horse-browser always supplies the CDP endpoint (BU_CDP_URL / BU_CDP_WS), so the
local-Chrome profile discovery and permission-popup flows from browser-harness are gone.
"""
import asyncio, json, os, sys, time, urllib.request
from collections import deque
from pathlib import Path

from . import _ipc as ipc
from . import paths
from cdp_use.client import CDPClient


def _load_env():
    repo_root = Path(__file__).resolve().parents[2]
    workspace = paths.workspace_dir()
    for p in (repo_root / ".env", workspace / ".env"):
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
LOG = str(ipc.log_path(NAME))
PID = str(ipc.pid_path(NAME))
BUF = 500
INTERNAL = ("chrome://", "chrome-untrusted://", "devtools://", "chrome-extension://", "about:")


def log(msg):
    open(LOG, "a", encoding="utf-8", errors="replace").write(f"{msg}\n")


async def _silent(coro):
    try:
        await coro
    except Exception:
        pass


def get_ws_url():
    if url := os.environ.get("BU_CDP_WS"):
        return url
    if url := os.environ.get("BU_CDP_URL"):
        # HTTP DevTools endpoint (e.g. http://127.0.0.1:9333) — resolve to ws via /json/version.
        deadline = time.time() + 30
        last_err = None
        base_url = url.rstrip("/")
        while time.time() < deadline:
            try:
                return json.loads(urllib.request.urlopen(f"{base_url}/json/version", timeout=5).read())["webSocketDebuggerUrl"]
            except Exception as e:
                last_err = e
                time.sleep(1)
        raise RuntimeError(f"BU_CDP_URL={url} unreachable after 30s: {last_err} -- is the horse browser running? `horse-browser` (bare) launches/heals it")
    raise RuntimeError("no CDP endpoint: BU_CDP_URL / BU_CDP_WS is unset — invoke via `horse-browser`, which owns the endpoint and sets it for you")


def is_real_page(t):
    return t["type"] == "page" and not t.get("url", "").startswith(INTERNAL)


class Daemon:
    def __init__(self):
        self.cdp = None
        self.session = None
        self.target_id = None
        self.events = deque(maxlen=BUF)
        self.dialog = None
        self.stop = None  # asyncio.Event, set inside start()

    async def attach_first_page(self):
        """Attach to a real page (or any page). Sets self.session. Returns attached target or None."""
        targets = (await self.cdp.send_raw("Target.getTargets"))["targetInfos"]
        pages = [t for t in targets if is_real_page(t)]
        if not pages:
            # No real pages - create one instead of attaching to omnibox popup.
            tid = (await self.cdp.send_raw("Target.createTarget", {"url": "about:blank"}))["targetId"]
            log(f"no real pages found, created about:blank ({tid})")
            pages = [{"targetId": tid, "url": "about:blank", "type": "page"}]
        self.session = (await self.cdp.send_raw(
            "Target.attachToTarget", {"targetId": pages[0]["targetId"], "flatten": True}
        ))["sessionId"]
        self.target_id = pages[0]["targetId"]
        log(f"attached {pages[0]['targetId']} ({pages[0].get('url','')[:80]}) session={self.session}")
        await self._enable_default_domains(self.session)
        return pages[0]

    async def _enable_default_domains(self, session_id):
        """Enable Page/DOM/Runtime/Network on a CDP session.

        Used by both initial attach and set_session (called after switch_tab/
        new_tab). Without this, helpers that depend on Network.* events —
        notably wait_for_network_idle() — silently stop receiving events
        after a tab switch, because each fresh CDP session starts with all
        domains disabled.

        Runs the four enables in parallel via gather so the worst-case time is
        bounded by a single CDP round trip rather than four sequential ones —
        important on the set_session path, where the helper's IPC socket has
        a 5s read timeout.
        """
        async def enable_one(d):
            try:
                await asyncio.wait_for(
                    self.cdp.send_raw(f"{d}.enable", session_id=session_id),
                    timeout=4,
                )
            except Exception as e:
                log(f"enable {d} on {session_id}: {e}")
        await asyncio.gather(*(enable_one(d) for d in ("Page", "DOM", "Runtime", "Network")))

    async def start(self):
        self.stop = asyncio.Event()
        url = get_ws_url()
        log(f"connecting to {url}")
        self.cdp = CDPClient(url)
        try:
            await self.cdp.start()
        except Exception as e:
            raise RuntimeError(f"CDP WS handshake failed: {e} -- is the horse browser running?")
        await self.attach_first_page()
        orig = self.cdp._event_registry.handle_event
        mark_js = "if(!document.title.startsWith('\U0001F434'))document.title='\U0001F434 '+document.title"
        async def tap(method, params, session_id=None):
            self.events.append({"method": method, "params": params, "session_id": session_id})
            if method == "Page.javascriptDialogOpening":
                self.dialog = params
            elif method == "Page.javascriptDialogClosed":
                self.dialog = None
            elif method in ("Page.loadEventFired", "Page.domContentEventFired"):
                asyncio.create_task(_silent(asyncio.wait_for(self.cdp.send_raw("Runtime.evaluate", {"expression": mark_js}, session_id=self.session), timeout=2)))
            return await orig(method, params, session_id)
        self.cdp._event_registry.handle_event = tap

    async def _activate_focus_safe(self, params):
        """Focus-safe rewrite of Target.activateTarget — THE shared-browser invariant.

        Chrome's native activateTarget calls [NSApp activate] on macOS and yanks
        the browser over whatever app the operator is in. On a browser shared by
        many agents that is never what the caller meant: they want the tab
        visible, not the operator's focus. The tab-grouper extension's
        activateTab (chrome.tabs.update) flips the visible tab inside the
        browser window without touching OS focus, so every client — helper,
        raw-CDP agent, monitor — gets the safe behaviour without opting in.
        """
        tid = params.get("targetId")
        sw_session = None
        try:
            targets = (await self.cdp.send_raw("Target.getTargets"))["targetInfos"]
            sw = next((t["targetId"] for t in targets
                       if t.get("type") == "service_worker"
                       and t.get("url", "").startswith("chrome-extension://")), None)
            if sw:
                sw_session = (await self.cdp.send_raw(
                    "Target.attachToTarget", {"targetId": sw, "flatten": True}
                ))["sessionId"]
                r = await self.cdp.send_raw(
                    "Runtime.evaluate",
                    {"expression": f"self.activateTab({json.dumps(tid)})",
                     "awaitPromise": True, "returnByValue": True},
                    session_id=sw_session,
                )
                exc = (r or {}).get("exceptionDetails")
                if not exc:
                    log(f"activateTarget({tid}) -> focus-safe extension activateTab")
                    return {"result": {}}
                # The extension answered but refused (e.g. no live tab for the
                # target). Surface its error; do NOT fall through to the native
                # call — that path steals focus.
                desc = ((exc.get("exception") or {}).get("description") or exc.get("text") or "activateTab failed")
                return {"error": desc}
        except Exception as e:
            log(f"focus-safe activate path unavailable ({e})")
        finally:
            if sw_session:
                await _silent(self.cdp.send_raw("Target.detachFromTarget", {"sessionId": sw_session}))
        # Extension unreachable (not installed / SW dead). Native fallback keeps
        # the raw capability alive on non-horse browsers — but it steals focus.
        log(f"activateTarget({tid}) falling back to NATIVE activateTarget (extension unavailable) — this steals OS focus")
        return {"result": await self.cdp.send_raw("Target.activateTarget", params)}

    async def handle(self, req):
        # Token guard for Windows TCP loopback: any local process can otherwise
        # connect and issue CDP commands. expected_token() is None on POSIX so
        # this check is a no-op there (AF_UNIX + chmod 600 is the boundary).
        expected = ipc.expected_token()
        if expected is not None and req.get("token") != expected:
            return {"error": "unauthorized"}
        meta = req.get("meta")
        # Liveness probe — lets clients confirm the listener is actually this
        # daemon and not an unrelated process that reused our port post-crash.
        # `pid` lets restart_daemon() verify the live daemon's identity before
        # signaling — protects against SIGTERM-by-stale-pid-file after PID reuse.
        if meta == "ping":        return {"pong": True, "pid": os.getpid()}
        if meta == "drain_events":
            out = list(self.events); self.events.clear()
            return {"events": out}
        if meta == "session":     return {"session_id": self.session}
        if meta == "current_tab":
            # Resolve the attached page's target info server-side. Helpers can't
            # send Target.getTargetInfo themselves: daemon strips session_id for
            # any Target.* method (browser-level call), and without a targetId
            # Chrome silently returns the *browser* target.
            if not self.target_id:
                return {"error": "not_attached"}
            try:
                info = (await self.cdp.send_raw("Target.getTargetInfo", {"targetId": self.target_id}))["targetInfo"]
            except Exception:
                return {"error": "cdp_disconnected"}
            return {"targetId": info.get("targetId"), "url": info.get("url", ""), "title": info.get("title", "")}
        if meta == "connection_status":
            if not self.target_id:
                return {"error": "not_attached"}
            try:
                info = (await self.cdp.send_raw("Target.getTargetInfo", {"targetId": self.target_id}))["targetInfo"]
            except Exception:
                return {"error": "cdp_disconnected"}
            page = None
            if is_real_page(info):
                page = {
                    "targetId": info.get("targetId"),
                    "title": info.get("title") or "(untitled)",
                    "url": info.get("url") or "",
                }
            return {"target_id": self.target_id, "session_id": self.session, "page": page}
        if meta == "set_session":
            old_session = self.session
            self.session = req.get("session_id")
            self.target_id = req.get("target_id") or self.target_id
            # Run the old-session Network.disable (defense in depth — keeps
            # background-tab traffic out of the global event buffer; the
            # consumer-side filter in wait_for_network_idle is the actual
            # correctness gate) in parallel with the four enables on the new
            # session. Different sessions, independent CDP requests. Keeps
            # the synchronous reply under the helper's 5s IPC read timeout
            # even on a remote daemon — sequentially these would have stacked
            # to ~22s worst case.
            tasks = []
            if old_session and old_session != self.session:
                async def disable_old():
                    try:
                        await asyncio.wait_for(
                            self.cdp.send_raw("Network.disable", session_id=old_session),
                            timeout=2,
                        )
                    except Exception: pass
                tasks.append(disable_old())
            tasks.append(self._enable_default_domains(self.session))
            await asyncio.gather(*tasks)
            # 🐴 tab-marker title prefix is purely cosmetic — fire-and-forget so
            # it doesn't add to the synchronous IPC budget.
            asyncio.create_task(_silent(asyncio.wait_for(
                self.cdp.send_raw(
                    "Runtime.evaluate",
                    {"expression": "if(!document.title.startsWith('\U0001F434'))document.title='\U0001F434 '+document.title"},
                    session_id=self.session,
                ),
                timeout=2,
            )))
            return {"session_id": self.session}
        if meta == "pending_dialog": return {"dialog": self.dialog}
        if meta == "shutdown":    self.stop.set(); return {"ok": True}

        method = req["method"]
        params = req.get("params") or {}
        if method == "Target.activateTarget":
            return await self._activate_focus_safe(params)
        # Browser-level Target.* calls must not use a session (stale or otherwise).
        # For everything else, explicit session in req wins; else default.
        sid = None if method.startswith("Target.") else (req.get("session_id") or self.session)
        try:
            return {"result": await self.cdp.send_raw(method, params, session_id=sid)}
        except Exception as e:
            msg = str(e)
            if "Session with given id not found" in msg and sid == self.session and sid:
                log(f"stale session {sid}, re-attaching")
                if await self.attach_first_page():
                    return {"result": await self.cdp.send_raw(method, params, session_id=self.session)}
            return {"error": msg}


async def serve(d):
    async def handler(reader, writer):
        try:
            line = await reader.readline()
            if not line: return
            resp = await d.handle(json.loads(line))
            writer.write((json.dumps(resp, default=str) + "\n").encode())
            await writer.drain()
        except Exception as e:
            log(f"conn: {e}")
            try:
                writer.write((json.dumps({"error": str(e)}) + "\n").encode())
                await writer.drain()
            except Exception:
                pass
        finally:
            writer.close()

    serve_task = asyncio.create_task(ipc.serve(NAME, handler))
    stop_task = asyncio.create_task(d.stop.wait())
    await asyncio.sleep(0.05)  # let serve() bind so sock_addr() resolves to the live endpoint
    log(f"listening on {ipc.sock_addr(NAME)} (name={NAME})")
    try:
        await asyncio.wait({serve_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        if serve_task.done(): await serve_task  # surfaces a serve crash
    finally:
        for t in (serve_task, stop_task):
            t.cancel()
            try: await t
            except (asyncio.CancelledError, Exception): pass
        ipc.cleanup_endpoint(NAME)


async def main():
    d = Daemon()
    await d.start()
    await serve(d)


def already_running():
    # Ping handshake (not a bare connect) so a stale .port file + port reuse
    # after a daemon crash doesn't make us mistake an unrelated listener for ours.
    return ipc.ping(NAME, timeout=1.0)


if __name__ == "__main__":
    if already_running():
        print(f"daemon already running on {SOCK}", file=sys.stderr)
        sys.exit(0)
    open(LOG, "w").close()
    open(PID, "w").write(str(os.getpid()))
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        log(f"fatal: {e}")
        sys.exit(1)
    finally:
        try: os.unlink(PID)
        except FileNotFoundError: pass
