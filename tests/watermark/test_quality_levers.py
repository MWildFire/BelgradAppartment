"""Quality levers (WM05 Q1): refined CityExpert removal, kind switch, self-check, mask growth.

Every "kept" outcome must leave the photo byte-identical and say why (`kept_reason`).
"""

from __future__ import annotations

import numpy as np
import pytest
from scipy import ndimage as ndi

import src.watermark.pipeline as pipeline
from src.watermark.detect import scaled_coverage
from src.watermark.inpaint import TeleaInpainter
from src.watermark.pipeline import clean_array, find_marks, removal_for
from src.watermark.settings import get_watermark_settings
from src.watermark.templates import load_templates
from tests.watermark.synth import apply_mark, jpeg_roundtrip, make_background


@pytest.fixture(autouse=True)
def _no_lama(monkeypatch):
    """CI has no torch; LaMa-dependent paths use a classic inpainter stub where needed."""
    monkeypatch.setattr(get_watermark_settings(), "watermark_lama_enabled", False)
    monkeypatch.setattr(get_watermark_settings(), "watermark_halo_corner_mode", "skip")


def _tpl(tid):
    return {t.id: t for t in load_templates()}[tid]


def _marked(tid, size, seed=3):
    bg = make_background(*size, seed=seed)
    return bg, jpeg_roundtrip(apply_mark(bg, _tpl(tid)))


# --- kind switch -------------------------------------------------------------------------------


def test_disabled_kind_is_found_but_kept(monkeypatch):
    monkeypatch.setattr(get_watermark_settings(), "watermark_disabled_kinds", "cityexpert_center")
    _bg, img = _marked("cityexpert_center", (1920, 1080))
    out, report = clean_array(img)
    assert [(m.template_id, m.method, m.kept_reason) for m in report.marks] == [
        ("cityexpert_center", "skip", "disabled")
    ]
    assert report.changed is False
    assert np.array_equal(out, img)


def test_disabled_kinds_ignore_case_and_spaces(monkeypatch):
    monkeypatch.setattr(
        get_watermark_settings(), "watermark_disabled_kinds", " Halo_Center , zida_logo ,, "
    )
    assert get_watermark_settings().disabled_kinds() == frozenset({"halo_center", "zida_logo"})
    monkeypatch.setattr(get_watermark_settings(), "watermark_disabled_kinds", "")
    assert get_watermark_settings().disabled_kinds() == frozenset()


def test_disabling_one_kind_leaves_the_others_working(monkeypatch):
    monkeypatch.setattr(get_watermark_settings(), "watermark_disabled_kinds", "zida_logo")
    _bg, img = _marked("cityexpert_center", (1920, 1080))
    _out, report = clean_array(img)
    assert [(m.method, m.kept_reason) for m in report.marks] == [("reverse_blend", None)]


# --- 4zida without LaMa ------------------------------------------------------------------------


def test_zida_without_lama_is_kept_not_reverse_blended():
    assert removal_for(_tpl("zida_logo"))[0] == "skip"
    _bg, img = _marked("zida_logo", (1440, 1080))
    out, report = clean_array(img)
    assert {(m.method, m.kept_reason) for m in report.marks} == {("skip", "lama_unavailable")}
    assert np.array_equal(out, img)


# --- self-check --------------------------------------------------------------------------------


def test_halo_center_limit_lives_in_the_template():
    # measured on the 6 sample photos: bad 7.0-11.4 % of the frame, acceptable 3.3-4.2 %
    assert _tpl("halo_center").lama_max_mask_fraction == pytest.approx(0.055)
    assert _tpl("zida_logo").lama_max_mask_fraction is None
    assert _tpl("cityexpert_center").lama_max_mask_fraction is None


def test_large_lama_mark_is_kept_by_self_check(monkeypatch):
    """A 1000x750 photo carries the full Halo logo (7 % of the frame): LaMa would invent scenery."""
    monkeypatch.setattr(pipeline, "get_inpainter", lambda preferred: TeleaInpainter())
    _bg, img = _marked("halo_center", (1000, 750))
    out, report = clean_array(img)
    assert [(m.template_id, m.method, m.kept_reason) for m in report.marks] == [
        ("halo_center", "skip", "self_check")
    ]
    assert np.array_equal(out, img)


def test_small_lama_mark_is_still_removed(monkeypatch):
    """Narrow photo: the logo is ~4 % of the frame, within the limit."""
    monkeypatch.setattr(pipeline, "get_inpainter", lambda preferred: TeleaInpainter())
    _bg, img = _marked("halo_center", (562, 750))
    _out, report = clean_array(img)
    assert [(m.method.startswith("inpaint"), m.kept_reason) for m in report.marks] == [(True, None)]


def test_self_check_reverts_a_removal_that_left_the_mark(monkeypatch):
    """If the template still correlates at its place after removal, the photo stays as it was."""
    _bg, img = _marked("cityexpert_center", (1920, 1080))
    monkeypatch.setattr(pipeline, "remove_refined", lambda im, tpl, det: (im.copy(), 1.0))
    monkeypatch.setattr(pipeline, "remove_reverse_blend", lambda im, tpl, det: (im.copy(), 1.0))
    out, report = clean_array(img)
    assert [(m.method, m.kept_reason) for m in report.marks] == [("skip", "self_check")]
    assert report.changed is False
    assert np.array_equal(out, img)


# --- mask growth floor for small photos --------------------------------------------------------


