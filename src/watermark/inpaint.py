"""Inpainters fill the masked pixels with plausible content.

TeleaInpainter is the classic fill (imgops.classic_inpaint: harmonic interpolation of the pixels
around the mask; the name and the "inpaint:telea" report value are kept from R04, where it was
OpenCV's TELEA). It needs no model and is good for thin bands and small text. It fills the
edge band of CityExpert reverse-blend results and runs for whole marks only when the operator
asks for it (Halo corner mode ``telea``).
LaMa (neural network, optional, needs torch) lives in `src/watermark/lama.py`;
`get_inpainter("lama")` returns None when LaMa is unavailable, and the pipeline then KEEPS the
mark instead of silently switching to a worse method.
"""

from __future__ import annotations

import logging
from typing import Protocol

import numpy as np

from src.watermark import imgops

logger = logging.getLogger(__name__)


class Inpainter(Protocol):
    name: str

    def inpaint(self, img_rgb: np.ndarray, mask: np.ndarray) -> np.ndarray:
        """img_rgb: uint8 HxWx3, mask: uint8 HxW (non-zero = fill). Returns uint8 HxWx3."""
        ...


class TeleaInpainter:
    name = "telea"

    def __init__(self, radius: int = 5) -> None:
        self.radius = radius  # R04 API; the harmonic fill has no neighbourhood radius

    def inpaint(self, img_rgb: np.ndarray, mask: np.ndarray) -> np.ndarray:
        if not mask.any():
            return img_rgb.copy()
        return imgops.classic_inpaint(img_rgb, mask)


_TELEA = TeleaInpainter(radius=5)
_lama_warned = False


def get_inpainter(preferred: str) -> Inpainter | None:
    """The requested inpainter; None when ``lama`` is requested and LaMa is unavailable.

    Logs one warning per process in that case. Never falls back to Telea by itself.
    """
    global _lama_warned
    if preferred != "lama":
        return _TELEA
    try:
        from src.watermark.lama import get_lama_inpainter
    except ImportError:
        get_lama_inpainter = None  # type: ignore[assignment]
    lama = get_lama_inpainter() if get_lama_inpainter is not None else None
    if lama is None and not _lama_warned:
        logger.warning("[watermark] LaMa unavailable - marks that need it are kept as they are")
        _lama_warned = True
    return lama
