"""LaMa crop plan: keep R04 behaviour for small crops, shrink the margin before downscaling.

Memory of the model grows with the crop area (about 2.6 KB per pixel), so a pixel budget caps
it; a smaller margin keeps native resolution, a downscale is the last resort.
"""

from __future__ import annotations

import math

import pytest

from src.watermark.lama_crop import CropPlan, plan_crop

IMG = (1080, 1920)  # H, W


def plan(bbox, **kw):
    kw.setdefault("margin", 96)
    kw.setdefault("min_margin", 16)
    kw.setdefault("max_pixels", 105_000)
    kw.setdefault("max_side", 1024)
    return plan_crop(bbox, IMG, **kw)


def area(p: CropPlan) -> float:
    y0, y1, x0, x1 = p.box
    return (y1 - y0) * (x1 - x0) * p.scale**2


def test_budget_off_is_the_old_behaviour_margin_and_max_side_only():
    p = plan((300, 399, 500, 599), max_pixels=0)  # 100x100 mask
    assert p == CropPlan(box=(204, 496, 404, 696), margin=96, scale=1.0)
    big = plan((100, 899, 100, 999), max_pixels=0, max_side=512)  # 800x900 mask
    assert big.margin == 96 and big.scale == pytest.approx(512 / max(*_size(big.box)))


def _size(box):
    return box[1] - box[0], box[3] - box[2]


def test_small_crop_under_the_budget_is_unchanged():
    p = plan((300, 349, 500, 549))  # 50x50 mask -> 242x242 crop = 58.6k px
    assert p.margin == 96 and p.scale == 1.0 and p.box == (204, 446, 404, 646)


def test_margin_shrinks_before_anything_is_downscaled():
    # mask 150x400: margin 96 -> 342x592 = 202k px, over the budget; margin 32 fits (99k)
    p = plan((400, 549, 700, 1099))
    assert p.margin == 32 and p.scale == 1.0
    assert area(p) <= 105_000
    # the largest margin (steps of 8) that fits was chosen: one step more would not fit
    bigger = plan((400, 549, 700, 1099), margin=40, min_margin=40)
    assert area(bigger) > 105_000


def test_downscale_is_the_last_resort_and_respects_the_budget():
    # mask 366x363 (4zida logo at scale 1.33): even margin 16 gives ~157k px
    p = plan((300, 665, 500, 862))
    assert p.margin == 16 and 0.7 < p.scale < 1.0
    assert area(p) <= 105_000 * 1.001


def test_max_side_still_caps_the_longest_side():
    p = plan((100, 140, 100, 1500), max_side=512)  # a very wide strip
    h, w = _size(p.box)
    assert max(h, w) * p.scale <= 512 + 1e-6


def test_crop_is_clipped_to_the_image_and_area_counts_the_clipped_box():
    p = plan((0, 99, 0, 99))  # mask in the corner: the margin falls off the image
    y0, y1, x0, x1 = p.box
    assert y0 == 0 and x0 == 0 and y1 <= IMG[0] and x1 <= IMG[1]
    assert p.margin == 96 and p.scale == 1.0  # 195x195 after clipping: fits


def test_margin_never_leaves_its_bounds():
    for bbox in [(300, 340, 300, 340), (200, 700, 200, 900), (0, 1079, 0, 1919)]:
        p = plan(bbox)
        assert 16 <= p.margin <= 96 and 0 < p.scale <= 1.0


def test_plan_is_deterministic():
    assert plan((250, 600, 400, 900)) == plan((250, 600, 400, 900))
    assert math.isfinite(plan((250, 600, 400, 900)).scale)
