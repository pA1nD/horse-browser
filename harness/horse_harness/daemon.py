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

# The agent-session process this daemon belongs to (set by the launcher via the
# integration's detect.sh). When the anchor dies, the watchdog reaps this session's
# tabs and shuts the daemon down. Absent/unverifiable → the watchdog never runs.
ANCHOR_PID = os.environ.get("BH_ANCHOR_PID")
ANCHOR_START = os.environ.get("BH_ANCHOR_START", "").strip()


def _session_label():
    """The session identity string the extension groups tabs under (see helpers._session_id)."""
    sid = os.environ.get("HORSE_SESSION") or os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    lane = os.environ.get("HORSE_LANE", "")
    return f"{sid}#{lane}" if sid and lane else sid


def _bound_file():
    return Path(os.path.expanduser("~/.config/horse-browser/current")) / NAME


def _bound_target():
    """The tab this session last drove (persisted by set_session / helpers)."""
    try:
        return _bound_file().read_text().strip() or None
    except OSError:
        return None


def _remember_target(tid):
    try:
        f = _bound_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(tid or "")
    except OSError:
        pass
    _track_target(tid)   # …and into the registry: the daemon mints tabs too


def _tabs_file():
    return Path(os.path.expanduser("~/.config/horse-browser/tabs")) / NAME


def _tracked_tabs():
    """Every tab this session claimed (helpers._hb_track / _track_target below).

    THE answer to "which tabs are mine" — not a fallback for one. The tab group renders
    this list; it never sources it. Same file and convention as _bound_file.
    """
    try:
        v = json.loads(_tabs_file().read_text())
        return [t for t in v if isinstance(t, str)] if isinstance(v, list) else []
    except (OSError, ValueError):
        return []


def _track_target(tid):
    """Record a tab as this session's — the mirror of helpers._hb_track, same file.

    attach_first_page mints an about:blank of its own, so the daemon has to record one
    too. Without this that tab exists ONLY in the extension's tab group, which makes it
    invisible the moment there is no extension — grouped, undiscoverable, and leaked.
    """
    if not tid:
        return
    try:
        ids = [t for t in _tracked_tabs() if t != tid]
        ids.append(tid)
        f = _tabs_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(ids[-64:]))
    except OSError:
        pass


