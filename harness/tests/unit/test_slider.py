"""slider_gap — finding where a puzzle piece belongs.

Tested against GENERATED puzzles whose answer this file chose, hundreds of them, because a
live captcha gives no ground truth: you cannot ask GeeTest where it put the notch, and it moves
every load. Synthetic puzzles are the only way to get an accuracy number rather than an
impression, and the only way to notice a regression that makes the solver 60% instead of 95%.

The fixture models the real shape: the notch is the background DARKENED, not a flat grey box.
That distinction matters — a flat box is trivially findable by edge energy, and a solver tuned
against it fails on the real thing.

Assertions are statistical (median, hit rate) rather than per-case, because a correlation
solver is allowed to miss a puzzle whose piece is mostly sky. What must not happen is the
distribution moving.
"""
import random

import pytest

from horse_harness.helpers import slider_gap

PIL = pytest.importorskip("PIL")
from PIL import Image, ImageDraw, ImageFilter  # noqa: E402


def _puzzle(w=320, h=160, piece=52, flat=False, seed=None):
    """(background_with_notch, piece_image, true_x, true_y)."""
    rnd = random.Random(seed)
    bg = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(bg)
    if flat:
        # A near-uniform field: the honest hard case. The piece correlates with everywhere,
        # so the score must come out low and the caller must be able to see that.
        d.rectangle([0, 0, w, h], fill=(140, 150, 160))
    else:
        for _ in range(300):
            x, y = rnd.randrange(w), rnd.randrange(h)
            d.ellipse([x, y, x + rnd.randrange(10, 50), y + rnd.randrange(10, 50)],
                      fill=(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)))
        bg = bg.filter(ImageFilter.GaussianBlur(1.0))
    gx, gy = rnd.randrange(90, w - piece - 10), rnd.randrange(6, h - piece - 6)
    pc = bg.crop((gx, gy, gx + piece, gy + piece))
    bg.paste(Image.blend(pc, Image.new("RGB", (piece, piece), (0, 0, 0)), 0.55), (gx, gy))
    return bg, pc, gx, gy


def _run(n, **kw):
    errs = []
    for i in range(n):
        bg, pc, gx, _gy = _puzzle(seed=1000 + i, **kw)
        errs.append(abs(slider_gap(bg, pc)["x"] - gx))
    errs.sort()
    return errs


def test_finds_the_gap_across_many_textured_puzzles():
    errs = _run(40)
    median = errs[len(errs) // 2]
    within3 = sum(1 for e in errs if e <= 3) / len(errs)
    # Measured at median 0px / 93% within 2px. The floor is set well below that so ordinary
    # noise does not fail the suite, but a method regression does: hand-rolled edge scoring,
    # which this replaced, sat at a median of 50-69px and would not clear a single assertion.
    assert median <= 3, "median error %dpx — the matcher is not locking on" % median
    assert within3 >= 0.80, "only %.0f%% within 3px" % (within3 * 100)


def test_survives_a_narrow_piece_and_an_off_centre_notch():
    errs = _run(15, piece=34)
    assert errs[len(errs) // 2] <= 4, "median %dpx with a small piece" % errs[len(errs) // 2]


def test_score_is_low_when_the_image_cannot_answer():
    """A flat background correlates with everything. The solver may return any x — what it must
    NOT do is report high confidence, because the caller decides whether to drag on that."""
    bg, pc, _gx, _gy = _puzzle(flat=True, seed=7)
    r = slider_gap(bg, pc)
    assert r["score"] < 0.95, "flat field reported score %.3f — callers would trust it" % r["score"]


def test_confident_answers_really_are_confident():
    good = [slider_gap(*_puzzle(seed=200 + i)[:2]) for i in range(8)]
    assert all(g["score"] > 0.6 for g in good), [g["score"] for g in good]


def test_accepts_png_bytes_and_base64_as_well_as_images():
    import base64, io
    bg, pc, gx, _ = _puzzle(seed=99)
    def png(im):
        b = io.BytesIO(); im.save(b, "PNG"); return b.getvalue()
    from_img = slider_gap(bg, pc)["x"]
    from_bytes = slider_gap(png(bg), png(pc))["x"]
    from_b64 = slider_gap(base64.b64encode(png(bg)).decode(), base64.b64encode(png(pc)).decode())["x"]
    assert from_img == from_bytes == from_b64, (from_img, from_bytes, from_b64)
    assert abs(from_img - gx) <= 3


def test_refuses_a_piece_larger_than_the_background():
    bg, _pc, _gx, _gy = _puzzle(seed=5)
    with pytest.raises(ValueError):
        slider_gap(bg, Image.new("RGB", (400, 400)))