def test_zida_extra_mask_growth_applies_on_very_small_photos_only():
    """Measured on the 9 sample photos: it cleans 562 px and 675 px wide photos, does nothing
    for 810 px and damages the 1000 px one (a bright light strip), so the limit is scale 0.5."""
    tpl = _tpl("zida_logo")
    assert tpl.mask_grow_px(0.466) == 2  # the R04 rule stays the base
    assert tpl.mask_extra_grow_px(0.466) == 4  # 675 px wide: up to 4 px, on neutral pixels
    assert tpl.mask_extra_grow_px(0.388) == 4  # 562 px wide
    assert tpl.mask_extra_grow_px(0.5625) == 0  # 810 px wide
    assert tpl.mask_extra_grow_px(0.694) == 0  # 1000 px wide
    assert tpl.mask_extra_grow_px(1.0) == 0  # 1440 px wide: unchanged


def test_other_kinds_have_no_extra_growth():
    halo = _tpl("halo_center")
    assert halo.mask_grow_px(1.0) == 3
    assert halo.mask_grow_px(0.56) == 2  # round(3 * max(scale, 0.5))
    assert halo.mask_extra_grow_px(0.56) == 0


def test_extra_growth_spares_saturated_scene_pixels():
    """Text/objects next to the logo (a yellow phone number) must not be filled by the extra ring."""
    from src.watermark.remove import remove_inpaint

    tpl, size = _tpl("zida_logo"), (675, 900)
    bg = np.full((size[1], size[0], 3), 200, np.uint8)
    img = apply_mark(bg, tpl)
    det = find_marks(img)[0][1]
    # paint a saturated yellow bar and a neutral grey bar 3 px outside the 2 px base mask
    from src.watermark.remove import alpha_map, support_mask

    base = support_mask(alpha_map(tpl, det, img.shape[:2]), threshold=0.02, dilate_px=2) > 0
    ring = support_mask(alpha_map(tpl, det, img.shape[:2]), threshold=0.02, dilate_px=4) > 0
    extra = ring & ~base
    half = int(np.median(np.nonzero(extra)[1]))
    yellow = extra & (np.arange(size[0])[None, :] < half)
    grey = extra & ~yellow
    img = img.copy()
    img[yellow] = (255, 230, 0)
    img[grey] = (120, 120, 120)

    class Recorder:
        name = "recorder"

        def inpaint(self, image, mask):
            self.mask = mask > 0
            return image

    rec = Recorder()
    remove_inpaint(img, tpl, det, rec, grow_px=2, extra_grow_px=4)
    assert not rec.mask[yellow].any()  # saturated pixels stay
    assert rec.mask[grey].mean() > 0.95  # neutral pixels are covered
    assert rec.mask[base].all()  # the base mask is always covered


def test_pipeline_passes_the_template_growth_to_the_inpainter(monkeypatch):
    seen = []

    def fake_inpaint(img, tpl, det, inpainter, **kw):
        seen.append((kw.get("grow_px"), kw.get("extra_grow_px")))
        return img

    monkeypatch.setattr(pipeline, "get_inpainter", lambda preferred: TeleaInpainter())
    monkeypatch.setattr(pipeline, "remove_inpaint", fake_inpaint)
    _bg, img = _marked("zida_logo", (675, 900))
    clean_array(img)
    assert seen and set(seen) == {(2, 4)}
    seen.clear()
    _bg, img = _marked("zida_logo", (810, 1080))
    clean_array(img)
    assert seen and set(seen) == {(2, 0)}


# --- CityExpert: no band, lines of the scene survive --------------------------------------------


def test_scene_lines_crossing_the_cityexpert_logo_survive():
    """A 2 px bright line (a moulding) through the logo must not be interrupted at the letters."""
    tpl, size = _tpl("cityexpert_center"), (1920, 1080)
    bg = make_background(*size, seed=5).astype(np.float32)
    place = tpl.predicted(*size)[0]
    cov = scaled_coverage(tpl, place.scale)
    x0, y0 = round(place.x), round(place.y)
    row = y0 + int(cov.shape[0] * 0.62)  # crosses the x-height of the letters
    bg[row : row + 2, x0 : x0 + cov.shape[1]] += 35.0
    bg = np.clip(bg, 0, 255).astype(np.uint8)
    img = jpeg_roundtrip(apply_mark(bg, tpl))
    out, report = clean_array(img)
    assert [m.method for m in report.marks] == ["reverse_blend"]
    line = out[row : row + 2, x0 : x0 + cov.shape[1]].astype(np.float32).mean(axis=(0, 2))
    around = np.concatenate(
        [out[row - 6 : row - 2], out[row + 4 : row + 8]]
    )[:, x0 : x0 + cov.shape[1]].astype(np.float32).mean(axis=(0, 2))
    contrast = ndi.uniform_filter1d(line - around, 3)
    broken = float((contrast < 20.0).mean())  # the real line has +35
    assert broken < 0.03, broken


def test_cityexpert_still_rejects_a_false_match(monkeypatch):
    """Nothing is drawn: the fitted opacity collapses and the photo stays untouched."""
    tpl = _tpl("cityexpert_center")
    bg = make_background(1920, 1080, seed=3)

    def fake_find_marks(img, templates=None):
        h, w = img.shape[:2]
        return [(tpl, pipeline.match_instance(pipeline.to_gray(img), tpl, tpl.predicted(w, h)[0]))]

    monkeypatch.setattr(pipeline, "find_marks", fake_find_marks)
    out, report = clean_array(bg)
    assert report.changed is False
    assert np.array_equal(out, bg)
    assert find_marks(jpeg_roundtrip(bg)) == []
