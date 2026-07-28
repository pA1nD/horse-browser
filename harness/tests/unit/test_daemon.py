import asyncio

from horse_harness import daemon


class _FakeCDP:
    """Records send_raw calls so tests can assert which CDP methods fired."""

    def __init__(self):
        self.calls = []  # list of (method, params, session_id)

    async def send_raw(self, method, params=None, session_id=None):
        self.calls.append((method, params, session_id))
        # Set-session/initial-attach paths only need a benign response.
        return {}


def _fresh_daemon():
    d = daemon.Daemon()
    d.cdp = _FakeCDP()
    return d


def test_set_session_enables_all_four_default_domains_on_new_session():
    """Regression: switch_tab() / open_tab() in helpers.py route through the
    `set_session` IPC, which previously only enabled Page on the new
    session. With Network disabled, wait_for_network_idle() silently stops
    receiving events after a tab switch. Initial attach enables all four
    (Page, DOM, Runtime, Network); set_session must enable the same set."""
    d = _fresh_daemon()
    new_session = "session-AFTER-switch"

    asyncio.run(d.handle({
        "meta": "set_session",
        "session_id": new_session,
        "target_id": "target-2",
    }))

    enabled_on_new = [
        method for (method, _params, sid) in d.cdp.calls
        if sid == new_session and method.endswith(".enable")
    ]
    assert set(enabled_on_new) == {"Page.enable", "DOM.enable", "Runtime.enable", "Network.enable"}, (
        f"set_session must enable Page/DOM/Runtime/Network on the new session "
        f"(parity with initial attach). Got: {enabled_on_new}"
    )
    assert d.session == new_session
    assert d.target_id == "target-2"


def test_set_session_falls_back_to_existing_target_id_when_not_provided():
    """If a caller forgets target_id (passes None), the daemon should keep its
    existing target_id rather than overwriting it with None — otherwise
    subsequent calls that depend on self.target_id would break."""
    d = _fresh_daemon()
    d.target_id = "original-target"

    asyncio.run(d.handle({
        "meta": "set_session",
        "session_id": "session-AFTER",
        "target_id": None,
    }))

    assert d.target_id == "original-target"
    assert d.session == "session-AFTER"


def test_enable_default_domains_swallows_errors_per_domain():
    """A single domain failing to enable must not prevent the others from
    being attempted — that would leave the daemon in a partially-configured
    state. Each Domain.enable call has its own try/except inside the helper."""
    class _PartialFailureCDP(_FakeCDP):
        async def send_raw(self, method, params=None, session_id=None):
            self.calls.append((method, params, session_id))
            if method == "DOM.enable":
                raise RuntimeError("simulated DOM failure")
            return {}

    d = daemon.Daemon()
    d.cdp = _PartialFailureCDP()

    asyncio.run(d._enable_default_domains("session-X"))

    attempted = [m for (m, _p, _s) in d.cdp.calls]
    assert "Page.enable" in attempted
    assert "DOM.enable" in attempted  # attempted, but raised
    assert "Runtime.enable" in attempted
    assert "Network.enable" in attempted


def test_set_session_disables_network_on_old_session_before_enabling_new():
    """When switching tabs, the previous session's Network domain must be
    disabled so background tabs (polling, SSE, etc.) stop emitting events
    into the global buffer that wait_for_network_idle reads. Initial attach
    has no `old_session` so this disable doesn't fire then."""
    d = _fresh_daemon()
    d.session = "session-OLD"
    d.target_id = "target-OLD"

    asyncio.run(d.handle({
        "meta": "set_session",
        "session_id": "session-NEW",
        "target_id": "target-NEW",
    }))

    disabled = [
        (method, sid) for (method, _params, sid) in d.cdp.calls
        if method == "Network.disable"
    ]
    assert disabled == [("Network.disable", "session-OLD")], (
        f"Network.disable must fire on the old session before re-enabling on "
        f"the new one. Got: {disabled}"
    )

    # Sanity: the new session still gets Network.enable.
    enabled_on_new = {
        method for (method, _p, sid) in d.cdp.calls
        if sid == "session-NEW" and method.endswith(".enable")
    }
    assert "Network.enable" in enabled_on_new


def test_set_session_does_not_disable_network_when_no_previous_session():
    """First set_session call (e.g. very early in startup before any attach)
    has no old_session — the Network.disable path must be skipped."""
    d = _fresh_daemon()
    d.session = None  # no prior attach

    asyncio.run(d.handle({
        "meta": "set_session",
        "session_id": "session-FIRST",
        "target_id": "target-FIRST",
    }))

    disables = [m for (m, _p, _s) in d.cdp.calls if m == "Network.disable"]
    assert disables == [], (
        f"Network.disable must not fire when there's no previous session "
        f"to disable. Got: {disables}"
    )


