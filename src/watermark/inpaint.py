"""Inpainters fill the masked pixels with plausible content.

TeleaInpainter uses OpenCV (cv2.INPAINT_TELEA): fast, no extra dependencies, good for small
text and thin bands; it runs only when the operator asks for it (Halo corner mode ``telea``).
LaMa (neural network, optional, needs torch) lives in `src/watermark/lama.py`;
`get_inpainter("lama")` returns None when LaMa is unavailable, and the pipeline then KEEPS the
mark instead of silently switching to a worse method.
"""

from __future__ import annotations

import logging
from typing import Protocol

import cv2
import numpy as np

logger = logging.getLogger(__name__)


class Inpainter(Protocol):
    name: str

    def inpaint(self, img_rgb: np.ndarray, mask: np.ndarray) -> np.ndarray:
        """img_rgb: uint8 HxWx3, mask: uint8 HxW (non-zero = fill). Returns uint8 HxWx3."""
        ...


class TeleaInpainter:
    name = "telea"

    def __init__(self, radius: int = 5) -> None:
        self.radius = radius

    def inpaint(self, img_rgb: np.ndarray, mask: np.ndarray) -> np.ndarray:
        if not mask.any():
            return img_rgb.copy()
        bgr = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2BGR)
        out = cv2.inpaint(bgr, (mask > 0).astype(np.uint8), self.radius, cv2.INPAINT_TELEA)
        return cv2.cvtColor(out, cv2.COLOR_BGR2RGB)


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
