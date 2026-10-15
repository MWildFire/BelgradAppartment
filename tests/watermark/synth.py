"""Synthetic data for watermark tests: a "room-like" background + a template mark + JPEG.

Images are generated in memory (the repo has no photo fixtures). Mark application is
independent of src/watermark/remove.py (integer position, its own resize) so the tests check
the template geometry instead of repeating the module's own code.
"""

from __future__ import annotations

import io

import cv2
import numpy as np
from PIL import Image

from src.watermark.templates import WatermarkTemplate


def make_background(width: int, height: int, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    ys = np.linspace(0.0, 1.0, height, dtype=np.float32)[:, None]
    top = rng.uniform(150, 220, 3).astype(np.float32)
    bottom = rng.uniform(60, 140, 3).astype(np.float32)
    img = (top * (1 - ys[..., None]) + bottom * ys[..., None]) * np.ones((1, width, 1), np.float32)
    low = rng.normal(0, 25, (max(2, height // 40), max(2, width // 40), 3)).astype(np.float32)
    img += cv2.resize(low, (width, height), interpolation=cv2.INTER_CUBIC)
    for _ in range(12):  # "furniture": rectangles with sharp edges
        x0, y0 = int(rng.integers(0, width - 20)), int(rng.integers(0, height - 20))
        x1 = min(width, x0 + int(rng.integers(20, max(21, width // 3))))
        y1 = min(height, y0 + int(rng.integers(20, max(21, height // 3))))
        img[y0:y1, x0:x1] = img[y0:y1, x0:x1] * 0.5 + rng.uniform(20, 230, 3).astype(np.float32) * 0.5
    img += rng.normal(0, 6, img.shape).astype(np.float32)
    return np.clip(img, 0, 255).astype(np.uint8)


def jpeg_roundtrip(img: np.ndarray, quality: int = 88) -> np.ndarray:
    buf = io.BytesIO()
    Image.fromarray(img).save(buf, "JPEG", quality=quality)
    return np.asarray(Image.open(io.BytesIO(buf.getvalue())).convert("RGB"))


def apply_mark(img: np.ndarray, tpl: WatermarkTemplate, *, opacity_scale: float = 1.0) -> np.ndarray:
    """Apply every template instance at its predicted place (as the platform does)."""
    out = img.astype(np.float32)
    h, w = img.shape[:2]
    for p in tpl.predicted(w, h):
        tw, th = max(2, round(tpl.width * p.scale)), max(2, round(tpl.height * p.scale))
        cov = cv2.resize(tpl.coverage, (tw, th), interpolation=cv2.INTER_AREA)
        x0, y0 = round(p.x), round(p.y)
        xs0, ys0 = max(0, x0), max(0, y0)
        xs1, ys1 = min(w, x0 + tw), min(h, y0 + th)
        if xs1 <= xs0 or ys1 <= ys0:
            continue
        c = cov[ys0 - y0 : ys1 - y0, xs0 - x0 : xs1 - x0][..., None]
        alpha = c * np.asarray(tpl.opacity, np.float32)[None, None, :] * opacity_scale
        region = out[ys0:ys1, xs0:xs1]
        out[ys0:ys1, xs0:xs1] = alpha * 255.0 + (1 - alpha) * region
    return np.clip(out, 0, 255).astype(np.uint8)


def mark_mask(tpl: WatermarkTemplate, width: int, height: int, *, dilate: int = 4) -> np.ndarray:
    mask = np.zeros((height, width), np.uint8)
    for p in tpl.predicted(width, height):
        tw, th = max(2, round(tpl.width * p.scale)), max(2, round(tpl.height * p.scale))
        cov = cv2.resize(tpl.coverage, (tw, th), interpolation=cv2.INTER_AREA)
        x0, y0 = round(p.x), round(p.y)
        xs0, ys0, xs1, ys1 = max(0, x0), max(0, y0), min(width, x0 + tw), min(height, y0 + th)
        mask[ys0:ys1, xs0:xs1] |= (cov[ys0 - y0 : ys1 - y0, xs0 - x0 : xs1 - x0] > 0.05).astype(np.uint8)
    if dilate:
        mask = cv2.dilate(mask, np.ones((2 * dilate + 1, 2 * dilate + 1), np.uint8))
    return mask > 0