def test_set_session_runs_disable_and_enables_in_parallel():
    """The four Domain.enable calls (plus Network.disable on the old session)
    must run concurrently via asyncio.gather, not sequentially. With the old
    sequential code, helpers.switch_tab() would block in _send() for up to
    ~22s on a slow/remote daemon while the helper's IPC socket has a 5s
    read timeout, causing client-side socket timeouts. Verifying that all
    five CDP calls reach send_raw before any returns proves parallelization."""
    class _ConcurrencyProbeCDP:
        def __init__(self):
            self.calls = []
            self.in_flight = 0
            self.max_concurrent = 0
            self.release = None  # asyncio.Event, set inside the test loop

        async def send_raw(self, method, params=None, session_id=None):
            self.calls.append((method, params, session_id))
            self.in_flight += 1
            self.max_concurrent = max(self.max_concurrent, self.in_flight)
            try:
                await self.release.wait()
            finally:
                self.in_flight -= 1
            return {}

    async def run():
        d = daemon.Daemon()
        d.cdp = _ConcurrencyProbeCDP()
        d.session = "session-OLD"  # ensures Network.disable on old fires
        d.cdp.release = asyncio.Event()

        handle_task = asyncio.create_task(d.handle({
            "meta": "set_session",
            "session_id": "session-NEW",
            "target_id": "target-NEW",
        }))
        # Yield repeatedly until everything that's going to be in-flight is
        # in-flight. Cap iterations to avoid hanging if parallelization breaks.
        for _ in range(50):
            await asyncio.sleep(0)
            # 5 = Network.disable on OLD + 4 enables on NEW.
            if d.cdp.in_flight >= 5:
                break
        peak = d.cdp.max_concurrent
        d.cdp.release.set()
        await handle_task
        return peak, d.cdp.calls

    peak, calls = asyncio.run(run())
    assert peak == 5, (
        f"set_session must run disable + 4 enables concurrently via gather "
        f"(observed peak in-flight = {peak}; expected 5 = 1 disable on OLD + "
        f"4 enables on NEW). Sequential await would peak at 1."
    )
    # Sanity: the right calls were made.
    methods = sorted({m for (m, _p, _s) in calls})
    assert "Network.disable" in methods
    assert {"Page.enable", "DOM.enable", "Runtime.enable", "Network.enable"}.issubset(methods)


def test_set_session_first_attach_runs_four_enables_in_parallel():
    """When there's no previous session, the disable path is skipped — only
    the four enables run, still in parallel."""
    class _ConcurrencyProbeCDP:
        def __init__(self):
            self.calls = []
            self.in_flight = 0
            self.max_concurrent = 0
            self.release = None

        async def send_raw(self, method, params=None, session_id=None):
            self.calls.append((method, params, session_id))
            self.in_flight += 1
            self.max_concurrent = max(self.max_concurrent, self.in_flight)
            try:
                await self.release.wait()
            finally:
                self.in_flight -= 1
            return {}

    async def run():
        d = daemon.Daemon()
        d.cdp = _ConcurrencyProbeCDP()
        d.session = None  # no previous session
        d.cdp.release = asyncio.Event()

        handle_task = asyncio.create_task(d.handle({
            "meta": "set_session",
            "session_id": "session-FIRST",
            "target_id": "target-FIRST",
        }))
        for _ in range(50):
            await asyncio.sleep(0)
            if d.cdp.in_flight >= 4:
                break
        peak = d.cdp.max_concurrent
        d.cdp.release.set()
        await handle_task
        return peak

    peak = asyncio.run(run())
    assert peak == 4, (
        f"first set_session must run 4 enables concurrently "
        f"(observed peak = {peak}). No Network.disable should fire."
    )


def test_current_tab_meta_passes_attached_target_id():
    """Regression for issue #304: helpers.current_tab() previously sent
    Target.getTargetInfo with no targetId. The daemon strips session_id for
    Target.* methods, so the call hit the browser-level connection with empty
    params, and Chrome returned info about the *browser* target (empty
    url/title) instead of the attached page. The daemon now resolves this
    server-side using its tracked target_id."""
    class _TargetInfoCDP(_FakeCDP):
        async def send_raw(self, method, params=None, session_id=None):
            self.calls.append((method, params, session_id))
            if method == "Target.getTargetInfo":
                return {"targetInfo": {
                    "targetId": params["targetId"],
                    "url": "https://example.com/",
                    "title": "Example Domain",
                    "type": "page",
                }}
            return {}

    d = daemon.Daemon()
    d.cdp = _TargetInfoCDP()
    d.target_id = "page-target-abc"

    result = asyncio.run(d.handle({"meta": "current_tab"}))

    assert result == {
        "targetId": "page-target-abc",
        "url": "https://example.com/",
        "title": "Example Domain",
    }
    # The targetId must be passed through — that's the whole point of the fix.
    get_info_calls = [(p, s) for (m, p, s) in d.cdp.calls if m == "Target.getTargetInfo"]
    assert get_info_calls == [({"targetId": "page-target-abc"}, None)]


