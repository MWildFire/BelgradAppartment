"""A timed-out inference thread must not overwrite newer results or opt-out."""
from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from src.config import get_settings
from src.telegram import bot_delivery
from src.watermark import pipeline, store
from src.watermark.files import atomic_write
from src.watermark.settings import get_watermark_settings


@pytest.fixture
def jobs(tmp_path, monkeypatch):
    inbound = tmp_path / "inbound"
    inbound.mkdir()
    original = inbound / "same.jpg"
    original.write_bytes(b"immutable original payload")
    settings = get_settings()
    wm = get_watermark_settings()
    monkeypatch.setattr(settings, "inbound_photos_dir", inbound)
    monkeypatch.setattr(wm, "watermark_clean_dir", tmp_path / "clean")
    monkeypatch.setattr(wm, "watermark_halo_corner_mode", "skip")
    monkeypatch.setattr(bot_delivery, "DELIVERY_CLEAN_TIMEOUT_SECONDS", .05)
    actual = store.ensure_clean
    guard = threading.Lock()
    local = threading.local()
    calls = []
    errors = []
    finished = {tag: threading.Event() for tag in ("A", "B")}

    def tracked(*args, **kwargs):
        with guard:
            local.tag = "A" if not calls else "B"
            calls.append(local.tag)
        try:
            return actual(*args, **kwargs)
        except RuntimeError as exc:
            errors.append(str(exc))
            raise
        finally:
            finished[local.tag].set()

    monkeypatch.setattr(store, "ensure_clean", tracked)
    return original, tmp_path / "clean/same.jpg", local, errors, finished


async def reached(event):
    for _ in range(800):
        if event.is_set():
            return
        await asyncio.sleep(.005)
    raise AssertionError("worker did not complete the expected phase")


def report(tag):
    return SimpleNamespace(changed=True, to_dict=lambda: {"job": tag, "marks": []})


@pytest.mark.asyncio
@pytest.mark.parametrize("intervening", ["second_job", "discard", "replace_original"])
async def test_late_worker_cannot_publish_an_obsolete_generation(jobs, monkeypatch, intervening):
    original, clean, local, errors, finished = jobs
    ready, release = threading.Event(), threading.Event()
    original_bytes = original.read_bytes()
    digest = hashlib.sha256(original_bytes).hexdigest()

    def fake_clean(source, destination, **kwargs):
        assert source != original and source.read_bytes() == original_bytes
        assert destination != clean  # inference writes its private stage only
        tag = local.tag
        atomic_write(destination, f"derived {tag}".encode())
        if tag == "A":
            ready.set()
            assert release.wait(20)
        if tag == "B":
            time.sleep(.12)  # Valid work can exceed the artificial timeout for A.
        return report(tag)

    monkeypatch.setattr(pipeline, "clean_file", fake_clean)
    first = asyncio.create_task(bot_delivery._clean_delivery_paths(701, [original]))
    second_started = False
    try:
        await reached(ready)
        assert await first == [original]
        assert not finished["A"].is_set() and not clean.exists()
        # Only A needs the artificial short timeout. B tests publication ordering,
        # not a 50 ms disk/executor latency budget under a loaded full test suite.
        monkeypatch.setattr(bot_delivery, "DELIVERY_CLEAN_TIMEOUT_SECONDS", 5)
        if intervening == "second_job":
            second_started = True
            assert await bot_delivery._clean_delivery_paths(702, [original]) == [clean]
            assert clean.read_bytes() == b"derived B"
        elif intervening == "discard":
            assert store.discard(original)
            second_started = True
            assert await bot_delivery._clean_delivery_paths(702, [original]) == [original]
        else:
            original.write_bytes(b"new original from external source")
    finally:
        release.set()
        # Join both real worker lifetimes even when an assertion fails. Restoring
        # monkeypatch settings while a worker still runs can contaminate later tests.
        await reached(finished["A"])
        if second_started:
            await reached(finished["B"])
    assert errors == ["watermark result superseded by a newer request or source"]
    if intervening == "second_job":
        assert clean.read_bytes() == b"derived B"
        assert store.read_sidecar(clean)["report"]["job"] == "B"
    elif intervening == "discard":
        assert not clean.exists() and store.read_sidecar(clean)["discarded"] is True
    else:
        assert not clean.exists() and store.read_sidecar(clean) is None
    if intervening != "replace_original":
        assert hashlib.sha256(original.read_bytes()).hexdigest() == digest
    assert not list(clean.parent.glob(".*.wm-*"))


