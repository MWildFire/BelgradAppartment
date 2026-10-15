"""Optional LaMa treatment for Halo corner text; not a default pipeline strategy.

Use with the existing ``remove_inpaint`` mask, only for ``halo_corner``. This
adapter adds two native pixels of mask support, supplies reflected context at
frame edges, and blends inward over three pixels. It shares the normal LaMa
singleton and its inference lock; selecting a corner mode must not load a second
model. The caller decides what to do when the factory returns None.
"""

from __future__ import annotations

import cv2
import numpy as np

from src.watermark.lama import LamaInpainter, get_lama_inpainter

Box = tuple[int, int, int, int]  # y0, y1, x0, x1; upper bounds exclusive
Pads = tuple[tuple[int, int], tuple[int, int]]


def _reflect_to_multiple(arr: np.ndarray) -> np.ndarray:
    h, w = arr.shape[:2]
    pads = ((0, -h % 8), (0, -w % 8)) + (((0, 0),) if arr.ndim == 3 else ())
    return np.pad(arr, pads, mode="reflect")


def _corner_crop(image: np.ndarray, mask: np.ndarray) -> tuple[np.ndarray, np.ndarray, Box, Pads]:
    """Native crop with max(64, mask extent) context on each axis, even at edges."""
    ys, xs = np.nonzero(mask)
    h, w = mask.shape
    my, mx = max(64, int(ys.max() - ys.min() + 1)), max(64, int(xs.max() - xs.min() + 1))
    top, bottom = int(ys.min()) - my, int(ys.max()) + my + 1
    left, right = int(xs.min()) - mx, int(xs.max()) + mx + 1
    y0, y1, x0, x1 = max(0, top), min(h, bottom), max(0, left), min(w, right)
    pads = ((max(0, -top), max(0, bottom - h)), (max(0, -left), max(0, right - w)))
    crop = np.pad(image[y0:y1, x0:x1], (*pads, (0, 0)), mode="reflect")
    cm = np.pad(mask[y0:y1, x0:x1], pads, mode="reflect")
    return crop, cm, (y0, y1, x0, x1), pads


def _feather_alpha(mask: np.ndarray) -> np.ndarray:
    """Feather only inside support; caller includes reflected frame context."""
    distance = cv2.distanceTransform(
        np.pad((mask > 0).astype(np.uint8), 1), cv2.DIST_L2, cv2.DIST_MASK_PRECISE
    )[1:-1, 1:-1]
    return np.clip(distance / 3.0, 0, 1)


class LamaCornerInpainter:
    """Halo-only adapter around the same model used for center and Zida marks."""

    name = "lama"

    def __init__(self, lama: LamaInpainter) -> None:
        self.lama = lama

    def _predict_rgb(self, crop: np.ndarray, mask: np.ndarray) -> np.ndarray:
        import torch

        h, w = mask.shape
        image = torch.from_numpy(_reflect_to_multiple(crop)).permute(2, 0, 1)
        image = image.unsqueeze(0).float() / 255.0
        mask_t = torch.from_numpy((_reflect_to_multiple(mask) > 0).astype(np.float32))
        mask_t = mask_t.unsqueeze(0).unsqueeze(0)
        model = self.lama._load()
        with self.lama._lock, torch.inference_mode():
            result = model(image, mask_t)
        return (result[0].permute(1, 2, 0).numpy() * 255.0).clip(0, 255).astype(np.uint8)[:h, :w]

    def inpaint(self, img_rgb: np.ndarray, mask: np.ndarray) -> np.ndarray:
        if not np.any(mask):
            return img_rgb.copy()
        support = cv2.dilate((mask > 0).astype(np.uint8), np.ones((5, 5), np.uint8))
        crop, cm, (y0, y1, x0, x1), pads = _corner_crop(img_rgb, support)
        prediction = self._predict_rgb(crop, cm)
        top, left = pads[0][0], pads[1][0]
        visible = (slice(top, top + y1 - y0), slice(left, left + x1 - x0))
        prediction = prediction[visible]
        alpha = _feather_alpha(cm)[visible][..., None]
        original = img_rgb[y0:y1, x0:x1]
        mixed = np.rint(original * (1 - alpha) + prediction * alpha).clip(0, 255).astype(np.uint8)
        out = img_rgb.copy()
        keep = support[y0:y1, x0:x1] > 0
        out[y0:y1, x0:x1][keep] = mixed[keep]
        return out


def get_lama_corner_inpainter() -> LamaCornerInpainter | None:
    """Return an adapter sharing the existing model, or None if LaMa is unavailable."""
    lama = get_lama_inpainter()
    return LamaCornerInpainter(lama) if lama is not None else None
