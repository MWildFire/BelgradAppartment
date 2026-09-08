"""The OS cancellation path must reach durable checkpoint/finally handlers."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from src.jobs.lifecycle import remaining_budget, run_budget


def test_sigterm_runs_finally_and_returns_failure(tmp_path):
    script = """
import asyncio
from pathlib import Path
from src.jobs.lifecycle import run_cooperatively
async def job():
    Path('ready').write_text('ready')
    try:
        await asyncio.sleep(60)
    finally:
        Path('final-status').write_text('cancelled')
run_cooperatively(job())
"""
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    child = subprocess.Popen([sys.executable, "-c", script], cwd=tmp_path, env=env)
    try:
        deadline = time.monotonic() + 5
        while not (tmp_path / "ready").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert (tmp_path / "ready").exists()
        child.send_signal(signal.SIGTERM)
        assert child.wait(timeout=5) == 143
        assert (tmp_path / "final-status").read_text() == "cancelled"
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


def test_total_budget_reserves_cleanup_and_restores_context():
    assert remaining_budget(100) == 100
    with run_budget(10):
        assert 6 < remaining_budget(100, reserve=3) <= 7
        assert remaining_budget(100, reserve=20) == 0
    assert remaining_budget(100) == 100