def test_current_tab_meta_returns_not_attached_when_no_target_id():
    """Without an attached page, current_tab() has no meaningful answer.
    Returning {error: not_attached} causes _send() to raise in helpers, which
    is the right signal for callers like ensure_real_tab() that wrap the call
    in try/except."""
    d = _fresh_daemon()
    d.target_id = None

    result = asyncio.run(d.handle({"meta": "current_tab"}))

    assert result == {"error": "not_attached"}
    # No CDP call should have been issued.
    assert d.cdp.calls == []


class _ScriptedCDP:
    """send_raw fake with per-method canned responses, recording every call."""

    def __init__(self, responses=None):
        self.calls = []  # list of (method, params, session_id)
        self.responses = responses or {}

    async def send_raw(self, method, params=None, session_id=None):
        self.calls.append((method, params, session_id))
        r = self.responses.get(method, {})
        return r(params, session_id) if callable(r) else r


_SW_TARGETS = {"targetInfos": [
    {"targetId": "PAGE1", "type": "page", "url": "https://example.com"},
    {"targetId": "SW1", "type": "service_worker", "url": "chrome-extension://abc/background.js"},
]}


def test_activate_target_routes_through_extension_never_native():
    """THE invariant: Target.activateTarget must never reach Chrome natively
    when the tab-grouper extension is present — the native call fires
    [NSApp activate] on macOS and steals the operator's focus. The daemon
    rewrites it to the extension's activateTab (chrome.tabs.update)."""
    d = daemon.Daemon()
    d.cdp = _ScriptedCDP({
        "Target.getTargets": _SW_TARGETS,
        "Target.attachToTarget": {"sessionId": "sw-session"},
        "Runtime.evaluate": {"result": {"value": 42}},
    })

    resp = asyncio.run(d.handle({"method": "Target.activateTarget", "params": {"targetId": "PAGE1"}}))

    assert resp == {"result": {}}
    methods = [m for (m, _p, _s) in d.cdp.calls]
    assert "Target.activateTarget" not in methods, (
        f"native activateTarget must not fire when the extension is live. Calls: {methods}"
    )
    evals = [(p, s) for (m, p, s) in d.cdp.calls if m == "Runtime.evaluate"]
    assert evals and evals[0][1] == "sw-session"
    assert 'self.activateTab("PAGE1")' in evals[0][0]["expression"]
    assert ("Target.detachFromTarget", {"sessionId": "sw-session"}, None) in d.cdp.calls


def test_activate_target_surfaces_extension_error_without_native_fallback():
    """If the extension answers but refuses (no live tab for the target id),
    the daemon must return that error — not fall through to the native,
    focus-stealing call."""
    d = daemon.Daemon()
    d.cdp = _ScriptedCDP({
        "Target.getTargets": _SW_TARGETS,
        "Target.attachToTarget": {"sessionId": "sw-session"},
        "Runtime.evaluate": {"result": {}, "exceptionDetails": {
            "exception": {"description": "Error: no tab for CDP target DEAD1"},
        }},
    })

    resp = asyncio.run(d.handle({"method": "Target.activateTarget", "params": {"targetId": "DEAD1"}}))

    assert "error" in resp and "no tab for CDP target" in resp["error"]
    methods = [m for (m, _p, _s) in d.cdp.calls]
    assert "Target.activateTarget" not in methods


def test_activate_target_falls_back_to_native_without_extension():
    """On a browser without the tab-grouper extension the raw capability stays
    alive: the daemon forwards the native call (and logs the focus steal)."""
    d = daemon.Daemon()
    d.cdp = _ScriptedCDP({
        "Target.getTargets": {"targetInfos": [
            {"targetId": "PAGE1", "type": "page", "url": "https://example.com"},
        ]},
    })

    resp = asyncio.run(d.handle({"method": "Target.activateTarget", "params": {"targetId": "PAGE1"}}))

    assert resp == {"result": {}}
    assert ("Target.activateTarget", {"targetId": "PAGE1"}, None) in d.cdp.calls


