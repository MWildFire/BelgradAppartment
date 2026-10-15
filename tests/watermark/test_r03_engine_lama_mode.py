"""Optional corner LaMa routing and safe fallback; default remains skip."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import numpy as np
import pytest

from src.config import Settings, get_settings
from src.watermark import pipeline
from src.watermark.templates import load_templates
from tests.watermark.synth import apply_mark, jpeg_roundtrip, make_background


def _templates():
    return {t.id: t for t in load_templates()}


def _factory(monkeypatch, value):
    module = ModuleType("src.watermark.lama_corner")
    module.get_lama_corner_inpainter = lambda: value
    monkeypatch.setitem(sys.modules, module.__name__, module)


def test_lama_is_an_explicit_mode_not_the_default(monkeypatch):
    monkeypatch.delenv("WATERMARK_HALO_CORNER_MODE", raising=False)
    assert Settings(_env_file=None).watermark_halo_corner_mode == "skip"
    monkeypatch.setenv("WATERMARK_HALO_CORNER_MODE", "lama")
    assert Settings(_env_file=None).watermark_halo_corner_mode == "lama"


def test_lama_corner_selects_the_shared_model_adapter(monkeypatch):
    monkeypatch.setattr(get_settings(), "watermark_halo_corner_mode", "lama")
    adapter = object()
    _factory(monkeypatch, adapter)
    assert pipeline.removal_for(_templates()["halo_corner"]) == ("inpaint:lama", adapter)


def test_unavailable_lama_keeps_detected_corner_and_original_pixels(monkeypatch, caplog):
    monkeypatch.setattr(get_settings(), "watermark_halo_corner_mode", "lama")
    _factory(monkeypatch, None)
    monkeypatch.setattr(pipeline.logger, "disabled", False)
    img = jpeg_roundtrip(apply_mark(make_background(1000, 750, seed=3),
                                   _templates()["halo_corner"]))
    out, report = pipeline.clean_array(img)
    assert [(m.template_id, m.method) for m in report.marks] == [("halo_corner", "skip")]
    assert not report.changed
    assert np.array_equal(out, img)
    assert "corner LaMa unavailable; keeping corner (skip)" in caplog.text


def test_other_templates_keep_their_strategy_in_lama_corner_mode(monkeypatch):
    monkeypatch.setattr(get_settings(), "watermark_halo_corner_mode", "lama")
    templates = _templates()
    before = {key: pipeline.removal_for(templates[key])[0]
              for key in ("zida_logo", "halo_center", "cityexpert_center")}
    monkeypatch.setattr(get_settings(), "watermark_halo_corner_mode", "telea")
    assert before == {key: pipeline.removal_for(templates[key])[0] for key in before}


HAS_LAMA = (importlib.util.find_spec("torch") is not None
            and Path(get_settings().watermark_lama_model_path).is_file())


@pytest.mark.skipif(not HAS_LAMA, reason="torch or the LaMa model file is unavailable")
def test_real_lama_corner_pipeline_reports_the_requested_method(monkeypatch):
    monkeypatch.setattr(get_settings(), "watermark_halo_corner_mode", "lama")
    monkeypatch.setattr(get_settings(), "watermark_lama_enabled", True)
    img = jpeg_roundtrip(apply_mark(make_background(1000, 750, seed=3),
                                   _templates()["halo_corner"]))
    out, report = pipeline.clean_array(img)
    assert [(m.template_id, m.method) for m in report.marks] == [("halo_corner", "inpaint:lama")]
    assert report.changed and not np.array_equal(out, img)
