"""The torch part of LaMa: lazy load, thread limit, one inference at a time, unload after idle.

This module is all a LaMa worker process needs (numpy + torch, no scipy/pipeline), and it is also
what an in-process LaMa uses. `import torch` happens only on the first prediction.

Unloading inside a process is best effort: measured on Linux (WM05), `del model; gc.collect();
malloc_trim(0)` gives back only ~60 MiB of ~1.3 GiB. The bot therefore runs this in a worker
process that exits when idle (src/watermark/lama_proc.py); the unload here serves the in-process
mode and tests.
"""

from __future__ import annotations

import ctypes
import gc
import logging
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

Loader = Callable[[Path], Any]


def release_memory() -> None:
    """Return freed memory to the OS as far as the allocator allows (Linux/glibc)."""
    gc.collect()
    if sys.platform.startswith("linux"):
        try:
            ctypes.CDLL("libc.so.6").malloc_trim(0)
        except OSError:
            logger.debug("[watermark] malloc_trim unavailable")


def _load_torchscript(path: Path) -> Any:
    import torch

    model = torch.jit.load(str(path), map_location="cpu")
    model.eval()
    return model


class ModelRunner:
    """big-lama on CPU. `predict` takes the same arrays the model sees, in uint8."""

    def __init__(
        self,
        model_path: Path,
        *,
        threads: int = 2,
        clock: Callable[[], float] = time.monotonic,
        loader: Loader | None = None,
        auto_unload_seconds: float = 0,
    ) -> None:
        self.model_path = model_path
        self.threads = threads
        self._clock = clock
        self._loader = loader or _load_torchscript
        self._auto_unload_seconds = auto_unload_seconds
        self._model: Any = None
        self._lock = threading.Lock()  # one inference at a time in this process
        self._last_used = clock()
        self._stop = threading.Event()
        self._watcher: threading.Thread | None = None

    @property
    def loaded(self) -> bool:
        return self._model is not None

    # -- loading (the lock is held by the caller) --

    def _ensure_loaded(self) -> Any:
        if self._model is None:
            import torch

            torch.set_num_threads(self.threads)
            self._model = self._loader(self.model_path)
            logger.info("[watermark] LaMa model loaded from %s", self.model_path)
            self._start_watcher()
        return self._model

    def _start_watcher(self) -> None:
        if self._auto_unload_seconds <= 0 or (self._watcher and self._watcher.is_alive()):
            return
        self._watcher = threading.Thread(target=self._watch, name="lama-unload", daemon=True)
        self._watcher.start()

    def _watch(self) -> None:
        tick = min(30.0, max(1.0, self._auto_unload_seconds / 10))
        while not self._stop.wait(tick):
            if self.unload_if_idle(self._auto_unload_seconds):
                return

    # -- inference --

    def predict(self, image: np.ndarray, mask: np.ndarray) -> np.ndarray:
        """uint8 RGB (H, W, 3) and a mask (H, W; non-zero = fill), H and W multiples of 8."""
        import torch

        image_t = torch.from_numpy(np.ascontiguousarray(image)).permute(2, 0, 1).unsqueeze(0)
        image_t = image_t.float() / 255.0
        mask_t = torch.from_numpy((np.asarray(mask) > 0).astype(np.float32))
        mask_t = mask_t.unsqueeze(0).unsqueeze(0)
        with self._lock:
            model = self._ensure_loaded()
            with torch.inference_mode():
                result = model(image_t, mask_t)
            self._last_used = self._clock()
        out = result[0].permute(1, 2, 0).numpy() * 255.0
        return out.clip(0, 255).astype(np.uint8)

    # -- unloading --

    def unload_if_idle(self, idle_seconds: float) -> bool:
        """Drop the model when unused for `idle_seconds`. Never during an inference."""
        if not self._lock.acquire(blocking=False):
            return False
        try:
            if self._model is None or self._clock() - self._last_used < idle_seconds:
                return False
            self._model = None
        finally:
            self._lock.release()
        release_memory()
        logger.info("[watermark] LaMa model unloaded after %.0f s idle", idle_seconds)
        return True

    def unload(self) -> None:
        """Drop the model now (waits for a running inference)."""
        with self._lock:
            self._model = None
        release_memory()

    def close(self) -> None:
        self._stop.set()
        self.unload()
