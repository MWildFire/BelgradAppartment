"""LaMa runs in a worker process: wiring, limits, and what happens when the worker fails.

Fake workers (`serve()` with a tiny handler) stand in for big-lama where the model itself is not
the point; the real model is used for the "torch never enters the bot process" checks.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from src.watermark import lama, lama_proc
from src.watermark.pipeline import clean_array
from src.watermark.settings import get_watermark_settings
from src.watermark.templates import load_templates
from tests.watermark.synth import apply_mark, jpeg_roundtrip, make_background

ROOT = Path(__file__).resolve().parents[2]
MODEL = Path(get_watermark_settings().watermark_lama_model_path)
HAS_LAMA = importlib.util.find_spec("torch") is not None and MODEL.is_file()


def fake_worker_command(handler_src: str):
    code = (
        "import os, sys, time, numpy as np\n"
        "from src.watermark.lama_proc import serve\n"
        f"serve({handler_src})\n"
    )

    def build(_model_path, _threads):
        return [sys.executable, "-c", code]

    return build


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    """Process isolation on, a dummy model file present, no worker or singleton left behind."""
    model = tmp_path / "big-lama.pt"
    model.write_bytes(b"not a real model: the worker is a fake")
    settings = get_watermark_settings()
    monkeypatch.setattr(settings, "watermark_lama_enabled", True)
    monkeypatch.setattr(settings, "watermark_lama_model_path", model)
    monkeypatch.setattr(settings, "watermark_lama_isolation", "process")
    monkeypatch.setattr(settings, "watermark_lama_timeout_seconds", 20)
    monkeypatch.setattr(lama, "torch_available", lambda: True)
    lama.reset_lama_inpainter()
    yield settings
    lama.reset_lama_inpainter()


def zida_image(seed: int = 6) -> np.ndarray:
    tpl = {t.id: t for t in load_templates()}["zida_logo"]
    return jpeg_roundtrip(apply_mark(make_background(1440, 1080, seed=seed), tpl))


def halo_center_image(seed: int = 2) -> np.ndarray:
    tpl = {t.id: t for t in load_templates()}["halo_center"]
    return jpeg_roundtrip(apply_mark(make_background(1000, 750, seed=seed), tpl))


# ---- settings ----------------------------------------------------------------------------------


def test_limits_have_safe_defaults_in_the_watermark_settings():
    s = type(get_watermark_settings())()
    assert (s.watermark_lama_max_side, s.watermark_lama_max_pixels) == (1024, 75_000)
    assert (s.watermark_lama_min_margin, s.watermark_torch_threads) == (16, 2)
    assert s.watermark_lama_idle_unload_seconds == 600
    assert s.watermark_lama_isolation == "process" and s.watermark_lama_timeout_seconds == 180


# ---- wiring ------------------------------------------------------------------------------------


def test_factory_returns_an_inpainter_backed_by_the_worker(isolated, monkeypatch):
    monkeypatch.setattr(lama_proc, "build_worker_command", fake_worker_command("lambda o, i, m: i"))
    inpainter = lama.get_lama_inpainter()
    assert inpainter is not None and isinstance(inpainter._runner, lama_proc.WorkerRunner)
    assert inpainter.max_pixels == 75_000 and inpainter.max_side == 1024


def test_factory_passes_the_configured_limits(isolated, monkeypatch):
    monkeypatch.setattr(isolated, "watermark_lama_max_side", 640)
    monkeypatch.setattr(isolated, "watermark_lama_max_pixels", 70_000)
    monkeypatch.setattr(isolated, "watermark_lama_min_margin", 24)
    monkeypatch.setattr(lama_proc, "build_worker_command", fake_worker_command("lambda o, i, m: i"))
    inpainter = lama.get_lama_inpainter()
    assert (inpainter.max_side, inpainter.max_pixels, inpainter.min_margin) == (640, 70_000, 24)


def test_a_changed_setting_builds_a_new_inpainter(isolated, monkeypatch):
    monkeypatch.setattr(lama_proc, "build_worker_command", fake_worker_command("lambda o, i, m: i"))
    first = lama.get_lama_inpainter()
    monkeypatch.setattr(isolated, "watermark_lama_max_pixels", 50_000)
    second = lama.get_lama_inpainter()
    assert second is not first and second.max_pixels == 50_000


def test_inline_mode_uses_the_in_process_runner_with_the_idle_unload(isolated, monkeypatch):
    from src.watermark.lama_model import ModelRunner

    monkeypatch.setattr(isolated, "watermark_lama_isolation", "inline")
    monkeypatch.setattr(isolated, "watermark_torch_threads", 1)
    monkeypatch.setattr(isolated, "watermark_lama_idle_unload_seconds", 123)
    inpainter = lama.get_lama_inpainter()
    runner = inpainter._runner
    assert isinstance(runner, ModelRunner) and runner.threads == 1
    assert runner._auto_unload_seconds == 123 and not runner.loaded  # lazy


def test_center_and_corner_marks_reach_the_worker_and_only_masked_pixels_change(
    isolated, monkeypatch
):
    monkeypatch.setattr(
        lama_proc, "build_worker_command",
        fake_worker_command("lambda o, i, m: np.full(i.shape, (255, 0, 0), np.uint8)"),
    )
    img = halo_center_image()
    out, report = clean_array(img)
    assert [m.method for m in report.marks] == ["inpaint:lama"]
    changed = np.any(out != img, axis=2)
    assert changed.any() and np.all(out[changed] == [255, 0, 0])
    # the corner adapter goes through the very same worker
    from src.watermark.lama_corner import get_lama_corner_inpainter

    corner = get_lama_corner_inpainter()
    mask = np.zeros(img.shape[:2], np.uint8)
    mask[700:716, 800:900] = 1
    corner_out = corner.inpaint(img, mask)
    assert np.any(np.all(corner_out == [255, 0, 0], axis=2))
    assert lama_proc.shared_worker_count() == 1  # one worker for both paths


# ---- failures keep the mark -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("handler", "label"),
    [
        ("lambda o, i, m: os.kill(os.getpid(), 9)", "oom-kill"),
        ("lambda o, i, m: (_ for _ in ()).throw(RuntimeError('model exploded'))", "error"),
    ],
)
def test_worker_failure_keeps_the_mark_and_the_pixels(isolated, monkeypatch, handler, label):
    monkeypatch.setattr(lama_proc, "build_worker_command", fake_worker_command(handler))
    img = halo_center_image()
    out, report = clean_array(img)
    assert [(m.method, m.kept_reason) for m in report.marks] == [("skip", "lama_unavailable")]
    assert not report.changed and np.array_equal(out, img)


def test_worker_timeout_keeps_the_mark_and_the_next_photo_still_works(isolated, monkeypatch):
    monkeypatch.setattr(isolated, "watermark_lama_timeout_seconds", 1)
    monkeypatch.setattr(
        lama_proc, "build_worker_command",
        fake_worker_command("lambda o, i, m: (time.sleep(30), i)[1]"),
    )
    img = halo_center_image()
    out, report = clean_array(img)
    assert [(m.method, m.kept_reason) for m in report.marks] == [("skip", "lama_unavailable")]
    assert np.array_equal(out, img)
    lama_proc.close_all_workers()
    monkeypatch.setattr(
        lama_proc, "build_worker_command",
        fake_worker_command("lambda o, i, m: np.full(i.shape, (9, 9, 9), np.uint8)"),
    )
    lama.reset_lama_inpainter()
    _out, report = clean_array(halo_center_image(seed=3))
    assert [m.method for m in report.marks] == ["inpaint:lama"]


def test_worker_failure_in_a_mixed_photo_loses_only_that_mark(isolated, monkeypatch):
    """4zida marks fall back to reverse blend when LaMa is missing, but a runtime failure keeps."""
    monkeypatch.setattr(
        lama_proc, "build_worker_command",
        fake_worker_command("lambda o, i, m: os.kill(os.getpid(), 9)"),
    )
    _out, report = clean_array(zida_image())
    assert {m.method for m in report.marks} == {"skip"}
    assert {m.kept_reason for m in report.marks} == {"lama_unavailable"}


# ---- torch never enters the bot process --------------------------------------------------------

BOT_SCRIPT = """
import json, sys
sys.path.insert(0, {root!r})
from tests.watermark.synth import apply_mark, jpeg_roundtrip, make_background
from src.watermark.pipeline import clean_array
from src.watermark.settings import get_watermark_settings
from src.watermark.templates import load_templates
from src.watermark import lama_proc

