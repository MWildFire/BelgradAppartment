"""Self-checks that decide whether a found mark is removed or honestly left in place.

Two cheap checks (both configured in the template JSON, see `WatermarkTemplate`):

* before LaMa: the inpainting mask is larger than `lama_max_mask_fraction` of the photo. LaMa
  fills small holes plausibly but invents scenery when the mask covers a large share of the
  frame (central Halo Oglasi logo on 1000 px photos: invented sofa backrest, smeared facade).
* after removal: the template still correlates with the result at its place
  (`residual >= max_residual`): the mark was not cancelled, so the original is kept.

A kept mark is reported with `method="skip"` and `kept_reason="self_check"`.
"""

from __future__ import annotations

import numpy as np

from src.watermark.detect import Detection
from src.watermark.remove import alpha_map, support_mask
from src.watermark.templates import WatermarkTemplate

MASK_ALPHA_THRESHOLD = 0.02  # the same threshold `remove_inpaint` builds its mask with


def lama_mask_fraction(
    tpl: WatermarkTemplate, det: Detection, shape_hw: tuple[int, int], grow_px: int
) -> float:
    """Share of the photo covered by the inpainting mask `remove_inpaint` would build."""
    mask = support_mask(
        alpha_map(tpl, det, shape_hw), threshold=MASK_ALPHA_THRESHOLD, dilate_px=grow_px
    )
    return float(np.count_nonzero(mask)) / float(shape_hw[0] * shape_hw[1])


def mask_too_large(
    tpl: WatermarkTemplate, det: Detection, shape_hw: tuple[int, int], grow_px: int
) -> bool:
    """True when the template limits the LaMa mask size and this mark exceeds it."""
    limit = tpl.lama_max_mask_fraction
    return limit is not None and lama_mask_fraction(tpl, det, shape_hw, grow_px) > limit


def removal_failed(tpl: WatermarkTemplate, residual: float) -> bool:
    """True when the template still correlates with the cleaned photo at its place."""
    return residual >= tpl.max_residual
