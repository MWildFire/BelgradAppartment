"""Clean copies: data/clean/<rel>, sidecar, reuse; originals are never touched."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from PIL import Image

import src.watermark.store as store
from src.config import get_settings
from src.watermark.templates import load_templates
from tests.watermark.synth import apply_mark, make_background


@pytest.fixture
def dirs(tmp_path: Path, monkeypatch):
    inbound, clean = tmp_path / "inbound", tmp_path / "clean"
    inbound.mkdir()
    monkeypatch.setattr(get_settings(), "inbound_photos_dir", inbound)
    monkeypatch.setattr(get_settings(), "watermark_clean_dir", clean)
    monkeypatch.setattr(get_settings(), "watermark_lama_enabled", False)
    return inbound, clean


def _photo(path: Path, marked: bool) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = make_background(1440, 1080, seed=5)
    if marked:
        img = apply_mark(img, {t.id: t for t in load_templates()}["zida_logo"])
    Image.fromarray(img).save(path, "JPEG", quality=92)
    return path


def test_clean_copy_mirrors_inbound_layout(dirs):
    inbound, clean = dirs
    src = _photo(inbound / "7" / "123" / "00.jpg", marked=True)
    before = src.read_bytes()
    result = store.ensure_clean(str(src))
    assert result is not None and result.clean == (clean / "7" / "123" / "00.jpg").resolve()
    assert result.clean.is_file() and not result.reused
    assert store.sidecar_for(result.clean).is_file()
    assert src.read_bytes() == before
    assert store.clean_or_original(src) == result.clean


def test_second_call_reuses_until_source_changes(dirs):
    inbound, _clean = dirs
    src = _photo(inbound / "1" / "1" / "00.jpg", marked=True)
    first = store.ensure_clean(str(src))
    second = store.ensure_clean(str(src))
    assert first is not None and second is not None and second.reused
    _photo(src, marked=True)  # same content -> same sha -> reused again
    assert store.ensure_clean(str(src)).reused  # type: ignore[union-attr]
    third = store.ensure_clean(str(src), force=True)
    assert third is not None and not third.reused


def test_photo_without_marks_gets_no_copy(dirs):
    inbound, _clean = dirs
    src = _photo(inbound / "2" / "2" / "00.jpg", marked=False)
    result = store.ensure_clean(str(src))
    assert result is not None and result.clean is None
    assert store.clean_or_original(src) == src.resolve()


def test_relative_stored_paths_resolve_against_project_root(dirs, monkeypatch, tmp_path):
    inbound, _clean = dirs
    src = _photo(inbound / "3" / "3" / "00.jpg", marked=True)
    monkeypatch.setattr(store, "PROJECT_ROOT", tmp_path)
    assert store.ensure_clean("inbound/3/3/00.jpg").clean is not None  # type: ignore[union-attr]
    assert src.is_file()


def test_missing_and_foreign_paths(dirs, tmp_path):
    assert store.ensure_clean(str(tmp_path / "nope.jpg")) is None
    foreign = _photo(tmp_path / "elsewhere" / "x.jpg", marked=True)
    result = store.ensure_clean(str(foreign))
    assert result is not None and result.clean is None and result.report == {"outside_inbound": True}


def test_is_current_follows_sidecar_and_original(dirs):
    inbound, _clean = dirs
    src = _photo(inbound / "4" / "4" / "00.jpg", marked=True)
    assert not store.is_current(src)
    store.ensure_clean(str(src))
    assert store.is_current(src)
    sidecar = store.sidecar_for(store.clean_path_for(src))  # type: ignore[arg-type]
    later = sidecar.stat().st_mtime_ns + 10_000_000
    os.utime(src, ns=(later, later))  # the original was replaced after cleaning
    assert not store.is_current(src)
