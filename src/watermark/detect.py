"""Find a mark instance near its predicted place: NCC of gradient fields + refinement.

Marks are white and semi-transparent, so their edges are the most reliable signal. The image
and template gradient fields (gx, gy) are compared with normalized correlation
(TM_CCORR_NORMED over two channels). The search runs only inside a +/- pos_tol*W window around
the prediction and over a few scales: platform geometry is strict (checked on ~1300 photos).

Two stages: coarse on a downscaled copy (long side <= WORK_MAX), then refinement at full
resolution in a +/- (2/f + 2) px window with 0.5 % scale steps and a sub-pixel parabola fit.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import cv2
import numpy as np

from src.watermark.templates import Placement, WatermarkTemplate

WORK_MAX = 900
_REFINE_STEPS = (-0.01, -0.005, 0.0, 0.005, 0.01)


@dataclass(frozen=True)
class Detection:
    template_id: str
    instance: int
    x: float  # template top-left corner in image pixels (sub-pixel)
    y: float
    scale: float
    score: float
    dx: float  # offset from the prediction, px
    dy: float


def to_gray(img_rgb: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(img_rgb, cv2.COLOR_RGB2GRAY).astype(np.float32)


def gradient_field(gray: np.ndarray) -> np.ndarray:
    g = cv2.GaussianBlur(gray, (0, 0), 1.0)
    gx = cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)
    return cv2.merge([gx, gy])


def scaled_coverage(tpl: WatermarkTemplate, scale: float) -> np.ndarray:
    w = max(2, round(tpl.width * scale))
    h = max(2, round(tpl.height * scale))
    interp = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
    return cv2.resize(tpl.coverage, (w, h), interpolation=interp)


def _best_in_window(
    gray: np.ndarray, cov: np.ndarray, cx: float, cy: float, radius: int
) -> tuple[float, float, float] | None:
    """Best top-left position in a window around (cx, cy): (score, x, y) or None."""
    th, tw = cov.shape
    H, W = gray.shape
    x0, y0 = max(0, math.floor(cx) - radius), max(0, math.floor(cy) - radius)
    x1, y1 = min(W - tw, math.floor(cx) + radius), min(H - th, math.floor(cy) + radius)
    if x1 < x0 or y1 < y0:
        return None
    sub = gray[y0 : y1 + th, x0 : x1 + tw]
    res = cv2.matchTemplate(gradient_field(sub), gradient_field(cov * 255.0), cv2.TM_CCORR_NORMED)
    _, score, _, (px, py) = cv2.minMaxLoc(res)
    fx, fy = 0.0, 0.0
    if 0 < px < res.shape[1] - 1:
        left, mid, right = res[py, px - 1], res[py, px], res[py, px + 1]
        den = left - 2.0 * mid + right
        fx = float(0.5 * (left - right) / den) if abs(den) > 1e-9 else 0.0
    if 0 < py < res.shape[0] - 1:
        up, mid, down = res[py - 1, px], res[py, px], res[py + 1, px]
        den = up - 2.0 * mid + down
        fy = float(0.5 * (up - down) / den) if abs(den) > 1e-9 else 0.0
    fx, fy = max(-0.5, min(0.5, fx)), max(-0.5, min(0.5, fy))
    return float(score), x0 + px + fx, y0 + py + fy


def match_instance(
    gray: np.ndarray, tpl: WatermarkTemplate, placement: Placement, *, work_max: int = WORK_MAX
) -> Detection | None:
    """Best match of one instance near its prediction (score may be below the threshold)."""
    H, W = gray.shape
    f = min(1.0, work_max / float(max(H, W)))
    small = gray if f >= 1.0 else cv2.resize(
        gray, (round(W * f), round(H * f)), interpolation=cv2.INTER_AREA
    )
    radius_full = int(tpl.pos_tol * W) + 4
    radius_small = max(3, int(radius_full * f) + 2)
    coarse: tuple[float, float, float, float] | None = None  # score, scale, x, y (full res)
    for mult in tpl.scale_search:
        scale = placement.scale * mult
        cov = scaled_coverage(tpl, scale * f)
        hit = _best_in_window(small, cov, placement.x * f, placement.y * f, radius_small)
        if hit is None:
            continue
        score, x, y = hit
        if coarse is None or score > coarse[0]:
            coarse = (score, scale, x / f, y / f)
    if coarse is None:
        return None
    _, scale0, x0, y0 = coarse
    radius_refine = math.ceil(2.0 / f) + 2
    best: tuple[float, float, float, float] | None = None
    for step in _REFINE_STEPS:
        scale = scale0 * (1.0 + step)
        hit = _best_in_window(gray, scaled_coverage(tpl, scale), x0, y0, radius_refine)
        if hit is None:
            continue
        score, x, y = hit
        if best is None or score > best[0]:
            best = (score, scale, x, y)
    if best is None:
        return None
    score, scale, x, y = best
    return Detection(
        template_id=tpl.id,
        instance=placement.instance,
        x=x,
        y=y,
        scale=scale,
        score=score,
        dx=x - placement.x,
        dy=y - placement.y,
    )


def score_at(gray: np.ndarray, tpl: WatermarkTemplate, det: Detection) -> float:
    """Correlation exactly at the detected place (residual check after removal)."""
    cov = scaled_coverage(tpl, det.scale)
    hit = _best_in_window(gray, cov, det.x, det.y, 1)
    return 0.0 if hit is None else hit[0]
