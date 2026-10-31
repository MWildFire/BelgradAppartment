"""LaMa: neural inpainting (big-lama, Apache-2.0) for marks that cannot be "subtracted".

Optional: needs torch (already on the server via the `clip` extra) and the model file
(`otz wm-fetch-model` downloads it and checks sha256). Without torch/model, or with
WATERMARK_LAMA_ENABLED=0, `get_lama_inpainter()` returns None and the pipeline uses Telea.

Processing: crop a rectangle around the mask with `margin` pixels, downscale to `max_side`
if needed, pad to a multiple of 8, run the model and paste back ONLY the masked pixels;
everything else stays byte-for-byte identical to the input.
"""

from __future__ import annotations

import hashlib
import importlib.util
import logging
import os
import threading
from pathlib import Path
from typing import Any

import numpy as np

from src.watermark import imgops
from src.watermark.lama_crop import plan_crop
from src.watermark.settings import get_watermark_settings

logger = logging.getLogger(__name__)

# Crop budget: model memory grows ~2.6 KB per crop pixel (Linux, measured in WM05); 105k px keeps
# the whole bot process near 1 GiB. Small crops (most 4zida marks) are untouched by it.
DEFAULT_MAX_PIXELS = 105_000
DEFAULT_MIN_MARGIN = 16

MODEL_URL = "https://github.com/Sanster/models/releases/download/add_big_lama/big-lama.pt"
MODEL_SHA256 = "344c77bbcb158f17dd143070d1e789f38a66c04202311ae3a258ef66667a9ea9"
MODEL_BYTES = 205_669_692


def _pad_to_multiple(arr: np.ndarray, mod: int = 8) -> np.ndarray:
    h, w = arr.shape[:2]
    ph, pw = (mod - h % mod) % mod, (mod - w % mod) % mod
    pads: tuple[tuple[int, int], ...] = ((0, ph), (0, pw)) + (((0, 0),) if arr.ndim == 3 else ())
    return np.pad(arr, pads, mode="symmetric")


class LamaInpainter:
    name = "lama"

    def __init__(
        self,
        model_path: Path,
        *,
        margin: int = 96,
        max_side: int = 1024,
        max_pixels: int = DEFAULT_MAX_PIXELS,
        min_margin: int = DEFAULT_MIN_MARGIN,
    ) -> None:
        self.model_path = model_path
        self.margin = margin
        self.max_side = max_side
        self.max_pixels = max_pixels  # crop area budget (model memory ~2.6 KB/px); 0 = off
        self.min_margin = min_margin
        self._model: Any = None
        self._lock = threading.Lock()

    def _load(self) -> Any:
        with self._lock:
            if self._model is None:
                import torch

                model = torch.jit.load(str(self.model_path), map_location="cpu")
                model.eval()
                self._model = model
                logger.info("[watermark] LaMa model loaded from %s", self.model_path)
            return self._model

    def inpaint(self, img_rgb: np.ndarray, mask: np.ndarray) -> np.ndarray:
        import torch

        sel = mask > 0
        if not sel.any():
            return img_rgb.copy()
        ys, xs = np.nonzero(sel)
        plan = plan_crop(
            (int(ys.min()), int(ys.max()), int(xs.min()), int(xs.max())),
            sel.shape,
            margin=self.margin,
            min_margin=self.min_margin,
            max_pixels=self.max_pixels,
            max_side=self.max_side,
        )
        y0, y1, x0, x1 = plan.box
        crop = img_rgb[y0:y1, x0:x1]
        crop_mask = sel[y0:y1, x0:x1].astype(np.uint8)
        ch, cw = crop.shape[:2]
        factor = plan.scale
        if factor < 1.0:
            size = (round(cw * factor), round(ch * factor))
            work = imgops.resize(crop, size, "area")
            work_mask = imgops.resize(crop_mask, size, "nearest")
        else:
            work, work_mask = crop, crop_mask
        hh, ww = work.shape[:2]
        image = torch.from_numpy(_pad_to_multiple(work)).permute(2, 0, 1).unsqueeze(0).float()
        image = image / 255.0
        mask_t = torch.from_numpy((_pad_to_multiple(work_mask) > 0).astype(np.float32))
        mask_t = mask_t.unsqueeze(0).unsqueeze(0)
        model = self._load()
        with self._lock, torch.inference_mode():
            result = model(image, mask_t)
        res = (result[0].permute(1, 2, 0).numpy() * 255.0).clip(0, 255).astype(np.uint8)[:hh, :ww]
        if factor < 1.0:
            res = imgops.resize(res, (cw, ch), "cubic")
        out = img_rgb.copy()
        region = out[y0:y1, x0:x1]
        keep = crop_mask > 0
        region[keep] = res[keep]
        return out


_instance: LamaInpainter | None = None
_instance_lock = threading.Lock()


def torch_available() -> bool:
    return importlib.util.find_spec("torch") is not None


def get_lama_inpainter() -> LamaInpainter | None:
    """LaMa singleton, or None (disabled by flag / no torch / no model file)."""
    global _instance
    settings = get_watermark_settings()
    if not settings.watermark_lama_enabled:
        return None
    model_path = Path(settings.watermark_lama_model_path)
    if not model_path.is_file() or not torch_available():
        return None
    with _instance_lock:
        if _instance is None or _instance.model_path != model_path:
            _instance = LamaInpainter(model_path)
        return _instance


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_model(dest: Path, *, url: str = MODEL_URL, sha256: str = MODEL_SHA256) -> Path:
    """Download the model to a temp file, verify sha256, then move it into place."""
    import httpx

    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and sha256_of(dest) == sha256:
        return dest
    tmp = dest.with_name(f".{dest.name}.{os.getpid()}.part")
    try:
        with httpx.stream("GET", url, follow_redirects=True, timeout=httpx.Timeout(60.0)) as resp:
            resp.raise_for_status()
            with tmp.open("wb") as fh:
                for chunk in resp.iter_bytes(1 << 20):
                    fh.write(chunk)
    except BaseException:
        tmp.unlink(missing_ok=True)  # a dropped connection must not leave a partial model behind
        raise
    actual = sha256_of(tmp)
    if actual != sha256:
        tmp.unlink(missing_ok=True)
        raise ValueError(f"LaMa model checksum mismatch: {actual} != {sha256}")
    os.replace(tmp, dest)
    return dest