def test_failed_pair_publication_falls_back_and_is_retryable(tmp_path, monkeypatch):
    inbound = tmp_path / "inbound"
    inbound.mkdir()
    original = inbound / "same.jpg"
    original.write_bytes(b"original")
    settings = get_settings()
    wm = get_watermark_settings()
    monkeypatch.setattr(settings, "inbound_photos_dir", inbound)
    monkeypatch.setattr(wm, "watermark_clean_dir", tmp_path / "clean")
    monkeypatch.setattr(wm, "watermark_halo_corner_mode", "skip")

    def fake_clean(source, destination, **kwargs):
        destination.write_bytes(b"complete staged derivative")
        return report("new")

    monkeypatch.setattr(pipeline, "clean_file", fake_clean)
    actual = store._write_sidecar

    def fail_final(clean, payload):
        if not payload.get("publishing"):
            raise OSError("synthetic sidecar failure")
        actual(clean, payload)

    monkeypatch.setattr(store, "_write_sidecar", fail_final)
    with pytest.raises(OSError, match="synthetic"):
        store.ensure_clean(str(original))
    assert store.clean_or_original(original) == original
    assert not store.is_current(original)
    assert original.read_bytes() == b"original"
    monkeypatch.setattr(store, "_write_sidecar", actual)
    result = store.ensure_clean(str(original))
    assert result is not None and not result.reused and result.clean is not None
    assert store.read_sidecar(result.clean)["report"]["job"] == "new"
    assert store.is_current(original)


def test_generation_lock_symlink_is_rejected_before_external_mutation(tmp_path, monkeypatch):
    inbound, clean = tmp_path / "inbound", tmp_path / "clean"
    inbound.mkdir()
    clean.mkdir()
    original = inbound / "same.jpg"
    original.write_bytes(b"original")
    settings = get_settings()
    wm = get_watermark_settings()
    monkeypatch.setattr(settings, "inbound_photos_dir", inbound)
    monkeypatch.setattr(wm, "watermark_clean_dir", clean)
    target = tmp_path / "unrelated.txt"
    target.write_bytes(b"must remain intact")
    (clean / "same.jpg.wm.lock").symlink_to(target)
    for action in (lambda: store.ensure_clean(str(original)), lambda: store.discard(original)):
        with pytest.raises(RuntimeError, match="unsafe"):
            action()
    assert target.read_bytes() == b"must remain intact"
    assert original.read_bytes() == b"original"


@pytest.mark.parametrize("intervening", ["second_job", "discard"])
def test_generation_is_shared_with_an_independent_process(tmp_path, monkeypatch, intervening):
    """A second service process must invalidate an already-running generation too."""
    inbound, clean_root = tmp_path / "inbound", tmp_path / "clean"
    inbound.mkdir()
    original = inbound / "same.jpg"
    original.write_bytes(b"unchanged source")
    settings = get_settings()
    wm = get_watermark_settings()
    monkeypatch.setattr(settings, "inbound_photos_dir", inbound)
    monkeypatch.setattr(wm, "watermark_clean_dir", clean_root)
    monkeypatch.setattr(wm, "watermark_halo_corner_mode", "skip")
    ready, release = tmp_path / "ready", tmp_path / "release"
    worker = r'''
import sys, time
from pathlib import Path
from types import SimpleNamespace
from src.config import get_settings
from src.watermark.settings import get_watermark_settings
from src.watermark import pipeline, store
root=Path(sys.argv[1]); settings=get_settings(); wm=get_watermark_settings()
settings.inbound_photos_dir=root/'inbound'
wm.watermark_clean_dir=root/'clean'
wm.watermark_halo_corner_mode='skip'
def clean(source, destination, **kwargs):
    destination.write_bytes(b'derived A')
    (root/'ready').touch()
    deadline=time.monotonic()+15
    while not (root/'release').exists():
        if time.monotonic()>deadline:raise RuntimeError('fixture release timeout')
        time.sleep(.01)
    return SimpleNamespace(changed=True, to_dict=lambda:{'job':'A','marks':[]})
pipeline.clean_file=clean
try:
    store.ensure_clean(str(root/'inbound/same.jpg'))
except RuntimeError as exc:
    (root/'worker-result').write_text(str(exc))
else:
    (root/'worker-result').write_text('unexpected publication')
'''
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.Popen([sys.executable, "-B", "-c", worker, str(tmp_path)], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and proc.poll() is None and time.monotonic() < deadline:
            time.sleep(.01)
        assert ready.exists(), "independent worker failed to reach its inference barrier"

        def fake_clean(source, destination, **kwargs):
            destination.write_bytes(b"derived B")
            return report("B")

        monkeypatch.setattr(pipeline, "clean_file", fake_clean)
        if intervening == "second_job":
            assert store.ensure_clean(str(original)).clean is not None
        else:
            assert store.discard(original)
    finally:
        release.touch()
        try:
            stdout, stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate(timeout=5)
            raise
    assert proc.returncode == 0, (stdout, stderr)
    assert (tmp_path / "worker-result").read_text() == (
        "watermark result superseded by a newer request or source"
    )
    clean = clean_root / "same.jpg"
    if intervening == "second_job":
        assert clean.read_bytes() == b"derived B"
        assert store.read_sidecar(clean)["report"]["job"] == "B"
    else:
        assert not clean.exists() and store.read_sidecar(clean)["discarded"] is True
    assert original.read_bytes() == b"unchanged source"
    assert not list(clean_root.glob(".*.wm-*"))
