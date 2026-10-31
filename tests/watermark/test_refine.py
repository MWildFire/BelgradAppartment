"""Per-part alpha refinement (src/watermark/refine.py) on synthetic "room" photos.

The mark is applied here with its own scipy code (sub-pixel shift, edge blur, opacity multiplier)
and independently of the module under test, so the tests check that refinement recovers the
background instead of repeating the module's arithmetic.
"""

from __future__ import annotations

import numpy as np
import pytest
from scipy import ndimage as ndi

from src.watermark.detect import scaled_coverage
from src.watermark.refine import refine_alpha
from src.watermark.templates import WatermarkTemplate, load_templates

WIDTH, HEIGHT = 960, 560
X0, Y0 = 40, 90  # integer template origin used as the "detected" position


def _template(tid: str = "cityexpert_center") -> WatermarkTemplate:
    return next(t for t in load_templates() if t.id == tid)


def _background(seed: int = 1) -> np.ndarray:
    """Smooth gradient + sharp-edged furniture + sensor noise; float32 HxWx3 in 90..200."""
    rng = np.random.default_rng(seed)
    ys = np.linspace(0.0, 1.0, HEIGHT, dtype=np.float32)[:, None, None]
    top = rng.uniform(150, 200, 3).astype(np.float32)
    bottom = rng.uniform(90, 140, 3).astype(np.float32)
    img = (top * (1 - ys) + bottom * ys) * np.ones((1, WIDTH, 1), np.float32)
    low = ndi.zoom(rng.normal(0, 14, (HEIGHT // 40, WIDTH // 40, 3)), (40, 40, 1), order=3)
    img += low[:HEIGHT, :WIDTH].astype(np.float32)
    for _ in range(10):  # thin bright/dark bars crossing the logo area (like mouldings)
        y = int(rng.integers(Y0, Y0 + 160))
        x = int(rng.integers(0, WIDTH - 200))
        img[y : y + 2, x : x + int(rng.integers(100, 400))] += float(rng.choice([-25, 25]))
    img += rng.normal(0, 1.2, img.shape).astype(np.float32)
    return np.clip(img, 0, 255)


def _apply(
    bg: np.ndarray,
    tpl: WatermarkTemplate,
    *,
    scale: float,
    shift: tuple[float, float] = (0.0, 0.0),
    blur: float = 0.0,
    k: float = 1.0,
) -> np.ndarray:
    """Observed photo: white mark with sub-pixel (dx, dy) shift, edge blur and opacity * k."""
    cov = scaled_coverage(tpl, scale).astype(np.float32)
    canvas = np.zeros(bg.shape[:2], np.float32)
    h, w = cov.shape
    canvas[Y0 : Y0 + h, X0 : X0 + w] = cov
    if blur > 0:
        canvas = ndi.gaussian_filter(canvas, blur)
    canvas = ndi.shift(canvas, (shift[1], shift[0]), order=1)
    alpha = np.clip(canvas * tpl.opacity[0] * k, 0, 0.95)[..., None]
    return np.round(alpha * 255.0 + (1.0 - alpha) * bg).clip(0, 255).astype(np.uint8)


def _restore(img: np.ndarray, tpl: WatermarkTemplate, cov_k: np.ndarray) -> np.ndarray:
    alpha = np.clip(cov_k * tpl.opacity[0], 0, 0.95)[..., None]
    return (img.astype(np.float32) - alpha * 255.0) / (1.0 - alpha)


def _nominal(tpl: WatermarkTemplate, scale: float) -> np.ndarray:
    cov = scaled_coverage(tpl, scale).astype(np.float32)
    out = np.zeros((HEIGHT, WIDTH), np.float32)
    h, w = cov.shape
    out[Y0 : Y0 + h, X0 : X0 + w] = cov
    return out


def _psnr_on_mark(restored: np.ndarray, truth: np.ndarray, tpl: WatermarkTemplate, scale: float):
    mask = ndi.binary_dilation(_nominal(tpl, scale) > 0.01, iterations=2)
    err = (restored[mask] - truth[mask]) ** 2
    return float(10.0 * np.log10(255.0**2 / max(float(err.mean()), 1e-9)))


@pytest.fixture(scope="module")
def case():
    tpl = _template()
    scale = WIDTH / float(tpl.ref_width)
    truth = _background()
    return tpl, scale, truth


def test_refinement_recovers_background_under_shifted_blurred_mark(case) -> None:
    tpl, scale, truth = case
    img = _apply(truth, tpl, scale=scale, shift=(0.7, -0.6), blur=0.9, k=0.9)
    cov = scaled_coverage(tpl, scale)
    plain = _psnr_on_mark(_restore(img, tpl, _nominal(tpl, scale)), truth, tpl, scale)
    refined = refine_alpha(img, cov, x=float(X0), y=float(Y0), opacity=tpl.opacity[0])
    fitted = _psnr_on_mark(_restore(img, tpl, refined.cov_k), truth, tpl, scale)
    assert fitted >= plain + 3.0, (plain, fitted)
    assert fitted >= 35.0, fitted


def test_refinement_absorbs_a_scale_error_of_the_detected_mark(case) -> None:
    """Detection picks one scale for the whole logo (0.5 % steps); parts differ in size."""
    tpl, scale, truth = case
    mismatch = 1.006  # the real logo is 0.6 % larger than the detected scale says
    bigger = scaled_coverage(tpl, scale * mismatch).astype(np.float32)
    h0, w0 = scaled_coverage(tpl, scale).shape
    canvas = np.zeros((HEIGHT, WIDTH), np.float32)
    top = Y0 - (bigger.shape[0] - h0) / 2.0  # grows around the logo centre
    left = X0 - (bigger.shape[1] - w0) / 2.0
    tile = ndi.shift(bigger, (top - np.floor(top), left - np.floor(left)), order=1)
    y, x = int(np.floor(top)), int(np.floor(left))
    canvas[y : y + tile.shape[0], x : x + tile.shape[1]] = tile
    alpha = np.clip(canvas * tpl.opacity[0], 0, 0.95)[..., None]
    img = np.round(alpha * 255.0 + (1.0 - alpha) * truth).clip(0, 255).astype(np.uint8)
    cov = scaled_coverage(tpl, scale)
    plain = _psnr_on_mark(_restore(img, tpl, _nominal(tpl, scale)), truth, tpl, scale)
    refined = refine_alpha(img, cov, x=float(X0), y=float(Y0), opacity=tpl.opacity[0])
    fitted = _psnr_on_mark(_restore(img, tpl, refined.cov_k), truth, tpl, scale)
    assert fitted >= plain + 3.0, (plain, fitted)  # measured: 36.2 -> 40.0 dB
    assert fitted >= 38.0, (plain, fitted)


def test_refinement_does_not_hurt_an_exact_mark(case) -> None:
    tpl, scale, truth = case
    img = _apply(truth, tpl, scale=scale)
    cov = scaled_coverage(tpl, scale)
    plain = _psnr_on_mark(_restore(img, tpl, _nominal(tpl, scale)), truth, tpl, scale)
    refined = refine_alpha(img, cov, x=float(X0), y=float(Y0), opacity=tpl.opacity[0])
    fitted = _psnr_on_mark(_restore(img, tpl, refined.cov_k), truth, tpl, scale)
    # 50 dB = 0.8 levels RMS (invisible); the nominal subtraction itself gives ~57 dB here
    assert fitted >= 50.0, (plain, fitted)
    assert all(abs(p.dx) <= 0.5 and abs(p.dy) <= 0.5 for p in refined.parts)


def test_refinement_estimates_opacity_multiplier(case) -> None:
    tpl, scale, truth = case
    img = _apply(truth, tpl, scale=scale, k=0.7)
    cov = scaled_coverage(tpl, scale)
    refined = refine_alpha(img, cov, x=float(X0), y=float(Y0), opacity=tpl.opacity[0])
    assert refined.k_center == pytest.approx(0.7, abs=0.1)
    assert len(refined.parts) >= 8  # letters and icon bars are fitted separately


def test_opacity_is_not_biased_low_by_scene_structure(case) -> None:
    """A busy scene (many bright/dark bars through the logo) must not pull k towards zero."""
    tpl, scale, _truth = case
    rng = np.random.default_rng(7)
    busy = _background(seed=3).copy()
    h = scaled_coverage(tpl, scale).shape[0]
    for _ in range(90):  # dense furniture-like strokes across the whole logo box
        y = int(rng.integers(Y0 - 10, Y0 + h + 10))
        x = int(rng.integers(0, WIDTH - 60))
        busy[y : y + int(rng.integers(1, 4)), x : x + int(rng.integers(30, 220))] += float(
            rng.choice([-40, 40])
        )
    busy = np.clip(busy, 0, 255)
    img = _apply(busy, tpl, scale=scale, k=1.0)
    refined = refine_alpha(img, scaled_coverage(tpl, scale), x=float(X0), y=float(Y0),
                           opacity=tpl.opacity[0])
    assert refined.k_center == pytest.approx(1.0, abs=0.08), refined.k_center
    big = [p.k for p in refined.parts if p.area >= 600]
    assert min(big) >= 0.85, big


def test_refinement_leaves_pixels_outside_the_mark_untouched(case) -> None:
    tpl, scale, truth = case
    img = _apply(truth, tpl, scale=scale, shift=(0.4, 0.4), blur=0.6)
    cov = scaled_coverage(tpl, scale)
    refined = refine_alpha(img, cov, x=float(X0), y=float(Y0), opacity=tpl.opacity[0])
    far = np.ones((HEIGHT, WIDTH), bool)
    h, w = cov.shape
    far[max(0, Y0 - 30) : Y0 + h + 30, max(0, X0 - 30) : X0 + w + 30] = False
    assert float(refined.cov_k[far].max(initial=0.0)) == 0.0
