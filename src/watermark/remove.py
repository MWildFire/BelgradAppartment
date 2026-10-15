"""Remove a detected mark.

reverse_blend: exact "subtraction" of a semi-transparent white mark,
O = (J - k*a*255) / (1 - k*a). Keeps the real pixels under the mark (best for the big
CityExpert logo). The multiplier k is chosen per connected part of the mark (each letter, each
icon bar) so the restored pixels match the brightness of nearby unmarked pixels: platforms draw
parts of their logo with slightly different opacity. A thin band along the mark contour is then
filled with Telea.

inpaint: the mark mask (dilated by a few pixels) is filled by an inpainter (LaMa or Telea).
Used for thin or uneven marks (Halo Oglasi, 4zida) where subtraction leaves outlines.
"""

from __future__ import annotations

import cv2
import numpy as np

from src.watermark.detect import Detection, scaled_coverage
from src.watermark.inpaint import Inpainter, TeleaInpainter
from src.watermark.templates import WatermarkTemplate

WHITE = 255.0
K_GRID = tuple(round(0.60 + 0.02 * i, 2) for i in range(41))  # 0.60 .. 1.40
K_PART_LIMIT = 0.25  # a part's k stays within the central k +/- this
BRIGHT_BACKGROUND = 235.0  # on near-white backgrounds the brightness criterion is unreliable
MIN_VALID_PIXELS = 30
PLATEAU = 0.85  # compare only part pixels with alpha >= 85 % of the part's peak
OUTSIDE_MARGIN = 2  # px between the template edge and pixels used as background
BACKGROUND_SIGMA = 4.0  # px, Gaussian radius of the local background estimate
BAND_INPAINTER = TeleaInpainter(radius=3)


def alpha_map(tpl: WatermarkTemplate, det: Detection, shape_hw: tuple[int, int]) -> np.ndarray:
    """Mark alpha for the whole image: HxWx3 float32 (per channel R, G, B)."""
    H, W = shape_hw
    cov = scaled_coverage(tpl, det.scale)
    m = np.float32([[1, 0, det.x], [0, 1, det.y]])
    placed = cv2.warpAffine(
        cov, m, (W, H), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0
    )
    opacity = np.asarray(tpl.opacity, dtype=np.float32)
    return placed[..., None] * opacity[None, None, :]


def reverse_blend(img_rgb: np.ndarray, alpha: np.ndarray, k: float | np.ndarray) -> np.ndarray:
    """k: a scalar or an HxW per-pixel map."""
    k_arr = np.asarray(k, dtype=np.float32)
    if k_arr.ndim == 2:
        k_arr = k_arr[..., None]
    a = np.clip(alpha * k_arr, 0.0, 0.95)
    return ((img_rgb.astype(np.float32) - a * WHITE) / (1.0 - a)).astype(np.float32)


def _window(lum: np.ndarray, x: int, y: int, w: int, h: int) -> tuple[int, int, int, int]:
    """Box (x, y, w, h) grown by 12 px and clipped: (y0, y1, x0, x1)."""
    return (max(0, y - 12), min(lum.shape[0], y + h + 12),
            max(0, x - 12), min(lum.shape[1], x + w + 12))


def _best_k(
    img_rgb: np.ndarray,
    alpha: np.ndarray,
    lum: np.ndarray,
    window: tuple[int, int, int, int],
    part: np.ndarray,
) -> tuple[float, int] | None:
    """(k, weight) for one part: the K_GRID value whose median (restored part pixel - nearby
    unmarked background) is closest to 0; weight = number of pixels it was measured on.

    Only the part's plateau (alpha >= PLATEAU of its peak) is compared, and the background
    starts OUTSIDE_MARGIN px beyond the template: real marks have softer edges than the
    template, and measuring on those edges biases k (bars of the CityExpert icon came out as
    visible rectangles). `part` is a boolean mask inside `window` (y0, y1, x0, x1). None when
    too few plateau pixels lie near unmarked ones or the background is near-white (there the
    mark is barely visible and the criterion is noise).
    """
    y0, y1, x0, x1 = window
    local = lum[y0:y1, x0:x1]
    size = 2 * OUTSIDE_MARGIN + 1
    support = cv2.dilate((local >= 0.005).astype(np.uint8), np.ones((size, size), np.uint8))
    outside = (support == 0).astype(np.float32)
    den = cv2.GaussianBlur(outside, (0, 0), BACKGROUND_SIGMA)
    peak = float(local[part].max()) if part.any() else 0.0
    valid = part & (local >= PLATEAU * peak) & (den > 0.03)
    count = int(valid.sum())
    if count < MIN_VALID_PIXELS:
        return None
    sub = img_rgb[y0:y1, x0:x1]
    gray = sub.astype(np.float32).mean(axis=2)
    background = cv2.GaussianBlur(gray * outside, (0, 0), BACKGROUND_SIGMA) / np.maximum(den, 1e-6)
    if float(np.median(background[valid])) > BRIGHT_BACKGROUND:
        return None
    al = alpha[y0:y1, x0:x1]
    best_k, best_err = 1.0, float("inf")
    for k in K_GRID:
        r = reverse_blend(sub, al, k).mean(axis=2).astype(np.float32)
        bg = cv2.GaussianBlur(r * outside, (0, 0), BACKGROUND_SIGMA) / np.maximum(den, 1e-6)
        err = abs(float(np.median((r - bg)[valid])))
        if err < best_err:
            best_k, best_err = k, err
    return best_k, count


