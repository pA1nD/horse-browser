import base64
import io

import pytest
from PIL import Image


@pytest.fixture(autouse=True)
def _no_session_identity(monkeypatch, tmp_path):
    """Unit tests must not inherit the invoking agent's session identity — with
    HORSE_SESSION/CLAUDE_CODE_SESSION_ID set, cdp()'s auto-home path activates and
    tests would touch the live extension/daemon. Likewise the persisted driven-tab
    file must come from a tmp dir, not the operator's real ~/.config."""
    for var in ("HORSE_SESSION", "HORSE_LANE", "CLAUDE_CODE_SESSION_ID", "BU_NAME"):
        monkeypatch.delenv(var, raising=False)
    from horse_harness import helpers
    monkeypatch.setattr(helpers, "_hb_current_file",
                        lambda: str(tmp_path / "hb-current"), raising=False)


class Clock:
    """A stand-in for `time` where nothing actually waits.

    The gestures read the clock to decide where the pointer should be, so it has to advance for
    the same reasons it would in reality: sleeping advances it, and so does every dispatch, by
    the cost being simulated. That makes the injected dispatch cost the only variable — a test
    that injects a REAL sleep and asserts a real duration fails whenever the machine is busy,
    which is the exact condition those tests exist to check.
    """
    def __init__(self, dispatch_cost=0.0):
        self.now = 1000.0
        self.cost = dispatch_cost

    def monotonic(self):
        return self.now

    def time(self):
        return self.now

    def sleep(self, s):
        if s > 0:
            self.now += s

    def spend_dispatch(self):
        self.now += self.cost


class Trace:
    """What the page would have seen: every event, at the virtual time it arrived."""
    def __init__(self):
        self.events = []          # (t, type, x, y)

    @property
    def moves(self):
        return [e for e in self.events if e[1] == "mouseMoved"]

    def _stamp(self, kind):
        return next(e[0] for e in self.events if e[1] == kind)

    @property
    def held(self):
        """Press to release — the span the 14 recorded human drags measure, and the one
        drag-profile asserts. The reach beforehand is real motion but not part of it."""
        return self._stamp("mouseReleased") - self._stamp("mousePressed")

    @property
    def total(self):
        return self.events[-1][0] - self.events[0][0]

    @property
    def overshoot(self):
        """How far past the release point the pointer travelled — measured the way
        drag-profile measures it off real events, so both see the same number."""
        return max(e[2] for e in self.moves) - self._release_x()

    def _release_x(self):
        return next(e[2] for e in self.events if e[1] == "mouseReleased")


@pytest.fixture
def drag_probe(monkeypatch):
    """Run drag() on a virtual clock with a chosen per-dispatch cost. Returns a Trace."""
    from horse_harness import helpers

    def run(dispatch_cost=0.0, start=(100.0, 220.0), to=(380.0, 220.0)):
        clock = Clock(dispatch_cost)
        tr = Trace()

        def dispatch(method, **params):
            clock.spend_dispatch()
            tr.events.append((clock.now, params.get("type"),
                              params.get("x"), params.get("y")))

        monkeypatch.setattr(helpers, "_it", clock)
        monkeypatch.setattr(helpers, "_cdp_nowait", dispatch)
        monkeypatch.setattr(helpers, "cdp", lambda method, **p: dispatch(method, **p) or {})
        monkeypatch.setattr(helpers, "_pt", lambda target: target)
        monkeypatch.setattr(helpers, "_mouse", {"x": 20.0, "y": 20.0})

        helpers.drag(start, to=to)
        return tr

    return run


def make_png(width, height):
    buf = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


@pytest.fixture
def fake_png():
    return make_png