def _scripted_for_attach(targets, monkeypatch, bound=None, label=""):
    monkeypatch.setattr(daemon, "_bound_target", lambda: bound)
    monkeypatch.setattr(daemon, "_session_label", lambda: label)
    monkeypatch.setattr(daemon, "_remember_target", lambda tid: remembered.append(tid))
    remembered.clear()
    d = daemon.Daemon()
    created = {"n": 0}

    def create(_params, _sid):
        created["n"] += 1
        return {"targetId": "NEW-BLANK"}
    d.cdp = _ScriptedCDP({
        "Target.getTargets": {"targetInfos": targets},
        "Target.createTarget": create,
        "Target.attachToTarget": {"sessionId": "attach-session"},
        "Runtime.evaluate": {"result": {"value": 1}},
    })
    return d, created


remembered = []

_FOREIGN_PAGE = {"targetId": "NEIGHBOUR", "type": "page", "url": "https://neighbour.example"}


def test_attach_prefers_bound_tab(monkeypatch):
    """With a persisted binding alive, the daemon re-attaches THAT tab — never a guess."""
    own = {"targetId": "MYTAB", "type": "page", "url": "https://mine.example"}
    d, created = _scripted_for_attach([_FOREIGN_PAGE, own, _SW_TARGETS["targetInfos"][1]],
                                      monkeypatch, bound="MYTAB", label="sess-1")

    pick = asyncio.run(d.attach_first_page())

    assert pick["targetId"] == "MYTAB"
    assert d.target_id == "MYTAB"
    assert created["n"] == 0
    assert remembered == ["MYTAB"]


def test_attach_with_identity_never_grabs_foreign_tab(monkeypatch):
    """THE isolation invariant: a session with identity whose bound tab is gone must
    mint (and group) its own about:blank — never attach a neighbour's page."""
    d, created = _scripted_for_attach([_FOREIGN_PAGE, _SW_TARGETS["targetInfos"][1]],
                                      monkeypatch, bound="GONE", label="sess-1")

    pick = asyncio.run(d.attach_first_page())

    assert pick["targetId"] == "NEW-BLANK"
    assert created["n"] == 1
    attaches = [p for (m, p, _s) in d.cdp.calls if m == "Target.attachToTarget"]
    assert {"targetId": "NEIGHBOUR", "flatten": True} not in attaches
    evals = [p["expression"] for (m, p, _s) in d.cdp.calls if m == "Runtime.evaluate"]
    assert any("groupTab" in e and "NEW-BLANK" in e and "sess-1" in e for e in evals), evals
    creates = [p for (m, p, _s) in d.cdp.calls if m == "Target.createTarget"]
    assert creates == [{"url": "about:blank", "background": True}]


def test_attach_without_identity_keeps_legacy_first_page(monkeypatch):
    d, created = _scripted_for_attach([_FOREIGN_PAGE], monkeypatch, bound=None, label="")

    pick = asyncio.run(d.attach_first_page())

    assert pick["targetId"] == "NEIGHBOUR"
    assert created["n"] == 0


def test_anchor_alive_true_for_live_pid_and_false_for_dead(monkeypatch):
    import os
    monkeypatch.setattr(daemon, "ANCHOR_PID", str(os.getpid()))
    monkeypatch.setattr(daemon, "ANCHOR_START", "")
    assert daemon._anchor_alive() is True

    # find a PID that doesn't exist
    pid = 99999
    while True:
        try:
            os.kill(pid, 0)
            pid -= 1
        except ProcessLookupError:
            break
        except OSError:
            pid -= 1
    monkeypatch.setattr(daemon, "ANCHOR_PID", str(pid))
    assert daemon._anchor_alive() is False


def test_anchor_alive_unverifiable_defaults_to_alive(monkeypatch):
    monkeypatch.setattr(daemon, "ANCHOR_PID", None)
    assert daemon._anchor_alive() is True
    monkeypatch.setattr(daemon, "ANCHOR_PID", "not-a-pid")
    assert daemon._anchor_alive() is True


def test_reap_own_tabs_closes_every_group_tab(monkeypatch):
    monkeypatch.setattr(daemon, "_session_label", lambda: "sess-1")
    d = daemon.Daemon()
    d.cdp = _ScriptedCDP({
        "Target.getTargets": _SW_TARGETS,
        "Target.attachToTarget": {"sessionId": "sw-session"},
        "Runtime.evaluate": {"result": {"value": [
            {"targetId": "T1"}, {"targetId": "T2"}, {"targetId": None},
        ]}},
    })

    asyncio.run(d._reap_own_tabs())

    closed = [p for (m, p, _s) in d.cdp.calls if m == "Target.closeTarget"]
    assert closed == [{"targetId": "T1"}, {"targetId": "T2"}]
