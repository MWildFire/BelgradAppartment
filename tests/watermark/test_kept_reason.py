"""Kept marks (WM05 contract): found but untouched pixels -> method "skip" + kept_reason.

Values written by W1: corner_mode (Halo corner in skip mode) and lama_unavailable (the mark
needs LaMa and LaMa is not there: no silent Telea any more). W2 adds disabled/self_check.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json

import numpy as np
import pytest

from src.watermark import inpaint, store
from src.watermark.pipeline import (
    CleanReport,
    MarkResult,
    clean_array,
    plan_removal,
    removal_for,
)
from src.watermark.settings import get_watermark_settings
from src.watermark.templates import load_templates
from tests.watermark.synth import apply_mark, jpeg_roundtrip, make_background


@pytest.fixture(autouse=True)
def _no_lama(monkeypatch):
    """LaMa is not available: flag off (also what a server without the model looks like)."""
    wm = get_watermark_settings()
    monkeypatch.setattr(wm, "watermark_lama_enabled", False)
    monkeypatch.setattr(wm, "watermark_halo_corner_mode", "skip")


def _tpl(tid):
    return {t.id: t for t in load_templates()}[tid]


def _marked(tid, size, seed=3):
    return jpeg_roundtrip(apply_mark(make_background(*size, seed=seed), _tpl(tid)))


def test_kept_reason_is_the_last_optional_field_of_markresult():
    fields = dataclasses.fields(MarkResult)
    assert fields[-1].name == "kept_reason"
    assert fields[-1].default is None


def test_to_dict_carries_kept_reason_and_old_dicts_still_load():
    mark = MarkResult("halo_corner", "halooglasi", 0, 0.9, 1.0, 2.0, 1.0, "skip", 0.9, "corner_mode")
    assert CleanReport(10, 10, [mark]).to_dict()["marks"][0]["kept_reason"] == "corner_mode"
    old = {"template_id": "halo_corner", "platform": "halooglasi", "instance": 0, "score": 0.9,
           "x": 1.0, "y": 2.0, "scale": 1.0, "method": "skip", "residual": 0.9}
    assert MarkResult(**old).kept_reason is None


def test_corner_in_skip_mode_is_kept_for_corner_mode():
    img = _marked("halo_corner", (1000, 750))
    out, report = clean_array(img)
    assert np.array_equal(out, img) and not report.changed
    assert [(m.template_id, m.method, m.kept_reason) for m in report.marks] == [
        ("halo_corner", "skip", "corner_mode")
    ]


def test_center_mark_without_lama_is_kept_not_telea():
    img = _marked("halo_center", (1000, 750))
    out, report = clean_array(img)
    assert np.array_equal(out, img) and not report.changed
    assert [(m.template_id, m.method, m.kept_reason) for m in report.marks] == [
        ("halo_center", "skip", "lama_unavailable")
    ]
    assert report.to_dict()["marks"][0]["kept_reason"] == "lama_unavailable"


def test_corner_in_lama_mode_without_lama_is_kept_for_lama_unavailable(monkeypatch):
    monkeypatch.setattr(get_watermark_settings(), "watermark_halo_corner_mode", "lama")
    img = _marked("halo_corner", (1000, 750))
    out, report = clean_array(img)
    assert np.array_equal(out, img)
    assert [(m.method, m.kept_reason) for m in report.marks] == [("skip", "lama_unavailable")]


def test_explicit_telea_corner_mode_still_uses_the_classic_fill(monkeypatch):
    monkeypatch.setattr(get_watermark_settings(), "watermark_halo_corner_mode", "telea")
    assert removal_for(_tpl("halo_corner"))[0] == "inpaint:telea"
    img = _marked("halo_corner", (1000, 750))
    out, report = clean_array(img)
    assert report.changed and not np.array_equal(out, img)
    assert [(m.method, m.kept_reason) for m in report.marks] == [("inpaint:telea", None)]


def test_removed_marks_have_no_kept_reason():
    _out, report = clean_array(_marked("cityexpert_center", (1920, 1080)))
    assert report.changed
    assert {m.kept_reason for m in report.marks} == {None}


def test_zida_needs_lama_and_is_kept_without_it():
    """zida_logo no longer declares a reverse-blend fallback (WM05 decision 5)."""
    assert removal_for(_tpl("zida_logo"))[0] == "skip"
    assert plan_removal(_tpl("zida_logo")).kept_reason == "lama_unavailable"


def test_get_inpainter_says_none_when_lama_is_missing_and_never_telea():
    assert inpaint.get_inpainter("lama") is None
    assert inpaint.get_inpainter("telea").name == "telea"


def test_kept_result_is_saved_in_the_sidecar_and_old_sidecars_still_read(tmp_path, monkeypatch):
    from src.config import get_settings

    inbound = tmp_path / "inbound"
    inbound.mkdir()
    original = inbound / "center.jpg"
    from PIL import Image

    Image.fromarray(_marked("halo_center", (1000, 750))).save(original, "JPEG", quality=92)
    monkeypatch.setattr(get_settings(), "inbound_photos_dir", inbound)
    monkeypatch.setattr(get_watermark_settings(), "watermark_clean_dir", tmp_path / "clean")
    result = store.ensure_clean(str(original))
    assert result is not None and result.clean is None
    mark = result.report["marks"][0]
    assert (mark["method"], mark["kept_reason"]) == ("skip", "lama_unavailable")
    sidecar = store.read_sidecar(store.clean_path_for(original))
    assert sidecar["report"]["marks"][0]["kept_reason"] == "lama_unavailable"

    # An R04 sidecar (no kept_reason in marks) is still a valid cache entry and parses.
    clean = store.clean_path_for(original)
    legacy = json.loads(json.dumps(sidecar))
    for m in legacy["report"]["marks"]:
        m.pop("kept_reason")
    store._write_sidecar(clean, legacy)
    cached = store.ensure_clean(str(original))
    assert cached is not None and cached.reused
    assert MarkResult(**cached.report["marks"][0]).kept_reason is None
    assert hashlib.sha256(original.read_bytes()).hexdigest() == legacy["sha256"]


@pytest.fixture
def saved_kept(tmp_path, monkeypatch):
    """A stored result whose only mark was kept; the LaMa model file does not exist yet."""
    from src.config import get_settings

    inbound = tmp_path / "inbound"
    inbound.mkdir()
    original = inbound / "photo.jpg"
    original.write_bytes(b"frozen original fixture")
    wm = get_watermark_settings()
    monkeypatch.setattr(get_settings(), "inbound_photos_dir", inbound)
    monkeypatch.setattr(wm, "watermark_clean_dir", tmp_path / "clean")
    monkeypatch.setattr(wm, "watermark_lama_enabled", True)
    model = tmp_path / "model.pt"
    monkeypatch.setattr(wm, "watermark_lama_model_path", model)
    monkeypatch.setattr("importlib.util.find_spec", lambda name: object())

    def save(template_id: str, reason: str | None):
        mark = {"template_id": template_id, "method": "skip", "kept_reason": reason}
        store._write_sidecar(store.clean_path_for(original), {
            "version": store.STORE_VERSION, "halo_corner_mode": "skip",
            "sha256": hashlib.sha256(original.read_bytes()).hexdigest(),
            "changed": False, "report": {"marks": [mark]},
        })

    return original, model, save


def test_center_mark_kept_for_missing_lama_is_retried_when_the_model_appears(saved_kept):
    original, model, save = saved_kept
    save("halo_center", "lama_unavailable")
    assert store.is_current(original)
    model.write_bytes(b"model appeared")
    assert not store.is_current(original)


@pytest.mark.parametrize(("template_id", "reason"), [
    ("halo_corner", "corner_mode"), ("halo_center", "disabled"), ("zida_logo", "self_check"),
])
def test_intentional_keeps_are_not_retried(saved_kept, template_id, reason):
    original, model, save = saved_kept
    save(template_id, reason)
    model.write_bytes(b"model appeared")
    assert store.is_current(original)
