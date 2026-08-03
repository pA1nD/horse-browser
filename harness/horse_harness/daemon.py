"""CDP WS holder + IPC relay (Unix socket on POSIX, TCP loopback on Windows). One daemon per BU_NAME.

horse-browser always supplies the CDP endpoint (BU_CDP_URL / BU_CDP_WS), so the
local-Chrome profile discovery and permission-popup flows from browser-harness are gone.
"""
import asyncio, contextlib, json, os, re, sys, time, urllib.request
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
    f = _tabs_file()
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    # Locked read-modify-write, mirroring helpers._hb_registry_update. os.replace() alone stops
    # a torn READ; it does not stop two writers from both reading [X] and one replacement
    # silently winning. The daemon and its own clients are exactly such a pair.
    fd = None
    try:
        import fcntl
        # Dotfiles: the launcher's reaper globs TABS/* and glob("*") skips them. A lock or temp
        # file it could see would be read as a dead session's registry and its tabs closed.
        fd = os.open(str(f.with_name("." + f.name + ".lock")), os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
    except (ImportError, OSError) as e:
        # Unlocked beats not writing at all, but this re-enables the lost-update bug the lock
        # prevents — log it, so a degraded daemon is visible instead of merely slower to trust.
        fd = None
        if not getattr(_track_target, "_warned", False):
            _track_target._warned = True
            log(f"registry writes are UNLOCKED ({e}) — concurrent claims on this lane can be lost")
    try:
        ids = [t for t in _tracked_tabs() if t != tid]
        ids.append(tid)
        tmp = f.with_name(f".{f.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(ids[-64:]))
        os.replace(tmp, f)
    except OSError:
        pass
    finally:
        if fd is not None:
            try:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                os.close(fd)
            except OSError:
                pass


@contextlib.contextmanager
def _adopt_lock():
    """Serialise "adopt the New Tab Page" across every daemon on ONE browser.

    Yields True while held, False when it could not be taken. A caller that gets False must
    NOT adopt — minting is always safe, adopting on a guess is not. That is the right way
    round: the cost of not adopting is one extra tab, the cost of adopting the same tab twice
    is two sessions driving one page.

    Keyed on the CDP endpoint, not the lane, because the thing being raced for is the
    browser's tab. Keying it per lane would serialise nothing; keying it globally would make
    daemons on unrelated browsers (multi-instance) wait on each other for no reason.
    """
    fd = None
    try:
        import fcntl
        ep = os.environ.get("BU_CDP_URL") or os.environ.get("BU_CDP_WS") or "default"
        key = re.sub(r"[^A-Za-z0-9]+", "-", ep)[-48:]
        p = _tabs_file().parent
        p.mkdir(parents=True, exist_ok=True)
        # Dotfile: the launcher's reaper globs TABS/* and glob("*") skips dotfiles. Anything
        # else here would be read as a dead session's registry and its tabs closed.
        fd = os.open(str(p / f".adopt-{key}.lock"), os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield True
    except (ImportError, OSError) as e:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
            fd = None
        log(f"cannot serialise New Tab Page adoption ({e}) — minting instead, which is safe")
        yield False
        return
    finally:
        if fd is not None:
            try:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                os.close(fd)
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
        # The fingerprint is only evidence when both sides speak the same dialect.
        # _process_start_time returns `ps -o lstart=` on macOS but /proc start-ticks on Linux,
        # while every detect.sh wrote the ps form on BOTH — so on Linux the comparison could
        # never match, every live session looked PID-reused, and the watchdog closed its tabs
        # and shut the daemon down about 32 seconds in. detect.sh now tags the value with the
        # dialect it used ("linux:…" / "darwin:…").
        #
        # An UNTAGGED or foreign-tagged value is treated as no evidence rather than as proof
        # of death. That matters twice: an older install's exported env survives an upgrade,
        # and "I cannot compare these" must never be the reason a live agent loses its tabs.
        # This check exists to catch PID reuse — a rare, recoverable event — so the safe
        # failure is to assume alive.
        from .lifecycle import _process_start_time
        want = ANCHOR_START.split(":", 1)
        if len(want) == 2 and want[0] == ("linux" if sys.platform.startswith("linux")
                                          else "darwin" if sys.platform == "darwin" else "?"):
            st = _process_start_time(pid)
            if st is not None and str(st).strip() != want[1].strip():
                return False
    return True


def log(msg):
    open(LOG, "a", encoding="utf-8", errors="replace").write(f"{msg}\n")


# Identity guard for the extension's service worker — the verbs only OUR extension defines.
# Kept in step with tools/sw_eval.py's _MINE, which learned this lesson first.
_EXT_IS_MINE = "typeof self.groupTab==='function'&&typeof self.activateTab==='function'"
_EXT_NOT_MINE = "__not_horse_sw__"

_REALCHROME = None


def _realchrome_js():
    """Source of the Tier-1 realness mask — the SAME file the extension injects as a MAIN-world
    content script. One implementation of the masking logic, two ways to deliver it: the
    content script when our extension is loaded, this when it isn't. Writing a second mask in
    Python is how the two would drift apart.
    """
    global _REALCHROME
    if _REALCHROME is None:
        try:
            _REALCHROME = (Path(__file__).resolve().parents[2]
                           / "extension" / "realchrome.js").read_text()
        except OSError:
            _REALCHROME = ""
    return _REALCHROME


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


# Chrome's own startup tab, under every Chromium's spelling of it.
NEW_TAB_URLS = ("chrome://newtab", "chrome://new-tab-page", "edge://newtab", "about:newtab")


def is_reusable_new_tab_page(t):
    """The browser's New Tab Page — nobody's work, safe to take over."""
    return t.get("type") == "page" and t.get("url", "").startswith(NEW_TAB_URLS)


def _all_tracked():
    """Every tab claimed by ANY session — the union of the registry directory.

    Adoption must never take a tab another session is driving, and the registry is the
    only thing that knows. Cheap: the same directory the reaper already walks.
    """
    out = set()
    try:
        for f in _tabs_file().parent.iterdir():
            try:
                v = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            if isinstance(v, list):
                out.update(t for t in v if isinstance(t, str))
    except OSError:
        pass
    return out


class Daemon:
    def __init__(self):
        self.cdp = None
        self.session = None
        self.target_id = None
        self.events = deque(maxlen=BUF)
        self.dialog = None
        self.stop = None  # asyncio.Event, set inside start()
        self.has_extension = None  # probed once in start(); drives _apply_realness

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
        adopted = None          # set only when we take over the browser's New Tab Page
        bound = _bound_target()
        if bound:
            pick = next((t for t in targets if t["targetId"] == bound and t.get("type") == "page"), None)
        if pick is None and label:
            # The binding is dead, but the registry may still list live tabs of ours.
            # Adopt the newest instead of minting: a bound tab dies for ordinary reasons
            # (the operator closed it, a reap raced us, the page crashed), and minting
            # on each one turns every death into a fresh about:blank nobody reaps.
            pages = {t["targetId"]: t for t in targets if t.get("type") == "page"}
            for tid in reversed(_tracked_tabs()):
                if tid in pages:
                    pick = pages[tid]
                    log(f"bound tab {bound} gone, reusing own tab {tid}")
                    break
        if pick is None and not label:
            pages = [t for t in targets if is_real_page(t)]
            if pages:
                pick = pages[0]
        if pick is None:
            # Before minting: Chrome opens a New Tab Page of its own at launch, and it is
            # nobody's work. Leaving it there means the browser sits at a dead tab beside
            # the blank we just made. The first session that needs a tab takes it over —
            # unless some session has already claimed it.
            #
            # Deciding and claiming must be ONE step, across every daemon on this browser.
            # Two cold lanes otherwise both snapshot targets, both read the registries before
            # either writes, both see the New Tab Page unclaimed, and both adopt it: two
            # sessions driving one tab, and either watchdog free to close the other's work.
            # `targets` above was fetched before this point, so it is re-read inside the lock
            # — a snapshot taken outside it is exactly the stale evidence that causes this.
            ntp = None
            with _adopt_lock() as locked:
                if locked:
                    fresh = (await self.cdp.send_raw("Target.getTargets"))["targetInfos"]
                    claimed = _all_tracked()
                    ntp = next((t for t in fresh
                                if is_reusable_new_tab_page(t) and t["targetId"] not in claimed), None)
                    if ntp:
                        _track_target(ntp["targetId"])   # claim INSIDE the lock, or it is not a claim
            if ntp:
                tid = adopted = ntp["targetId"]
                log(f"adopted the browser's New Tab Page ({tid}) instead of minting")
            else:
                tid = (await self.cdp.send_raw(
                    "Target.createTarget", {"url": "about:blank", "background": True}
                ))["targetId"]
                log(f"no {'bound' if label else 'real'} tab, created about:blank ({tid})")
            _track_target(tid)      # claim before painting: the registry is the truth
            # Paint only when there is something to paint on. has_extension is probed once in
            # start(); without this the attach path spends four CDP round trips discovering
            # the absence again, then logs a failure, for every tab minted on an unattended or
            # attached browser. The tab is already OURS either way — the registry said so a
            # line above, and the group is a rendering of that, never its source.
            if label and self.has_extension:
                status, err = await self._ext_eval(f"self.groupTab({json.dumps(tid)}, {json.dumps(label)})")
                if status != "ok":
                    log(f"groupTab({tid}) failed: {err}")
            pick = {"targetId": tid, "url": "about:blank", "type": "page"}
        self.session = (await self.cdp.send_raw(
            "Target.attachToTarget", {"targetId": pick["targetId"], "flatten": True}
        ))["sessionId"]
        self.target_id = pick["targetId"]
        if adopted:
            # Blank the adopted tab. A New Tab Page is not inert — it keeps fetching
            # (chrome-untrusted://new-tab-page/one-google-bar and friends), so adopting one
            # and leaving it there hands the session a tab that is busier than the
            # about:blank it replaced. Navigate so adoption is behaviourally identical to
            # minting, minus the extra tab.
            try:
                await self.cdp.send_raw("Page.navigate", {"url": "about:blank"},
                                        session_id=self.session)
            except Exception as e:
                log(f"blanking adopted tab {adopted}: {e}")
        _remember_target(self.target_id)
        log(f"attached {pick['targetId']} ({pick.get('url','')[:80]}) session={self.session}")
        await self._enable_default_domains(self.session)
        return pick

    async def _ext_eval(self, expression):
        """Evaluate JS in OUR tab-grouper extension's service worker.

        Returns ("ok", value) | ("refused", why) — ours answered with an exception |
        ("unreachable", why) — ours isn't here / transport failure.

        "The first chrome-extension:// worker" is NOT ours. Every Chrome ships component
        extensions of its own: a bare, freshly-created profile already has Google's
        (nkeimhogjdpnpccoofpliimaahmaaome) with a live service worker, before any operator
        installs anything. Picking blind meant evaluating our verbs in a stranger's worker —
        which reads as "the extension refused" and, since the realness probe was added, as
        "an extension is present" on browsers that have none of ours at all.

        So ask each candidate whether it is ours in the SAME round trip, the way
        tools/sw_eval.py has always done, and never run `expression` anywhere that says no.
        """
        try:
            targets = (await self.cdp.send_raw("Target.getTargets"))["targetInfos"]
        except Exception as e:
            return ("unreachable", str(e))
        cands = [t["targetId"] for t in targets
                 if t.get("type") == "service_worker"
                 and t.get("url", "").startswith("chrome-extension://")]
        if not cands:
            return ("unreachable", "no extension service worker at all")
        guarded = f"({_EXT_IS_MINE}) ? ({expression}) : '{_EXT_NOT_MINE}'"
        for sw in cands:
            sw_session = None
            try:
                sw_session = (await self.cdp.send_raw(
                    "Target.attachToTarget", {"targetId": sw, "flatten": True}
                ))["sessionId"]
                r = await self.cdp.send_raw(
                    "Runtime.evaluate",
                    {"expression": guarded, "awaitPromise": True, "returnByValue": True},
                    session_id=sw_session,
                )
                exc = (r or {}).get("exceptionDetails")
                if exc:                       # ours, and `expression` itself threw
                    return ("refused", ((exc.get("exception") or {}).get("description")
                                        or exc.get("text") or "extension call failed"))
                value = ((r or {}).get("result") or {}).get("value")
                if value == _EXT_NOT_MINE:
                    continue                  # somebody else's worker — keep looking
                return ("ok", value)
            except Exception as e:
                last = str(e)
            finally:
                if sw_session:
                    await _silent(self.cdp.send_raw("Target.detachFromTarget",
                                                    {"sessionId": sw_session}))
        return ("unreachable", f"none of {len(cands)} extension worker(s) is ours")

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
        await self._apply_realness(session_id)   # after Page+Network: both are prerequisites

    async def _apply_realness(self, session_id):
        """Mask Chrome-for-Testing's branding on a browser that has no extension to do it.

        Owned mode needs none of this and doesn't use it: the extension applies the mask
        browser-wide, with zero clients attached, to the operator's own tabs as well as ours.
        A per-session CDP override matches none of those three properties, so it is strictly
        the attached-mode stand-in — where the alternative is no mask at all.

        Consistency is the reason this is not simply used everywhere. The operator signs in by
        hand and agents inherit that session; if their tab were unmasked and ours masked, the
        sec-ch-ua would CHANGE mid-session on the same cookies, which reads worse to a
        fingerprinter than either state alone. Only a browser-wide applier avoids that, and
        browser-wide means the extension.

        Fails OPEN by nature: if this daemon dies, new tabs are unmasked and nothing says so.
        helpers.realness_ok() is the loud check.
        """
        if self.has_extension:
            return
        js = _realchrome_js()
        if js:
            # Main world, before the page's first script — the same moment the content script
            # gets in owned mode. runImmediately also covers a tab that is already loaded.
            await _silent(self.cdp.send_raw(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": js, "runImmediately": True}, session_id=session_id))
        ua = (await self.cdp.send_raw("Browser.getVersion") or {}).get("userAgent") or ""
        m = re.search(r"Chrome/(\d+)", ua)
        if m:                                   # wire half, derived from the live UA
            v = m.group(1)
            await _silent(self.cdp.send_raw("Network.setExtraHTTPHeaders", {"headers": {
                "sec-ch-ua": f'"Not;A=Brand";v="8", "Chromium";v="{v}", "Google Chrome";v="{v}"',
            }}, session_id=session_id))

    async def start(self):
        self.stop = asyncio.Event()
        url = get_ws_url()
        log(f"connecting to {url}")
        self.cdp = CDPClient(url)
        try:
            await self.cdp.start()
        except Exception as e:
            raise RuntimeError(f"CDP WS handshake failed: {e} -- is the horse browser running?")
        # Probe ONCE, here, not lazily per attach: _ext_eval is four round trips, and
        # _enable_default_domains runs on the set_session path, which the helper's IPC socket
        # only gives 5s. Cheap flag from then on.
        status, _ = await self._ext_eval("1")
        self.has_extension = (status == "ok")
        if not self.has_extension:
            log("no tab-grouper extension on this browser — realness applied per session over CDP")
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
            reaped = True
            try:
                await self._reap_own_tabs()
            except Exception as e:
                reaped = False
                log(f"self-reap failed: {e}")
            try:
                _bound_file().unlink(missing_ok=True)   # the binding dies with the session
                if reaped:
                    _tabs_file().unlink(missing_ok=True)   # …and so does its tab registry
                else:
                    # Keep it. The registry is the ONLY record of these tabs; deleting it
                    # after a failed close leaves them alive and invisible to every future
                    # reaper — an orphan no sweep can ever attribute to a session.
                    log("self-reap failed: keeping the tab registry for the launcher's sweep")
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
        # Count what actually closed. _silent() swallowing every failure meant that when the
        # anchor died with CDP already disconnected — the ordinary way a session ends badly —
        # every closeTarget raised, this returned normally, the caller believed it, and the
        # watchdog deleted the registry. The tabs were still open and now unattributable: the
        # exact orphan the registry exists to prevent.
        failed = []
        for tid in ids:
            try:
                await self.cdp.send_raw("Target.closeTarget", {"targetId": tid})
            except Exception as e:
                failed.append((tid, e))
        if failed:
            log(f"self-reap: {len(ids) - len(failed)}/{len(ids)} closed for {label[-8:]}; "
                f"{len(failed)} failed (first: {failed[0][1]})")
            raise RuntimeError(f"{len(failed)} of {len(ids)} tabs could not be closed")
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
