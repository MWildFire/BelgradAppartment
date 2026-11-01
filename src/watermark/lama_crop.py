"""Which part of the photo goes to LaMa, and at what scale (memory budget of the model).

LaMa's working memory grows with the crop area (about 2.6 KB per pixel on Linux), so the crop
has a pixel budget. R04 behaviour is kept whenever the crop fits: margin around the mask and a
longest-side cap. Over the budget the margin shrinks first (native resolution, less context),
a downscale is the last resort - on large fills it visibly blurs the result.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

MARGIN_STEP = 8


@dataclass(frozen=True)
class CropPlan:
    box: tuple[int, int, int, int]  # y0, y1, x0, x1 (upper bounds exclusive), clipped to the image
    margin: int
    scale: float  # <= 1.0, applied to the crop before it goes to the model


def _clipped_box(
    bbox: tuple[int, int, int, int], shape: tuple[int, int], margin: int
) -> tuple[int, int, int, int]:
    y_min, y_max, x_min, x_max = bbox  # inclusive extent of the mask
    height, width = shape
    return (
        max(0, y_min - margin),
        min(height, y_max + margin + 1),
        max(0, x_min - margin),
        min(width, x_max + margin + 1),
    )


def plan_crop(
    bbox: tuple[int, int, int, int],
    shape: tuple[int, int],
    *,
    margin: int,
    min_margin: int,
    max_pixels: int,
    max_side: int,
) -> CropPlan:
    """Crop box and scale for a mask with inclusive extent `bbox` in an image of `shape`.

    `max_pixels` <= 0 switches the pixel budget off (only `max_side` applies, as in R04).
    """
    used = margin
    box = _clipped_box(bbox, shape, used)
    if max_pixels > 0:
        floor = min(min_margin, margin)
        while (box[1] - box[0]) * (box[3] - box[2]) > max_pixels and used > floor:
            used = max(floor, used - MARGIN_STEP)
            box = _clipped_box(bbox, shape, used)
    height, width = box[1] - box[0], box[3] - box[2]
    scale = min(1.0, max_side / float(max(height, width)))
    if max_pixels > 0 and height * width * scale**2 > max_pixels:
        scale = min(scale, math.sqrt(max_pixels / float(height * width)))
    return CropPlan(box=box, margin=used, scale=scale)
