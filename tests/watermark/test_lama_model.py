"""ModelRunner: lazy load, torch thread limit, one inference at a time, unload after idle.

A fake model and a fake clock stand in for big-lama (no model file needed); torch itself is
required for the tensors, so the module is skipped where torch is not installed.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

pytestmark = pytest.mark.skipif(importlib.util.find_spec("torch") is None, reason="torch unavailable")

ROOT = Path(__file__).resolve().parents[2]


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def red_model(image, mask):
    import torch

    out = torch.zeros_like(image)
    out[:, 0] = 1.0
    return out


def make_runner(model=red_model, **kw):
    from src.watermark.lama_model import ModelRunner

    loads: list[Path] = []

    def loader(path):
        loads.append(path)
        return model

    runner = ModelRunner(Path("unused.pt"), loader=loader, auto_unload_seconds=0, **kw)
    runner.loads = loads  # type: ignore[attr-defined]
    return runner


def rgb(h: int = 16, w: int = 24) -> np.ndarray:
    return np.full((h, w, 3), (255, 128, 0), np.uint8)


def hole(h: int = 16, w: int = 24) -> np.ndarray:
    m = np.zeros((h, w), np.uint8)
    m[4:8, 4:8] = 1
    return m


def test_model_gets_rgb_unit_range_float_binary_mask_and_returns_uint8_rgb():
    import torch

    seen = []

    def model(image, mask):
        seen.append((image.dtype, tuple(image.shape), mask.dtype, tuple(mask.shape)))
        assert torch.allclose(image[0, :, 0, 0], torch.tensor([1.0, 0.5, 0.0]), atol=0.003)
        assert set(mask.unique().tolist()) <= {0.0, 1.0}
        assert not torch.is_grad_enabled()
        return red_model(image, mask)

    runner = make_runner(model)
    out = runner.predict(rgb(), hole() * 255)  # any non-zero mask value means "fill"
    assert seen == [(torch.float32, (1, 3, 16, 24), torch.float32, (1, 1, 16, 24))]
    assert out.dtype == np.uint8 and out.shape == (16, 24, 3) and np.all(out == [255, 0, 0])


def test_importing_and_constructing_the_runner_does_not_import_torch():
    code = (
        "import sys\n"
        "from pathlib import Path\n"
        "from src.watermark.lama_model import ModelRunner\n"
        "ModelRunner(Path('x.pt'))\n"
        "print('torch' in sys.modules)\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env={"PYTHONPATH": str(ROOT)},
        capture_output=True, text=True, check=True,
    )
    assert out.stdout.strip() == "False"


def test_model_is_loaded_once_and_only_on_first_use():
    runner = make_runner()
    assert runner.loads == [] and not runner.loaded  # type: ignore[attr-defined]
    runner.predict(rgb(), hole())
    runner.predict(rgb(), hole())
    assert len(runner.loads) == 1 and runner.loaded  # type: ignore[attr-defined]


def test_page_cache_of_the_model_file_is_dropped_right_after_loading(monkeypatch, tmp_path):
    """200 MiB of clean file cache otherwise stays charged to the unit's cgroup for good."""
    from src.watermark import lama_model

    model_file = tmp_path / "big-lama.pt"
    model_file.write_bytes(b"x" * 4096)
    advised: list[tuple[int, int, int, int]] = []

    def fake_fadvise(fd, offset, length, advice):
        advised.append((offset, length, advice, len(loads)))

    loads: list[Path] = []

    def loader(path):
        loads.append(path)
        return red_model

    monkeypatch.setattr(lama_model.os, "posix_fadvise", fake_fadvise, raising=False)
    monkeypatch.setattr(lama_model.os, "POSIX_FADV_DONTNEED", 4, raising=False)
    runner = lama_model.ModelRunner(model_file, loader=loader)
    runner.predict(rgb(), hole())
    runner.predict(rgb(), hole())
    assert advised == [(0, 0, 4, 1)]  # once, after the load


def test_a_missing_model_file_does_not_break_the_cache_drop(tmp_path):
    from src.watermark import lama_model

    lama_model.drop_file_cache(tmp_path / "nope.pt")  # no exception


def test_torch_threads_are_limited_when_the_model_is_loaded():
    import torch

    before = torch.get_num_threads()
    try:
        runner = make_runner(threads=1)
        runner.predict(rgb(), hole())
        assert torch.get_num_threads() == 1
    finally:
        torch.set_num_threads(before)


def test_only_one_inference_runs_at_a_time():
    active = [0]
    peak = [0]
    guard = threading.Lock()

    def slow_model(image, mask):
        with guard:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.05)
        with guard:
            active[0] -= 1
        return red_model(image, mask)

    runner = make_runner(slow_model)
    threads = [threading.Thread(target=runner.predict, args=(rgb(), hole())) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak[0] == 1


def test_a_failing_inference_does_not_leave_the_runner_locked():
    calls = [0]

    def flaky(image, mask):
        calls[0] += 1
        if calls[0] == 1:
            raise RuntimeError("boom")
        return red_model(image, mask)

    runner = make_runner(flaky)
    with pytest.raises(RuntimeError, match="boom"):
        runner.predict(rgb(), hole())
    assert np.all(runner.predict(rgb(), hole()) == [255, 0, 0])


def test_idle_model_is_unloaded_memory_released_and_reloaded_on_demand(monkeypatch):
    from src.watermark import lama_model

    released = []
    monkeypatch.setattr(lama_model, "release_memory", lambda: released.append(True))
    clock = Clock()
    runner = make_runner(clock=clock)
    runner.predict(rgb(), hole())
    clock.now += 599
    assert runner.unload_if_idle(600) is False and runner.loaded
    clock.now += 2
    assert runner.unload_if_idle(600) is True and not runner.loaded
    assert released == [True]
    assert runner.unload_if_idle(600) is False  # nothing left to unload
    runner.predict(rgb(), hole())
    assert len(runner.loads) == 2 and runner.loaded  # type: ignore[attr-defined]


def test_activity_postpones_the_unload():
    clock = Clock()
    runner = make_runner(clock=clock)
    runner.predict(rgb(), hole())
    clock.now += 500
    runner.predict(rgb(), hole())  # used again: the idle period starts over
    clock.now += 500
    assert runner.unload_if_idle(600) is False and runner.loaded


def test_the_model_is_never_unloaded_in_the_middle_of_an_inference():
    started, release = threading.Event(), threading.Event()

    def blocking(image, mask):
        started.set()
        assert release.wait(5)
        return red_model(image, mask)

    clock = Clock()
    runner = make_runner(blocking, clock=clock)
    result: list[np.ndarray] = []
    worker = threading.Thread(target=lambda: result.append(runner.predict(rgb(), hole())))
    worker.start()
    assert started.wait(5)
    clock.now += 10_000  # far beyond any idle period
    assert runner.unload_if_idle(600) is False and runner.loaded
    release.set()
    worker.join()
    assert len(result) == 1 and np.all(result[0] == [255, 0, 0])


def test_unload_now_drops_the_model():
    runner = make_runner()
    runner.predict(rgb(), hole())
    runner.unload()
    assert not runner.loaded
