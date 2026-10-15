"""Audit regressions: derived-path boundaries, concurrent writes, failed previews."""
from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import numpy as np
import pytest

from src.config import get_settings
from src.watermark import pipeline, published, store


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    inbound, clean = tmp_path / "inbound", tmp_path / "clean"
    inbound.mkdir()
    clean.mkdir()
    monkeypatch.setattr(get_settings(), "inbound_photos_dir", inbound)
    monkeypatch.setattr(get_settings(), "watermark_clean_dir", clean)
    monkeypatch.setattr(get_settings(), "site_photos_root", inbound)
    return inbound, clean


@pytest.mark.parametrize("operation", ["discard", "ensure", "display", "current"])
def test_clean_subdirectory_symlink_cannot_reach_original(dirs, operation):
    inbound, clean = dirs
    src = inbound / "7" / "00.jpg"
    src.parent.mkdir()
    src.write_bytes(b"original must survive")
    (clean / "7").symlink_to(src.parent, target_is_directory=True)
    if operation in ("discard", "ensure"):
        with pytest.raises(RuntimeError, match=r"outside|unsafe"):
            if operation == "discard":
                store.discard(src)
            else:
                store.ensure_clean(str(src))
    elif operation == "display":
        assert store.clean_or_original(src) == src
    else:
        assert store.is_current(src)
    assert src.read_bytes() == b"original must survive"
    assert sorted(p.name for p in src.parent.iterdir()) == ["00.jpg"]


def test_display_does_not_read_a_foreign_copy_via_symlink(dirs, tmp_path):
    inbound, clean = dirs
    src = inbound / "7" / "00.jpg"
    src.parent.mkdir()
    src.write_bytes(b"public original")
    private = tmp_path / "private"
    private.mkdir()
    dst = private / "00.jpg"
    dst.write_bytes(b"private photo")
    (clean / "7").symlink_to(private, target_is_directory=True)
    meta = {"version": store.STORE_VERSION, "changed": True,
            "halo_corner_mode": getattr(get_settings(), "watermark_halo_corner_mode", "telea")}
    (private / "00.jpg.wm.json").write_text(json.dumps(meta))
    assert store.clean_or_original(src) == src


@pytest.mark.parametrize("writer", ["sidecar", "photo"])
def test_concurrent_writes_use_independent_temporary_files(dirs, monkeypatch, writer):
    inbound, clean = dirs
    target = clean / "00.jpg"
    barrier = Barrier(2)
    original_replace = os.replace
    temporary_paths = []

    def replace(src, dst):
        temporary_paths.append(Path(src))
        barrier.wait(timeout=15)
        original_replace(src, dst)

    monkeypatch.setattr(os, "replace", replace)
    if writer == "sidecar":
        def write(n):
            store._write_sidecar(target, {"n": n})
    else:
        src = inbound / "00.jpg"
        src.write_bytes(b"test source")
        image = np.full((20, 20, 3), 100, dtype=np.uint8)
        report = pipeline.CleanReport(20, 20, [pipeline.MarkResult(
            "zida_logo", "4zida", 0, 1, 0, 0, 1, "inpaint:telea", 0)])
        monkeypatch.setattr(pipeline, "decode_image", lambda _: image)
        monkeypatch.setattr(pipeline, "clean_array", lambda _: (image, report))

        def write(n):
            pipeline.clean_file(src, target)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(write, [1, 2]))
    assert len(set(temporary_paths)) == 2
    assert not any(p.exists() for p in temporary_paths)
    if writer == "sidecar":
        assert store.read_sidecar(target)["n"] in (1, 2)
    else:
        assert target.is_file() and src.read_bytes() == b"test source"


