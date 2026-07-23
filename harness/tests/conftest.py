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


def make_png(width, height):
    buf = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


@pytest.fixture
def fake_png():
    return make_png
