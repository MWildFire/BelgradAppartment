"""LaMa worker process: protocol, one request at a time, timeout, crash, idle shutdown.

The worker is a fake (`serve()` with a small handler), so no torch and no model are needed.
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

from src.watermark import lama_proc
from src.watermark.lama_proc import LamaWorkerClient, LamaWorkerError
from src.watermark.settings import get_watermark_settings

ROOT = Path(__file__).resolve().parents[2]


def fake_command(handler_src: str) -> list[str]:
    """A worker that runs `serve()` with the handler given as a Python expression."""
    code = (
        "import os, sys, time, numpy as np\n"
        "from src.watermark.lama_proc import serve\n"
        f"handler = {handler_src}\n"
        "serve(handler)\n"
    )
    return [sys.executable, "-c", code]


def make_client(handler_src: str, **kwargs) -> LamaWorkerClient:
    kwargs.setdefault("timeout", 20.0)
    kwargs.setdefault("auto_reap", False)  # tests drive reap_if_idle() with a fake clock
    return LamaWorkerClient(fake_command(handler_src), cwd=ROOT, env={"PYTHONPATH": str(ROOT)}, **kwargs)


def image(h: int = 16, w: int = 24, value: int = 7) -> np.ndarray:
    return np.full((h, w, 3), value, np.uint8)


def mask(h: int = 16, w: int = 24) -> np.ndarray:
    m = np.zeros((h, w), np.uint8)
    m[4:8, 4:8] = 1
    return m


PLUS_ONE = "lambda op, img, msk: ((img.astype(np.int16) + 1) % 256).astype(np.uint8)"


@pytest.fixture
def clients():
    made: list[LamaWorkerClient] = []

    def factory(handler_src: str, **kwargs) -> LamaWorkerClient:
        client = make_client(handler_src, **kwargs)
        made.append(client)
        return client

    yield factory
    for client in made:
        client.close()


def test_roundtrip_returns_the_handlers_array(clients):
    client = clients(PLUS_ONE)
    out = client.call("center", image(value=7), mask())
    assert out.dtype == np.uint8 and out.shape == (16, 24, 3) and int(out[0, 0, 0]) == 8


def test_op_and_mask_reach_the_worker(clients):
    client = clients(
        "lambda op, img, msk: np.full(img.shape, 200 if op == 'corner' else 100, np.uint8)"
        " + (msk.sum() % 7).astype(np.uint8)"
    )
    a = client.call("center", image(), mask())
    b = client.call("corner", image(), mask())
    assert int(a[0, 0, 0]) == 100 + 16 % 7 and int(b[0, 0, 0]) == 200 + 16 % 7


def test_worker_is_started_lazily_and_reused(clients):
    client = clients(PLUS_ONE)
    assert not client.alive and client.spawn_count == 0
    client.call("center", image(), mask())
    client.call("center", image(), mask())
    assert client.alive and client.spawn_count == 1


def test_concurrent_callers_get_their_own_answers(clients):
    client = clients("lambda op, img, msk: (time.sleep(0.05), img)[1]")
    results: dict[int, int] = {}

    def run(value: int) -> None:
        results[value] = int(client.call("center", image(value=value), mask())[0, 0, 0])

    threads = [threading.Thread(target=run, args=(v,)) for v in (11, 22, 33, 44)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == {11: 11, 22: 22, 33: 33, 44: 44}


def test_stray_prints_in_the_worker_do_not_break_the_protocol(clients):
    client = clients("lambda op, img, msk: (print('hello from torch'), img)[1]")
    assert int(client.call("center", image(value=5), mask())[0, 0, 0]) == 5


def test_handler_error_is_reported_and_the_worker_stays_usable(clients):
    client = clients(
        "lambda op, img, msk: (_ for _ in ()).throw(ValueError('bad crop')) if op == 'x' else img"
    )
    with pytest.raises(LamaWorkerError, match="bad crop"):
        client.call("x", image(), mask())
    assert client.alive and client.spawn_count == 1
    assert int(client.call("center", image(value=3), mask())[0, 0, 0]) == 3


def test_timeout_kills_the_worker_and_the_next_call_starts_a_new_one(clients):
    client = clients("lambda op, img, msk: (time.sleep(5), img)[1] if op == 'slow' else img", timeout=0.5)
    started = time.monotonic()
    with pytest.raises(LamaWorkerError, match="timeout"):
        client.call("slow", image(), mask())
    assert time.monotonic() - started < 4 and not client.alive
    assert int(client.call("center", image(value=9), mask())[0, 0, 0]) == 9
    assert client.spawn_count == 2


def test_a_worker_that_never_reads_its_stdin_cannot_freeze_the_caller(tmp_path):
    """Review M1: a blocking write into a full pipe ignored the deadline and held the lock."""
    marker = tmp_path / "second-start"
    code = (
        "import os, sys, time, numpy as np\n"
        "from src.watermark.lama_proc import serve\n"
        f"marker = {str(marker)!r}\n"
        "if not os.path.exists(marker):\n"
        "    open(marker, 'w').close()\n"
        "    time.sleep(60)  # alive, but never reads the request\n"
        "serve(lambda op, img, msk: img)\n"
    )
    client = LamaWorkerClient(
        [sys.executable, "-c", code], cwd=ROOT, env={"PYTHONPATH": str(ROOT)},
        timeout=2.0, auto_reap=False,
    )
    try:
        big = np.full((200, 200, 3), 5, np.uint8)  # image + mask = 160 KB > the 64 KiB pipe buffer
        started = time.monotonic()
        with pytest.raises(LamaWorkerError, match="timeout"):
            client.call("center", big, np.ones((200, 200), np.uint8))
        assert time.monotonic() - started < 4 and not client.alive
        out = client.call("center", big, np.ones((200, 200), np.uint8))  # a fresh worker serves
        assert int(out[0, 0, 0]) == 5 and client.spawn_count == 2
    finally:
        client.close()


def test_worker_killed_by_the_oom_killer_does_not_take_the_caller_down(clients):
    client = clients(
        "lambda op, img, msk: os.kill(os.getpid(), 9) if op == 'oom' else img"
    )
    with pytest.raises(LamaWorkerError, match="died"):
        client.call("oom", image(), mask())
    assert not client.alive
    assert int(client.call("center", image(value=4), mask())[0, 0, 0]) == 4
    assert client.spawn_count == 2


def test_worker_that_cannot_start_raises_instead_of_hanging():
    client = LamaWorkerClient(
        [sys.executable, "-c", "raise SystemExit(3)"], cwd=ROOT, timeout=5.0
    )
    with pytest.raises(LamaWorkerError, match=r"died|exit"):
        client.call("center", image(), mask())
    client.close()


def test_idle_worker_is_shut_down_and_restarted_on_demand(clients):
    now = [1000.0]
    client = clients(PLUS_ONE, idle_seconds=600, clock=lambda: now[0])
    client.call("center", image(), mask())
    now[0] += 599
    assert client.reap_if_idle() is False and client.alive
    now[0] += 2
    assert client.reap_if_idle() is True and not client.alive
    assert client.reap_if_idle() is False  # nothing left to stop
    client.call("center", image(value=1), mask())
    assert client.spawn_count == 2


def test_worker_is_never_stopped_in_the_middle_of_a_call(clients):
    now = [0.0]
    client = clients(
        "lambda op, img, msk: (time.sleep(0.6), img)[1]", idle_seconds=10, clock=lambda: now[0]
    )
    client.call("center", image(), mask())
    now[0] += 1000  # way past idle
    result: list[int] = []
    thread = threading.Thread(
        target=lambda: result.append(int(client.call("center", image(value=6), mask())[0, 0, 0]))
    )
    thread.start()
    time.sleep(0.2)  # the call is in flight now
    assert client.reap_if_idle() is False
    thread.join()
    assert result == [6]


def test_close_terminates_the_worker(clients):
    client = clients(PLUS_ONE)
    client.call("center", image(), mask())
    client.close()
    assert not client.alive


def test_the_calling_process_never_imports_torch():
    code = "import sys, src.watermark.lama_proc; print('torch' in sys.modules)"
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env={"PYTHONPATH": str(ROOT)},
        capture_output=True, text=True, check=True,
    )
    assert out.stdout.strip() == "False"


MODEL = Path(get_watermark_settings().watermark_lama_model_path)
HAS_LAMA = importlib.util.find_spec("torch") is not None and MODEL.is_file()


@pytest.mark.skipif(not HAS_LAMA, reason="torch or the LaMa model file is unavailable")
def test_real_worker_gives_the_same_pixels_as_the_in_process_model():
    from src.watermark.lama_model import ModelRunner

    rng = np.random.default_rng(3)
    img = rng.integers(0, 256, (64, 96, 3), dtype=np.uint8)
    msk = np.zeros((64, 96), np.uint8)
    msk[20:36, 30:60] = 1
    client = LamaWorkerClient(
        lama_proc.build_worker_command(MODEL, 2), cwd=ROOT, env={"PYTHONPATH": str(ROOT)},
        timeout=120.0, auto_reap=False,
    )
    try:
        remote = client.call("predict", img, msk)
    finally:
        client.close()
    local = ModelRunner(MODEL, threads=2).predict(img, msk)
    assert remote.shape == local.shape and int(np.abs(remote.astype(int) - local).max()) <= 1


def test_unknown_operation_is_reported_by_the_real_entry_point():
    client = LamaWorkerClient(
        lama_proc.build_worker_command("missing.pt", 1), cwd=ROOT,
        env={"PYTHONPATH": str(ROOT)}, timeout=60.0, auto_reap=False,
    )
    try:
        with pytest.raises(LamaWorkerError, match="unknown LaMa operation"):
            client.call("bogus", image(), mask())
    finally:
        client.close()


def test_frame_codec_roundtrip():
    import io

    buf = io.BytesIO()
    lama_proc.write_request(buf, "corner", image(5, 7, 3), mask(5, 7)[:5, :7])
    buf.seek(0)
    op, img, msk = lama_proc.read_request(buf)
    assert op == "corner" and img.shape == (5, 7, 3) and msk.shape == (5, 7)