@pytest.mark.parametrize("writer", ["sidecar", "photo"])
def test_failed_replace_cleans_up_temporary_file(dirs, monkeypatch, writer):
    inbound, clean = dirs
    target = clean / "00.jpg"
    target.write_bytes(b"old output")

    def fail(*args):
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError, match="replace failed"):
        if writer == "sidecar":
            store._write_sidecar(target, {"n": 1})
        else:
            src = inbound / "00.jpg"
            src.write_bytes(b"original")
            image = np.zeros((20, 20, 3), dtype=np.uint8)
            report = pipeline.CleanReport(20, 20, [pipeline.MarkResult(
                "zida_logo", "4zida", 0, 1, 0, 0, 1, "inpaint:telea", 0)])
            monkeypatch.setattr(pipeline, "decode_image", lambda _: image)
            monkeypatch.setattr(pipeline, "clean_array", lambda _: (image, report))
            pipeline.clean_file(src, target)
    assert target.read_bytes() == b"old output"
    assert sorted(p.name for p in clean.iterdir()) == ["00.jpg"]


def _cached_clean(dirs, monkeypatch):
    inbound, clean = dirs
    src, dst = inbound / "00.jpg", clean / "00.jpg"
    src.write_bytes(b"original")
    dst.write_bytes(b"broken derived image")
    meta = {"version": store.STORE_VERSION, "changed": True,
            "halo_corner_mode": getattr(get_settings(), "watermark_halo_corner_mode", "telea")}
    store.sidecar_for(dst).write_text(json.dumps(meta))
    monkeypatch.setattr(get_settings(), "watermark_site_use_clean", True)
    return src, dst


def test_failed_variant_rebuild_is_retried(dirs, monkeypatch):
    src, dst = _cached_clean(dirs, monkeypatch)
    assert published.needs_site_variants(str(src))
    assert published.rebuild_site_variants([str(src)]) == 0
    assert not store.site_marker_for(dst).exists()
    assert published.needs_site_variants(str(src))


def test_missing_variant_is_retried_even_with_a_marker(dirs, monkeypatch):
    src, dst = _cached_clean(dirs, monkeypatch)
    store.site_marker_for(dst).touch()
    variants = published.site_variant_files(str(src))
    variants[0].write_bytes(b"only one variant")
    assert published.needs_site_variants(str(src))


@pytest.mark.parametrize("operation", ["discard", "ensure", "display", "current"])
def test_sidecar_symlink_is_rejected_before_any_mutation(dirs, tmp_path, operation):
    inbound, clean = dirs
    src, dst = inbound / "00.jpg", clean / "00.jpg"
    src.write_bytes(b"original")
    dst.write_bytes(b"old clean")
    foreign = tmp_path / "foreign.json"
    foreign.write_text(json.dumps({"version": store.STORE_VERSION, "changed": True,
                                  "halo_corner_mode": get_settings().watermark_halo_corner_mode}))
    before = foreign.read_bytes()
    (clean / "00.jpg.wm.json").symlink_to(foreign)
    if operation in ("discard", "ensure"):
        with pytest.raises(RuntimeError, match=r"outside|unsafe"):
            store.discard(src) if operation == "discard" else store.ensure_clean(str(src))
        assert dst.read_bytes() == b"old clean"
    elif operation == "display":
        assert store.clean_or_original(src) == src
    else:
        assert store.is_current(src)
    assert src.read_bytes() == b"original"
    assert foreign.read_bytes() == before


def test_marker_symlink_cannot_be_touched_or_removed(dirs, monkeypatch, tmp_path):
    src, dst = _cached_clean(dirs, monkeypatch)
    foreign = tmp_path / "foreign.txt"
    foreign.write_bytes(b"private")
    before = foreign.stat().st_mtime_ns
    (dst.parent / (dst.name + ".site")).symlink_to(foreign)
    with pytest.raises(RuntimeError, match=r"outside|unsafe"):
        store.discard(src)
    assert dst.read_bytes() == b"broken derived image"
    assert published.rebuild_site_variants([str(src)]) == 0
    assert foreign.stat().st_mtime_ns == before
