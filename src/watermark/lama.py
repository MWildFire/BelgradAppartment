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
from typing import Any, Protocol

import numpy as np

from src.watermark import imgops
from src.watermark.lama_crop import plan_crop
from src.watermark.lama_model import LamaError, ModelRunner
from src.watermark.settings import get_watermark_settings

logger = logging.getLogger(__name__)

# Crop budget: model memory grows ~2.6 KB per crop pixel (Linux, measured in WM05). 75k px keeps
# the bot + LaMa worker at 0.96-1.0 GiB (sum of RSS) on an album of ten; 105k gave 1.02-1.07 GiB.
# Crops inside the budget are processed exactly as in R04.
DEFAULT_MAX_PIXELS = 75_000
DEFAULT_MIN_MARGIN = 16

MODEL_URL = "https://github.com/Sanster/models/releases/download/add_big_lama/big-lama.pt"
MODEL_SHA256 = "344c77bbcb158f17dd143070d1e789f38a66c04202311ae3a258ef66667a9ea9"
MODEL_BYTES = 205_669_692


class Runner(Protocol):
    """Where the model runs: `ModelRunner` (this process) or `WorkerRunner` (a worker process)."""

    def predict(self, image: np.ndarray, mask: np.ndarray) -> np.ndarray: ...


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
        runner: Runner | None = None,
    ) -> None:
        self.model_path = model_path
        self.margin = margin
        self.max_side = max_side
        self.max_pixels = max_pixels  # crop area budget (model memory ~2.6 KB/px); 0 = off
        self.min_margin = min_margin
        # The model lives in a runner: in this process (ModelRunner) or in a worker process
        # (WorkerRunner). `inpaint` and the corner adapter both go through `predict`.
        self._runner: Runner = runner if runner is not None else ModelRunner(
            model_path, threads=get_watermark_settings().watermark_torch_threads
        )

    # The in-process model and its lock, for tests and diagnostics (not used with a worker).
    @property
    def _model(self) -> Any:
        return self._runner._model  # type: ignore[attr-defined]

    @_model.setter
    def _model(self, value: Any) -> None:
        self._runner._model = value  # type: ignore[attr-defined]

    @property
    def _lock(self) -> threading.Lock:
        return self._runner._lock  # type: ignore[attr-defined]

    def _load(self) -> Any:
        with self._lock:
            return self._runner._ensure_loaded()  # type: ignore[attr-defined]

    def predict(self, image: np.ndarray, mask: np.ndarray) -> np.ndarray:
        """Run the model on a padded RGB crop (uint8, H and W multiples of 8). Only entry point.

        Any failure (load error, inference error, dead or slow worker) is a `LamaError`.
        """
        try:
            return self._runner.predict(image, mask)
        except LamaError:
            raise
        except Exception as error:
            raise LamaError(f"{type(error).__name__}: {error}") from error

    def inpaint(self, img_rgb: np.ndarray, mask: np.ndarray) -> np.ndarray:
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
        res = self.predict(_pad_to_multiple(work), _pad_to_multiple(work_mask))[:hh, :ww]
        if factor < 1.0:
            res = imgops.resize(res, (cw, ch), "cubic")
        out = img_rgb.copy()
        region = out[y0:y1, x0:x1]
        keep = crop_mask > 0
        region[keep] = res[keep]
        return out


_instance: LamaInpainter | None = None
_instance_key: tuple[object, ...] | None = None
_instance_lock = threading.Lock()


def torch_available() -> bool:
    return importlib.util.find_spec("torch") is not None


def _build_inpainter(model_path: Path) -> LamaInpainter:
    """LaMa with the limits from the settings: in a worker process (default) or in this one."""
    settings = get_watermark_settings()
    threads = settings.watermark_torch_threads
    idle = settings.watermark_lama_idle_unload_seconds
    runner: Runner
    if settings.watermark_lama_isolation == "process":
        from src.watermark.lama_proc import WorkerRunner, shared_worker

        runner = WorkerRunner(
            shared_worker(
                model_path,
                threads=threads,
                timeout=float(settings.watermark_lama_timeout_seconds),
                idle_seconds=float(idle),
            )
        )
    else:
        runner = ModelRunner(model_path, threads=threads, auto_unload_seconds=idle)
    return LamaInpainter(
        model_path,
        max_side=settings.watermark_lama_max_side,
        max_pixels=settings.watermark_lama_max_pixels,
        min_margin=settings.watermark_lama_min_margin,
        runner=runner,
    )


def get_lama_inpainter() -> LamaInpainter | None:
    """LaMa singleton, or None (disabled by flag / no torch / no model file).

    With the default `process` isolation the model never enters this process: `torch` is only
    imported by the worker, and `import torch` costs ~220 MiB the bot would keep for good.
    """
    global _instance, _instance_key
    settings = get_watermark_settings()
    if not settings.watermark_lama_enabled:
        return None
    model_path = Path(settings.watermark_lama_model_path)
    if not model_path.is_file() or not torch_available():
        return None
    key = (
        model_path, settings.watermark_lama_isolation, settings.watermark_torch_threads,
        settings.watermark_lama_idle_unload_seconds, settings.watermark_lama_timeout_seconds,
        settings.watermark_lama_max_side, settings.watermark_lama_max_pixels,
        settings.watermark_lama_min_margin,
    )
    with _instance_lock:
        if _instance is None or _instance_key != key:
            _close_runner(_instance)
            _instance, _instance_key = _build_inpainter(model_path), key
        return _instance


def _close_runner(inpainter: LamaInpainter | None) -> None:
    close = getattr(inpainter._runner, "close", None) if inpainter is not None else None
    if close is not None:
        close()


def reset_lama_inpainter() -> None:
    """Forget the singleton and stop its model / worker (tests, configuration reload)."""
    global _instance, _instance_key
    from src.watermark import lama_proc

    with _instance_lock:
        old, _instance, _instance_key = _instance, None, None
    _close_runner(old)
    lama_proc.close_all_workers()


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
