"""Detection and removal on synthetic data: every platform mark, negatives, bytes/files."""

from __future__ import annotations

import io
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

import src.watermark.pipeline as pipeline
from src.config import get_settings
from src.watermark.detect import match_instance, to_gray
from src.watermark.pipeline import (
    NotAnImageError,
    clean_array,
    clean_bytes,
    clean_file,
    decode_image,
    find_marks,
    removal_for,
)
from src.watermark.templates import load_templates
from tests.watermark.synth import apply_mark, jpeg_roundtrip, make_background, mark_mask

CASES = [
    ("cityexpert_center", (1920, 1080)),
    ("cityexpert_center", (900, 1600)),
    ("zida_logo", (1440, 1080)),
    ("zida_logo", (810, 1080)),
    ("halo_center", (1000, 750)),
    ("halo_center", (562, 750)),
    ("halo_corner", (1000, 750)),
    ("halo_corner", (750, 750)),
]


@pytest.fixture(autouse=True)
def _telea_only(monkeypatch):
    """CI has no torch: test the Telea path (LaMa has its own skippable tests)."""
    monkeypatch.setattr(get_settings(), "watermark_lama_enabled", False)


def _tpl(tid):
    return {t.id: t for t in load_templates()}[tid]


def _marked(tid, size, seed=3):
    bg = make_background(*size, seed=seed)
    return bg, jpeg_roundtrip(apply_mark(bg, _tpl(tid)))


@pytest.mark.parametrize(("tid", "size"), CASES)
def test_detects_exactly_the_applied_mark(tid, size):
    _bg, img = _marked(tid, size)
    found = find_marks(img)
    assert {(t.id, d.instance) for t, d in found} == {
        (tid, i) for i in range(len(_tpl(tid).instances))
    }
    for _t, det in found:
        assert abs(det.dx) <= 1.5 and abs(det.dy) <= 1.5


@pytest.mark.parametrize("seed", range(6))
@pytest.mark.parametrize("size", [(1920, 1080), (1000, 750), (810, 1080), (562, 750)])
def test_clean_background_has_no_marks(seed, size):
    img = jpeg_roundtrip(make_background(*size, seed=100 + seed))
    assert find_marks(img) == []


@pytest.mark.parametrize(("tid", "size"), CASES)
def test_cleaning_restores_background_under_the_mark(tid, size):
    bg, img = _marked(tid, size)
    out, report = clean_array(img)
    assert report.changed
    assert find_marks(out) == []
    mask = mark_mask(_tpl(tid), *size)
    # Error floor: even a perfect restoration carries the source JPEG noise.
    floor = np.abs(jpeg_roundtrip(bg).astype(np.float32) - bg)[mask].mean()
    before = np.abs(img.astype(np.float32) - bg)[mask].mean()
    after = np.abs(out.astype(np.float32) - bg)[mask].mean()
    if {m.method for m in report.marks} == {"reverse_blend"}:
        assert after <= floor + 2.0, (floor, before, after)
    else:  # inpainting cannot know the true pixels: require a clear improvement
        assert after <= min(before * 0.7, floor + 4.5), (floor, before, after)
    assert all(m.residual < _tpl(m.template_id).min_score for m in report.marks)


@pytest.mark.parametrize("seed", [11, 12])
def test_parts_with_weaker_opacity_are_restored_too(seed):
    """CityExpert draws its icon bars fainter than the letters; k is measured per part.

    With one k for the whole mark the bars come out ~3 levels too dark and the letters
    ~3-5 levels too bright (visible as faint rectangles/letters); per part both stay near 0.
    """
    tpl, size = _tpl("cityexpert_center"), (1920, 1080)
    bg = make_background(*size, seed=seed)
    place = tpl.predicted(*size)[0]
    bars_x = round(place.x + 560)  # the icon bars end at x~456 of the template
    faint = apply_mark(bg, tpl, opacity_scale=0.75)
    img = apply_mark(bg, tpl)
    img[:, :bars_x] = faint[:, :bars_x]
    out, report = clean_array(jpeg_roundtrip(img))
    assert [m.method for m in report.marks] == ["reverse_blend"]
    core = mark_mask(tpl, *size, dilate=0) & (apply_mark(np.zeros_like(bg), tpl).mean(axis=2) > 40)
    bias = out.astype(np.float32).mean(axis=2) - bg.astype(np.float32).mean(axis=2)
    bars, letters = core.copy(), core.copy()
    bars[:, bars_x:] = False
    letters[:, :bars_x] = False
    assert abs(float(bias[bars].mean())) < 1.5
    assert abs(float(bias[letters].mean())) < 1.5


