"""Corner LaMa geometry/compositing work without torch; model checks stay optional."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import Mock

import cv2
import numpy as np
import pytest

from src.config import get_settings
from src.watermark.lama import LamaInpainter
from src.watermark.lama_corner import (
    LamaCornerInpainter,
    _corner_crop,
    _feather_alpha,
    _reflect_to_multiple,
    get_lama_corner_inpainter,
)


def test_corner_crop_keeps_native_pixels_and_reflects_image_and_mask():
    image = np.arange(200 * 400 * 3, dtype=np.uint32).reshape(200, 400, 3).astype(np.uint8)
    mask = np.zeros((200, 400), np.uint8)
    mask[180:200, 310:400] = 1
    crop, cm, box, pads = _corner_crop(image, mask)
    assert box == (116, 200, 220, 400)
    assert pads == ((0, 64), (0, 90))
    assert crop.shape == (148, 270, 3)
    assert np.array_equal(crop[:84, :180], image[116:200, 220:400])
    assert np.array_equal(crop[84, :180], image[198, 220:400])
    assert np.array_equal(crop[:84, 180], image[116:200, 398])
    assert np.array_equal(cm[84, :180], mask[198, 220:400])
    assert np.array_equal(cm[:84, 180], mask[116:200, 398])


def test_context_margin_uses_each_dimension_not_longest_side():
    image = np.zeros((800, 1000, 3), np.uint8)
    mask = np.zeros((800, 1000), np.uint8)
    mask[300:400, 400:600] = 1
    crop, _cm, box, pads = _corner_crop(image, mask)
    assert box == (200, 500, 200, 800)
    assert crop.shape == (300, 600, 3)
    assert pads == ((0, 0), (0, 0))


def test_padding_reflects_without_repeating_boundary_and_preserves_aligned_input():
    mask = np.arange(13 * 17).reshape(13, 17)
    padded = _reflect_to_multiple(mask)
    assert padded.shape == (16, 24)
    assert padded[13, 0] == mask[11, 0]
    assert padded[0, 17] == mask[0, 15]
    assert np.array_equal(padded[:13, :17], mask)
    image = np.zeros((16, 24, 3), np.uint8)
    assert np.array_equal(_reflect_to_multiple(image), image)


def test_feather_is_inward_and_reaches_full_opacity_three_pixels_inside():
    mask = np.zeros((12, 12), np.uint8)
    mask[2:10, 2:10] = 1
    alpha = _feather_alpha(mask)
    assert np.all(alpha[mask == 0] == 0)
    assert alpha[2, 5] == pytest.approx(1 / 3)
    assert alpha[3, 5] == pytest.approx(2 / 3)
    assert alpha[4, 5] == 1


def test_corner_composites_only_expanded_mask_and_does_not_fade_at_frame_edge(monkeypatch):
    backend = LamaInpainter(Path("unused.pt"))
    corner = LamaCornerInpainter(backend)
    image = np.zeros((120, 160, 3), np.uint8)
    image[:] = (30, 60, 90)
    original = image.copy()
    mask = np.zeros(image.shape[:2], np.uint8)
    mask[90:120, 100:160] = 1
    original_mask = mask.copy()
    seen = []

    def fake_predict(crop, cm):
        seen.append((crop.shape, cm.shape))
        return np.full_like(crop, 210)

    monkeypatch.setattr(corner, "_predict_rgb", fake_predict)
    out = corner.inpaint(image, mask)
    expanded = cv2.dilate(mask, np.ones((5, 5), np.uint8))
    assert seen and seen[0][0][:2] == seen[0][1]
    assert np.array_equal(out[expanded == 0], image[expanded == 0])
    assert np.array_equal(out[-1, -1], [210, 210, 210])
    assert np.array_equal(out[88, 120], [90, 110, 130])
    assert np.array_equal(out[89, 120], [150, 160, 170])
    assert np.array_equal(image, original)
    assert np.array_equal(mask, original_mask)
    assert out.dtype == np.uint8 and out.shape == image.shape


def test_empty_mask_never_loads_torch_or_model(monkeypatch):
    corner = LamaCornerInpainter(LamaInpainter(Path("unused.pt")))
    predictor = Mock(side_effect=AssertionError("must not infer"))
    monkeypatch.setattr(corner, "_predict_rgb", predictor)
    image = np.zeros((1, 1, 3), np.uint8)
    out = corner.inpaint(image, np.zeros((1, 1), np.uint8))
    assert np.array_equal(out, image)
    assert out is not image
    predictor.assert_not_called()


@pytest.mark.parametrize("available", [False, True])
def test_factory_reuses_existing_lama_singleton_or_reports_unavailable(monkeypatch, available):
    from src.watermark import lama_corner

    backend = LamaInpainter(Path("unused.pt")) if available else None
    monkeypatch.setattr(lama_corner, "get_lama_inpainter", lambda: backend)
    corner = get_lama_corner_inpainter()
    if backend is None:
        assert corner is None
    else:
        assert corner.lama is backend
        assert get_lama_corner_inpainter().lama is backend
        assert corner.name == "lama"


@pytest.mark.skipif(importlib.util.find_spec("torch") is None, reason="torch unavailable")
def test_model_receives_rgb_float_unit_range_binary_mask_and_multiple_of_eight():
    import torch

    backend = LamaInpainter(Path("unused.pt"))
    seen = []

    def model(image, mask):
        seen.append(True)
        assert image.dtype == torch.float32
        assert image.shape[0:2] == (1, 3)
        assert image.shape[2] % 8 == image.shape[3] % 8 == 0
        assert torch.allclose(image[0, :, 0, 0], torch.tensor([1.0, 0.5, 0.0]), atol=0.003)
        assert mask.dtype == torch.float32 and mask.shape == (1, 1, *image.shape[2:])
        assert set(mask.unique().tolist()) <= {0.0, 1.0}
        assert not torch.is_grad_enabled()
        assert backend._lock.locked()
        result = torch.zeros_like(image)
        result[:, 0] = 1.0
        return result

    backend._model = model
    corner = LamaCornerInpainter(backend)
    image = np.full((13, 17, 3), (255, 128, 0), np.uint8)
    mask = np.zeros((13, 17), np.uint8)
    mask[2:6, 4:8] = 255
    out = corner._predict_rgb(image, mask)
    assert seen
    assert out.shape == image.shape and out.dtype == np.uint8
    assert np.all(out == [255, 0, 0])


MODEL = Path(get_settings().watermark_lama_model_path)
HAS_LAMA = importlib.util.find_spec("torch") is not None and MODEL.is_file()


@pytest.mark.skipif(not HAS_LAMA, reason="torch or LaMa model unavailable")
def test_real_corner_model_preserves_pixels_outside_expanded_mask():
    from tests.watermark.synth import make_background

    image = make_background(320, 240, seed=21)
    mask = np.zeros(image.shape[:2], np.uint8)
    mask[-20:, -100:] = 1
    corner = LamaCornerInpainter(LamaInpainter(MODEL))
    out = corner.inpaint(image, mask)
    expanded = cv2.dilate(mask, np.ones((5, 5), np.uint8))
    assert np.array_equal(out[expanded == 0], image[expanded == 0])
    assert not np.array_equal(out[mask > 0], image[mask > 0])
