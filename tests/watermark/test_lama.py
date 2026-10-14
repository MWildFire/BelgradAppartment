"""LaMa: without torch/model it falls back to Telea; with the model only the mask changes.

Model tests are skipped in CI (no torch there). Locally: `uv sync --extra clip`,
`uv run otz wm-fetch-model`, then `uv run pytest tests/watermark/test_lama.py`.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pytest

from src.config import get_settings
from src.watermark.inpaint import get_inpainter
from src.watermark.pipeline import clean_array
from src.watermark.templates import load_templates
from tests.watermark.synth import apply_mark, jpeg_roundtrip, make_background

MODEL = Path(get_settings().watermark_lama_model_path)
HAS_LAMA = importlib.util.find_spec("torch") is not None and MODEL.is_file()


def test_pad_to_multiple_of_8():
    from src.watermark.lama import _pad_to_multiple

    assert _pad_to_multiple(np.zeros((13, 17, 3), np.uint8)).shape == (16, 24, 3)
    assert _pad_to_multiple(np.zeros((16, 8), np.uint8)).shape == (16, 8)


def test_download_model_keeps_only_a_verified_file(tmp_path):
    import hashlib

    import httpx
    import respx

    from src.watermark.lama import download_model

    url = "https://models.example.invalid/big-lama.pt"
    good = hashlib.sha256(b"model-bytes").hexdigest()
    with respx.mock:
        respx.get(url).mock(return_value=httpx.Response(200, content=b"model-bytes"))
        dest = download_model(tmp_path / "models" / "big-lama.pt", url=url, sha256=good)
        assert dest.read_bytes() == b"model-bytes"
        with pytest.raises(ValueError):
            download_model(tmp_path / "bad.pt", url=url, sha256="0" * 64)
    assert not (tmp_path / "bad.pt").exists()
    assert not list(tmp_path.glob(".bad.pt.*"))  # the partial download is removed


def test_falls_back_to_telea_without_model(monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "watermark_lama_enabled", True)
    monkeypatch.setattr(get_settings(), "watermark_lama_model_path", tmp_path / "missing.pt")
    assert get_inpainter("lama").name == "telea"


def test_disabled_flag_means_telea(monkeypatch):
    monkeypatch.setattr(get_settings(), "watermark_lama_enabled", False)
    assert get_inpainter("lama").name == "telea"


@pytest.mark.skipif(not HAS_LAMA, reason="torch or the LaMa model file is unavailable")
def test_lama_changes_only_masked_pixels():
    from src.watermark.lama import LamaInpainter

    img = make_background(640, 480, seed=4)
    mask = np.zeros((480, 640), np.uint8)
    mask[200:240, 220:420] = 1
    out = LamaInpainter(MODEL).inpaint(img, mask)
    assert out.shape == img.shape
    assert np.array_equal(out[mask == 0], img[mask == 0])
    assert not np.array_equal(out[mask == 1], img[mask == 1])


@pytest.mark.skipif(not HAS_LAMA, reason="torch or the LaMa model file is unavailable")
def test_pipeline_uses_lama_for_halo_center(monkeypatch):
    monkeypatch.setattr(get_settings(), "watermark_lama_enabled", True)
    tpl = {t.id: t for t in load_templates()}["halo_center"]
    img = jpeg_roundtrip(apply_mark(make_background(1000, 750, seed=2), tpl))
    _out, report = clean_array(img)
    assert [m.method for m in report.marks] == ["inpaint:lama"]


@pytest.mark.skipif(not HAS_LAMA, reason="torch or the LaMa model file is unavailable")
def test_pipeline_uses_lama_for_4zida(monkeypatch):
    monkeypatch.setattr(get_settings(), "watermark_lama_enabled", True)
    tpl = {t.id: t for t in load_templates()}["zida_logo"]
    img = jpeg_roundtrip(apply_mark(make_background(1440, 1080, seed=6), tpl))
    _out, report = clean_array(img)
    assert [m.method for m in report.marks] == ["inpaint:lama", "inpaint:lama"]