def test_cityexpert_detection_without_a_mark_changes_nothing(monkeypatch):
    """Without a real mark the fitted opacity k stops at its 0.60 lower bound: no change."""
    tpl = _tpl("cityexpert_center")
    bg = make_background(1920, 1080, seed=3)

    def fake_find_marks(img, templates=None):
        h, w = img.shape[:2]
        return [(tpl, match_instance(to_gray(img), tpl, tpl.predicted(w, h)[0]))]

    monkeypatch.setattr(pipeline, "find_marks", fake_find_marks)
    out, report = clean_array(bg)
    assert report.changed is False
    assert np.array_equal(out, bg)


def test_cityexpert_real_mark_is_still_removed():
    _bg, img = _marked("cityexpert_center", (1920, 1080))
    _out, report = clean_array(img)
    assert report.changed is True


def test_lama_fallback_when_lama_is_missing():
    assert removal_for(_tpl("zida_logo"))[0] == "reverse_blend"
    assert removal_for(_tpl("halo_center"))[0] == "inpaint:telea"
    assert removal_for(_tpl("halo_corner"))[0] == "inpaint:telea"
    assert removal_for(_tpl("cityexpert_center"))[0] == "reverse_blend"


@pytest.mark.parametrize(("tid", "size"), CASES)
def test_pixels_far_from_the_mark_are_untouched(tid, size):
    _bg, img = _marked(tid, size)
    out, _report = clean_array(img)
    far = ~mark_mask(_tpl(tid), *size, dilate=12)
    assert np.array_equal(out[far], img[far])


def test_clean_bytes_returns_same_bytes_when_nothing_found():
    buf = io.BytesIO()
    Image.fromarray(make_background(800, 600, seed=7)).save(buf, "JPEG", quality=90)
    data = buf.getvalue()
    out, report = clean_bytes(data)
    assert out is data and not report.changed


def test_clean_bytes_returns_jpeg_without_mark():
    _bg, img = _marked("zida_logo", (1440, 1080))
    buf = io.BytesIO()
    Image.fromarray(img).save(buf, "JPEG", quality=92)
    out, report = clean_bytes(buf.getvalue())
    assert report.changed and len(report.marks) == 2
    cleaned = decode_image(out)
    assert cleaned.shape == img.shape
    assert find_marks(cleaned) == []


def test_decode_rejects_garbage():
    with pytest.raises(NotAnImageError):
        decode_image(b"definitely not an image")


def test_decode_applies_exif_rotation():
    img = Image.fromarray(make_background(400, 200, seed=1))
    exif = img.getexif()
    exif[0x0112] = 6  # rotate by 90 degrees
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif.tobytes())
    assert decode_image(buf.getvalue()).shape[:2] == (400, 200)


def test_clean_file_writes_only_when_marks_found(tmp_path: Path):
    plain = tmp_path / "plain.jpg"
    Image.fromarray(make_background(800, 600, seed=9)).save(plain, "JPEG", quality=90)
    marked = tmp_path / "marked.jpg"
    Image.fromarray(_marked("halo_corner", (1000, 750))[1]).save(marked, "JPEG", quality=92)
    original_bytes = marked.read_bytes()
    assert not clean_file(plain, tmp_path / "plain.clean.jpg").changed
    assert not (tmp_path / "plain.clean.jpg").exists()
    assert clean_file(marked, tmp_path / "out" / "marked.clean.jpg").changed
    assert (tmp_path / "out" / "marked.clean.jpg").is_file()
    assert marked.read_bytes() == original_bytes


def test_clean_file_never_overwrites_its_source(tmp_path: Path):
    marked = tmp_path / "marked.jpg"
    Image.fromarray(_marked("halo_corner", (1000, 750))[1]).save(marked, "JPEG", quality=92)
    original_bytes = marked.read_bytes()
    with pytest.raises(ValueError):
        clean_file(marked, marked)
    assert marked.read_bytes() == original_bytes