def refine_opacity(img_rgb: np.ndarray, alpha: np.ndarray) -> float:
    """One k for the whole mark (1.0 when it cannot be measured)."""
    lum = alpha.mean(axis=2)
    peak = float(lum.max())
    if peak <= 0.0:
        return 1.0
    core = lum > 0.5 * peak
    ys, xs = np.nonzero(core)
    x, y = int(xs.min()), int(ys.min())
    window = _window(lum, x, y, int(xs.max()) - x + 1, int(ys.max()) - y + 1)
    y0, y1, x0, x1 = window
    found = _best_k(img_rgb, alpha, lum, window, core[y0:y1, x0:x1])
    return 1.0 if found is None else found[0]


def _weighted_median(values: list[tuple[float, int]]) -> float:
    ordered = sorted(values)
    half = sum(w for _k, w in ordered) / 2.0
    acc = 0
    for k, w in ordered:
        acc += w
        if acc >= half:
            return k
    return ordered[-1][0]


def opacity_map(img_rgb: np.ndarray, alpha: np.ndarray) -> tuple[np.ndarray, float]:
    """Per-pixel k (HxW float32) from per-part estimates, plus the central k.

    Parts = connected components of the mark core (letters, icon bars). The central k is the
    pixel-weighted median of the measured parts (a whole-mark measurement when no part can be
    measured); each part's k stays within central +/- K_PART_LIMIT. Pixels between parts get a
    smooth blend of nearby parts' k, pixels far from any part get the central k.
    """
    lum = alpha.mean(axis=2)
    peak = float(lum.max())
    if peak <= 0.0:
        return np.ones(lum.shape, np.float32), 1.0
    core = (lum > 0.5 * peak).astype(np.uint8)
    n, labels, stats, _centroids = cv2.connectedComponentsWithStats(core, connectivity=8)
    measured: dict[int, tuple[float, int]] = {}
    for i in range(1, n):
        x, y, w, h = (int(v) for v in stats[i, :4])
        window = _window(lum, x, y, w, h)
        y0, y1, x0, x1 = window
        found = _best_k(img_rgb, alpha, lum, window, labels[y0:y1, x0:x1] == i)
        if found is not None:
            measured[i] = found
    center = (
        _weighted_median(list(measured.values())) if measured else refine_opacity(img_rgb, alpha)
    )
    values = np.full(n, center, np.float32)
    for i, (k, _weight) in measured.items():
        values[i] = min(max(k, center - K_PART_LIMIT), center + K_PART_LIMIT)
    known = (labels > 0).astype(np.float32)
    num = cv2.GaussianBlur(values[labels] * known, (0, 0), 3.0)
    den = cv2.GaussianBlur(known, (0, 0), 3.0)
    kmap = np.full(lum.shape, center, np.float32)
    near = den > 1e-3
    kmap[near] = num[near] / den[near]
    return kmap, center


def support_mask(alpha: np.ndarray, *, threshold: float = 0.02, dilate_px: int = 0) -> np.ndarray:
    mask = (alpha.mean(axis=2) > threshold).astype(np.uint8)
    if dilate_px > 0:
        size = 2 * dilate_px + 1
        mask = cv2.dilate(mask, np.ones((size, size), np.uint8))
    return mask


def edge_band(alpha: np.ndarray, *, band_px: int = 2) -> np.ndarray:
    support = support_mask(alpha, threshold=0.01)
    band = cv2.morphologyEx(support, cv2.MORPH_GRADIENT, np.ones((3, 3), np.uint8))
    if band_px > 0:
        size = 2 * band_px + 1
        band = cv2.dilate(band, np.ones((size, size), np.uint8))
    return band


def remove_reverse_blend(
    img_rgb: np.ndarray, tpl: WatermarkTemplate, det: Detection
) -> tuple[np.ndarray, float]:
    """Returns (cleaned image, central k)."""
    alpha = alpha_map(tpl, det, img_rgb.shape[:2])
    kmap, k_center = opacity_map(img_rgb, alpha)
    restored = np.clip(reverse_blend(img_rgb, alpha, kmap), 0, 255).astype(np.uint8)
    return BAND_INPAINTER.inpaint(restored, edge_band(alpha)), k_center


def remove_inpaint(
    img_rgb: np.ndarray,
    tpl: WatermarkTemplate,
    det: Detection,
    inpainter: Inpainter,
    *,
    dilate_px: int = 3,
) -> np.ndarray:
    alpha = alpha_map(tpl, det, img_rgb.shape[:2])
    grow = max(1, round(dilate_px * max(det.scale, 0.5)))
    mask = support_mask(alpha, threshold=0.02, dilate_px=grow)
    return inpainter.inpaint(img_rgb, mask)