def _anchor_alive():
    """False only when the anchor process is verifiably gone (or its PID was reused)."""
    try:
        pid = int(ANCHOR_PID)
    except (TypeError, ValueError):
        return True                       # no/garbage anchor — unverifiable, assume alive
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        pass                              # e.g. EPERM — process exists
    if ANCHOR_START:
        # BH_ANCHOR_START is `ps -o lstart=` output — the same fingerprint
        # lifecycle._process_start_time returns on macOS. A mismatch means the
        # PID was reused by an unrelated process: the session is gone.
        from .lifecycle import _process_start_time
        st = _process_start_time(pid)
        if st is not None and str(st).strip() != ANCHOR_START:
            return False
    return True


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
        """Attach to THIS session's tab. Sets self.session. Returns attached target or None.

        The shared-browser isolation invariant lives here: with a session identity
        present, the daemon only ever attaches to the tab this session last drove
        (the persisted binding) — never to a neighbour's tab. No bound tab alive →
        mint a fresh about:blank and group it into the session's tab group, so a
        raw cdp()/page_info()/js() before any open_tab still lands on OWN ground.
        Without identity (manual/default use) the legacy first-real-page pick stays.
        """
        targets = (await self.cdp.send_raw("Target.getTargets"))["targetInfos"]
        label = _session_label()
        pick = None
        bound = _bound_target()
        if bound:
            pick = next((t for t in targets if t["targetId"] == bound and t.get("type") == "page"), None)
        if pick is None and not label:
            pages = [t for t in targets if is_real_page(t)]
            if pages:
                pick = pages[0]
        if pick is None:
            tid = (await self.cdp.send_raw(
                "Target.createTarget", {"url": "about:blank", "background": True}
            ))["targetId"]
            log(f"no {'bound' if label else 'real'} tab, created about:blank ({tid})")
            _track_target(tid)      # claim before painting: the registry is the truth
            if label:
                status, err = await self._ext_eval(f"self.groupTab({json.dumps(tid)}, {json.dumps(label)})")
                if status != "ok":
                    log(f"groupTab({tid}) failed: {err}")
            pick = {"targetId": tid, "url": "about:blank", "type": "page"}
        self.session = (await self.cdp.send_raw(
            "Target.attachToTarget", {"targetId": pick["targetId"], "flatten": True}
        ))["sessionId"]
        self.target_id = pick["targetId"]
        _remember_target(self.target_id)
        log(f"attached {pick['targetId']} ({pick.get('url','')[:80]}) session={self.session}")
        await self._enable_default_domains(self.session)
        return pick

    async def _ext_eval(self, expression):
        """Evaluate JS in the tab-grouper extension's service worker.
        Returns ("ok", value) | ("refused", why) — the extension answered with an
        exception | ("unreachable", why) — no SW / transport failure."""
        sw_session = None
        try:
            targets = (await self.cdp.send_raw("Target.getTargets"))["targetInfos"]
            sw = next((t["targetId"] for t in targets
                       if t.get("type") == "service_worker"
                       and t.get("url", "").startswith("chrome-extension://")), None)
            if not sw:
                return ("unreachable", "extension service worker not found")
            sw_session = (await self.cdp.send_raw(
                "Target.attachToTarget", {"targetId": sw, "flatten": True}
            ))["sessionId"]
            r = await self.cdp.send_raw(
                "Runtime.evaluate",
                {"expression": expression, "awaitPromise": True, "returnByValue": True},
                session_id=sw_session,
            )
            exc = (r or {}).get("exceptionDetails")
            if exc:
                return ("refused", ((exc.get("exception") or {}).get("description")
                                    or exc.get("text") or "extension call failed"))
            return ("ok", ((r or {}).get("result") or {}).get("value"))
        except Exception as e:
            return ("unreachable", str(e))
        finally:
            if sw_session:
                await _silent(self.cdp.send_raw("Target.detachFromTarget", {"sessionId": sw_session}))

    async def _enable_default_domains(self, session_id):
        """Enable Page/DOM/Runtime/Network on a CDP session.

        Used by both initial attach and set_session (called after switch_tab/
        open_tab). Without this, helpers that depend on Network.* events —
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
        if ANCHOR_PID and _session_label():
            asyncio.create_task(self._watchdog())
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
        status, value = await self._ext_eval(f"self.activateTab({json.dumps(tid)})")
        if status == "ok":
            log(f"activateTarget({tid}) -> focus-safe extension activateTab")
            return {"result": {}}
        if status == "refused":
            # The extension answered but refused (e.g. no live tab for the target).
            # Surface its error; do NOT fall through to the native call — that path
            # steals focus.
            return {"error": value}
        # Extension unreachable (not installed / SW dead). Native fallback keeps
        # the raw capability alive on non-horse browsers — but it steals focus.
        log(f"activateTarget({tid}) falling back to NATIVE activateTarget (extension unavailable: {value}) — this steals OS focus")
        return {"result": await self.cdp.send_raw("Target.activateTarget", params)}

    async def _watchdog(self, interval=30):
        """Self-reap: when the anchoring agent-session process dies, close this
        session's tabs and shut down — no launcher sweep needed for a live daemon.
        (The launcher's reap_orphan_* stays as backstop for CRASHED daemons, which
        by definition can't reap after death.)"""
        while not self.stop.is_set():
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=interval)
                return                      # clean shutdown requested elsewhere
            except asyncio.TimeoutError:
                pass
            if _anchor_alive():
                continue
            await asyncio.sleep(2)          # one beat + re-check before acting
            if _anchor_alive():
                continue
            log(f"anchor {ANCHOR_PID} gone — reaping session tabs + shutting down")
            try:
                await self._reap_own_tabs()
            except Exception as e:
                log(f"self-reap failed: {e}")
            try:
                _bound_file().unlink(missing_ok=True)   # the binding dies with the session
                _tabs_file().unlink(missing_ok=True)    # …and so does its tab registry
            except OSError:
                pass
            self.stop.set()
            return

    async def _reap_own_tabs(self):
        """Close this session's tabs — from the registry, over plain CDP.

        No extension in this path at all. It used to ask listTabs which tabs carried our
        group title, which meant the one cleanup you most need when things have gone
        wrong depended on a service worker being awake, an extension being installed,
        and a groupTab call having succeeded earlier. The registry needs none of that,
        and works identically on a browser we didn't launch.
        """
        label = _session_label()
        if not label:
            return
        ids = _tracked_tabs()
        if self.target_id and self.target_id not in ids:
            ids.append(self.target_id)      # bound tab, if it predates the registry
        if not ids:
            return                          # nothing claimed ⇒ nothing of ours to close
        for tid in ids:
            await _silent(self.cdp.send_raw("Target.closeTarget", {"targetId": tid}))
        log(f"self-reap: closed {len(ids)} tab(s) for {label[-8:]}")

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
        # `endpoint` is the CDP endpoint this daemon was SPAWNED with (its env is frozen
        # for life). Several browsers run side by side, one per agent; a daemon is reused
        # by name, so a caller pointed at a different browser has to be able to tell that
        # this one is pinned elsewhere and rebuild it — see lifecycle.ensure_daemon.
        if meta == "ping":        return {"pong": True, "pid": os.getpid(),
                                          "endpoint": os.environ.get("BU_CDP_WS") or os.environ.get("BU_CDP_URL")}
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
            _remember_target(self.target_id)   # persist the binding: bound-tab attach + drift guard read it
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
    # Singleton lock closes the cold-start spawn race: if a sibling daemon won the
    # bind between our already_running() check and now, we get None and exit BEFORE
    # connecting to Chrome — no orphan daemon/websocket. _lock is held for our whole
    # life (module-global so it isn't GC'd); the OS frees it if we crash.
    _lock = ipc.singleton_lock(NAME)
    if _lock is None:
        print(f"daemon for {NAME} already starting/running", file=sys.stderr)
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