tpls = {{t.id: t for t in load_templates()}}
out = {{}}
for name, size, seed in (({kind!r}, ({w}, {h}), 6),):
    img = jpeg_roundtrip(apply_mark(make_background(size[0], size[1], seed=seed), tpls[name]))
    _o, report = clean_array(img)
    out["methods"] = [m.method for m in report.marks]
out["torch"] = "torch" in sys.modules
out["workers"] = lama_proc.shared_worker_count()
print(json.dumps(out))
"""


def run_bot_process(kind: str, w: int, h: int, **env: str) -> dict:
    code = BOT_SCRIPT.format(root=str(ROOT), kind=kind, w=w, h=h)
    done = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=300,
        env={
            "PYTHONPATH": str(ROOT), "PATH": "/usr/bin:/bin", "WATERMARK_LAMA_MODEL_PATH": str(MODEL),
            "WATERMARK_LAMA_ISOLATION": "process", **env,
        },
    )
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(not HAS_LAMA, reason="torch or the LaMa model file is unavailable")
def test_4zida_with_the_real_model_never_imports_torch_in_the_bot_process():
    result = run_bot_process("zida_logo", 1440, 1080)
    assert result["methods"] == ["inpaint:lama", "inpaint:lama"]
    assert result["torch"] is False and result["workers"] == 1


@pytest.mark.skipif(not HAS_LAMA, reason="torch or the LaMa model file is unavailable")
def test_halo_corner_with_the_real_model_goes_through_the_worker_too():
    result = run_bot_process("halo_corner", 1000, 750, WATERMARK_HALO_CORNER_MODE="lama")
    assert result["methods"] == ["inpaint:lama"]
    assert result["torch"] is False and result["workers"] == 1


@pytest.mark.skipif(not HAS_LAMA, reason="torch or the LaMa model file is unavailable")
def test_a_photo_without_lama_marks_neither_imports_torch_nor_starts_a_worker():
    result = run_bot_process("cityexpert_center", 1920, 1078)
    assert result["methods"] == ["reverse_blend"]
    assert result["torch"] is False and result["workers"] == 0
