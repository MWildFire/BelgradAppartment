"""R03 corner policy: keep detections visible and avoid reusing the wrong mode."""
from __future__ import annotations

import csv
import io
from pathlib import Path

import numpy as np
import pytest
from click.testing import CliRunner
from PIL import Image
from pydantic import ValidationError

from src.config import Settings, get_settings
from src.watermark import pipeline, store
from src.watermark.cli import wm_qa
from src.watermark.inpaint import TeleaInpainter
from src.watermark.remove import remove_inpaint
from src.watermark.templates import load_templates
from tests.watermark.synth import apply_mark, jpeg_roundtrip, make_background, mark_mask


@pytest.fixture
def policy(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "watermark_lama_enabled", False)
    return settings


def _templates():
    return {t.id: t for t in load_templates()}


def _corner():
    return jpeg_roundtrip(apply_mark(make_background(1000, 750, seed=3),
                                     _templates()["halo_corner"]))


def test_default_corner_mode_is_skip(monkeypatch):
    monkeypatch.delenv("WATERMARK_HALO_CORNER_MODE", raising=False)
    assert Settings(_env_file=None).watermark_halo_corner_mode == "skip"


def test_invalid_corner_mode_is_rejected(monkeypatch):
    monkeypatch.setenv("WATERMARK_HALO_CORNER_MODE", "typo")
    with pytest.raises(ValidationError, match="watermark_halo_corner_mode"):
        Settings(_env_file=None)


def test_skip_preserves_pixels_but_reports_detected_corner(policy, monkeypatch):
    monkeypatch.setattr(policy, "watermark_halo_corner_mode", "skip")
    img = _corner()
    out, report = pipeline.clean_array(img)
    assert np.array_equal(out, img)
    assert not report.changed
    assert [(m.template_id, m.method) for m in report.marks] == [("halo_corner", "skip")]
    assert report.to_dict()["marks"][0]["method"] == "skip"


def test_telea_matches_previous_algorithm_exactly(policy, monkeypatch):
    monkeypatch.setattr(policy, "watermark_halo_corner_mode", "telea")
    img = _corner()
    found = pipeline.find_marks(img)
    assert len(found) == 1 and found[0][0].id == "halo_corner"
    tpl, det = found[0]
    expected = remove_inpaint(img, tpl, det, TeleaInpainter(radius=5))
    out, report = pipeline.clean_array(img)
    assert np.array_equal(out, expected)
    assert not np.array_equal(out, img)
    assert report.changed and report.marks[0].method == "inpaint:telea"


def test_logo_is_cleaned_while_corner_is_kept(policy, monkeypatch):
    monkeypatch.setattr(policy, "watermark_halo_corner_mode", "skip")
    templates = _templates()
    img = make_background(1000, 750, seed=3)
    img = jpeg_roundtrip(apply_mark(apply_mark(img, templates["zida_logo"]),
                                   templates["halo_corner"]))
    out, report = pipeline.clean_array(img)
    assert report.changed
    assert [(m.template_id, m.method) for m in report.marks].count(("halo_corner", "skip")) == 1
    assert sum(m.template_id == "zida_logo" and m.method == "reverse_blend"
               for m in report.marks) == 2
    corner = mark_mask(templates["halo_corner"], 1000, 750, dilate=12)
    assert np.array_equal(out[corner], img[corner])
    assert not np.array_equal(out, img)


def test_skip_only_bytes_and_file_behave_like_unmarked(policy, monkeypatch, tmp_path):
    monkeypatch.setattr(policy, "watermark_halo_corner_mode", "skip")
    buf = io.BytesIO()
    Image.fromarray(_corner()).save(buf, "JPEG", quality=92)
    data = buf.getvalue()
    out, report = pipeline.clean_bytes(data)
    assert out is data and not report.changed
    src, dst = tmp_path / "src.jpg", tmp_path / "clean.jpg"
    src.write_bytes(data)
    report = pipeline.clean_file(src, dst)
    assert not report.changed and report.marks[0].method == "skip"
    assert not dst.exists() and src.read_bytes() == data


