"""LamaInpainter honours the crop budget (fake model: records what it is given)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pytest

from src.watermark.lama import LamaInpainter

pytestmark = pytest.mark.skipif(importlib.util.find_spec("torch") is None, reason="torch unavailable")


def run(mask_box, *, shape=(1080, 1920), **kw) -> tuple[list[tuple[int, int]], np.ndarray, np.ndarray]:
    """Inpaint with a model that records its input size and paints everything red."""
    import torch

    backend = LamaInpainter(Path("unused.pt"), **kw)
    seen: list[tuple[int, int]] = []

    def model(image, mask):
        seen.append(tuple(image.shape[2:]))
        out = torch.zeros_like(image)
        out[:, 0] = 1.0
        return out

    backend._model = model
    img = np.full((*shape, 3), 90, np.uint8)
    mask = np.zeros(shape, np.uint8)
    y0, y1, x0, x1 = mask_box
    mask[y0:y1, x0:x1] = 1
    return seen, backend.inpaint(img, mask), mask


def test_small_crop_keeps_the_r04_input_size():
    seen, _, _ = run((300, 350, 500, 550))  # 50x50 mask, margin 96 -> 242x242 -> padded to 248
    assert seen == [(248, 248)]


def test_large_crop_stays_inside_the_pixel_budget_without_a_visible_downscale():
    seen, out, mask = run((300, 450, 500, 900), max_pixels=105_000)  # 150x400 mask
    ((h, w),) = seen
    assert h * w <= 105_000 * 1.05  # padding to a multiple of 8 adds a little
    assert (h, w) == (216, 464)  # margin 32, native resolution (214x464 padded)
    assert np.all(out[mask == 1] == [255, 0, 0])  # the model output reaches the masked pixels


def test_only_masked_pixels_change_with_a_reduced_margin():
    _, out, mask = run((300, 450, 500, 900), max_pixels=105_000)
    img = np.full_like(out, 90)
    assert np.array_equal(out[mask == 0], img[mask == 0])


def test_budget_off_gives_the_old_crop():
    seen, _, _ = run((300, 450, 500, 900), max_pixels=0)
    assert seen == [(344, 592)]  # margin 96 both sides, padded to a multiple of 8


def test_huge_mask_is_downscaled_to_the_budget_as_a_last_resort():
    seen, _, _ = run((100, 700, 100, 800), max_pixels=105_000)
    ((h, w),) = seen
    assert h * w <= 105_000 * 1.05