def test_store_skip_only_and_mode_changes(policy, monkeypatch, tmp_path):
    inbound = tmp_path / "inbound"
    inbound.mkdir()
    monkeypatch.setattr(policy, "inbound_photos_dir", inbound)
    monkeypatch.setattr(policy, "watermark_clean_dir", tmp_path / "clean")
    src = inbound / "corner.jpg"
    Image.fromarray(_corner()).save(src, "JPEG", quality=92)
    original = src.read_bytes()
    for mode in ("telea", "skip", "telea"):
        monkeypatch.setattr(policy, "watermark_halo_corner_mode", mode)
        assert not store.is_current(src)
        assert store.clean_or_original(src) == src
        result = store.ensure_clean(str(src))
        assert result is not None and not result.reused
        assert (result.clean is None) == (mode == "skip")
        target = store.clean_path_for(src)
        assert target.is_file() == (mode == "telea")
        assert store.read_sidecar(target)["changed"] == (mode == "telea")
        assert store.is_current(src)
        assert store.ensure_clean(str(src)).reused
        assert store.clean_or_original(src) == result.best
        assert src.read_bytes() == original


def test_qa_counts_skipped_detections(policy, monkeypatch, tmp_path: Path):
    monkeypatch.setattr(policy, "watermark_halo_corner_mode", "skip")
    samples = tmp_path / "samples"
    samples.mkdir()
    Image.fromarray(_corner()).save(samples / "corner.jpg", "JPEG", quality=92)
    expected = tmp_path / "expected.csv"
    expected.write_text("file,marks\ncorner.jpg,halo_corner#0\n")
    out = tmp_path / "qa"
    result = CliRunner().invoke(wm_qa, [str(samples), "--out-dir", str(out),
                                      "--expect", str(expected)])
    assert result.exit_code == 0, result.output
    assert "1 images, marks in 1" in result.output
    assert "QA expectations: all matched" in result.output
    assert "halo_corner#0" in result.output and "skip" in result.output
    with (out / "report.csv").open() as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 1 and rows[0]["method"] == "skip"


def test_discard_remains_explicit_across_corner_modes(policy, monkeypatch, tmp_path):
    inbound = tmp_path / "inbound"
    inbound.mkdir()
    monkeypatch.setattr(policy, "inbound_photos_dir", inbound)
    monkeypatch.setattr(policy, "watermark_clean_dir", tmp_path / "clean")
    monkeypatch.setattr(policy, "watermark_halo_corner_mode", "telea")
    src = inbound / "corner.jpg"
    Image.fromarray(_corner()).save(src, "JPEG", quality=92)
    assert store.ensure_clean(str(src)).clean is not None
    assert store.discard(src)
    monkeypatch.setattr(policy, "watermark_halo_corner_mode", "skip")
    assert store.is_current(src)
    result = store.ensure_clean(str(src))
    assert result.reused and result.clean is None
    monkeypatch.setattr(policy, "watermark_halo_corner_mode", "telea")
    assert store.ensure_clean(str(src)).reused
    assert store.ensure_clean(str(src), force=True).clean is not None


def test_switching_to_skip_restores_existing_site_variants(policy, monkeypatch, tmp_path):
    from src.watermark import published

    inbound = tmp_path / "inbound"
    inbound.mkdir()
    monkeypatch.setattr(policy, "inbound_photos_dir", inbound)
    monkeypatch.setattr(policy, "site_photos_root", inbound)
    monkeypatch.setattr(policy, "watermark_clean_dir", tmp_path / "clean")
    monkeypatch.setattr(policy, "watermark_site_use_clean", True)
    monkeypatch.setattr(policy, "watermark_halo_corner_mode", "telea")
    src = inbound / "corner.jpg"
    Image.fromarray(_corner()).save(src, "JPEG", quality=92)
    original = src.read_bytes()
    store.ensure_clean(str(src))
    assert published.rebuild_site_variants([str(src)]) == 1
    detail = next(p for p in published.site_variant_files(str(src)) if ".detail." in p.name)
    assert not pipeline.find_marks(pipeline.decode_image(detail.read_bytes()))
    monkeypatch.setattr(policy, "watermark_halo_corner_mode", "skip")
    assert store.ensure_clean(str(src)).clean is None
    assert published.needs_site_variants(str(src))
    assert published.rebuild_site_variants([str(src)]) == 1
    assert not published.needs_site_variants(str(src))
    assert [tpl.id for tpl, _ in pipeline.find_marks(
        pipeline.decode_image(detail.read_bytes()))] == ["halo_corner"]
    assert src.read_bytes() == original
