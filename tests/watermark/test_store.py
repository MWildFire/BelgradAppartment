"""Clean copies: data/clean/<rel>, sidecar, reuse; originals are never touched."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

import src.watermark.pipeline as pipeline
import src.watermark.store as store
from src.config import PROJECT_ROOT, get_settings
from src.watermark.inpaint import TeleaInpainter
from src.watermark.settings import get_watermark_settings
from src.watermark.templates import load_templates
from tests.watermark.synth import apply_mark, make_background


@pytest.fixture
def dirs(tmp_path: Path, monkeypatch):
    inbound, clean = tmp_path / "inbound", tmp_path / "clean"
    inbound.mkdir()
    monkeypatch.setattr(get_settings(), "inbound_photos_dir", inbound)
    monkeypatch.setattr(get_watermark_settings(), "watermark_clean_dir", clean)
    monkeypatch.setattr(get_watermark_settings(), "watermark_lama_enabled", False)
    # 4zida needs LaMa; a classic inpainter stands in so the storage logic is covered without torch.
    monkeypatch.setattr(pipeline, "get_inpainter", lambda preferred: TeleaInpainter())
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


def test_store_and_site_imports_do_not_load_opencv():
    """Without the watermark flags the site and admin must not import OpenCV (cv2)."""
    probe = (
        "import sys, src.watermark.store, src.site.photos, src.site.media, src.site.og_image; "
        "print('cv2' in sys.modules)"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=True, timeout=120,
    )
    assert done.stdout.strip() == "False"


def _cityexpert_photo(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tpl = {t.id: t for t in load_templates()}["cityexpert_center"]
    img = apply_mark(make_background(1920, 1080, seed=7), tpl)
    Image.fromarray(img).save(path, "JPEG", quality=92)
    return path


def test_kind_switch_invalidates_stored_copies_both_ways(dirs, monkeypatch):
    """WATERMARK_DISABLED_KINDS acts on photos that already have a clean copy, and lifting it
    brings the removal back (a kept_reason="disabled" copy is recomputed)."""
    inbound, _clean = dirs
    src = _cityexpert_photo(inbound / "9" / "9" / "00.jpg")
    before = src.read_bytes()
    first = store.ensure_clean(str(src))
    assert first is not None and first.clean is not None and not first.reused
    assert store.clean_or_original(src) == first.clean
    settings = get_watermark_settings()

    monkeypatch.setattr(settings, "watermark_disabled_kinds", "cityexpert_center")
    assert not store.is_current(src)  # the copy no longer matches the policy
    assert store.clean_or_original(src) == src  # the site stops using it right away
    kept = store.ensure_clean(str(src))
    assert kept is not None and kept.clean is None and not kept.reused
    marks = kept.report["marks"]
    assert [(m["method"], m["kept_reason"]) for m in marks] == [("skip", "disabled")]
    assert src.read_bytes() == before  # the original is byte-identical
    assert not first.clean.exists()  # the stale derivative is gone
    meta = store.read_sidecar(first.clean)
    assert meta is not None and meta["disabled_kinds"] == "cityexpert_center"
    assert store.ensure_clean(str(src)).reused  # same policy: reused

    monkeypatch.setattr(settings, "watermark_disabled_kinds", "")
    assert not store.is_current(src)
    again = store.ensure_clean(str(src))
    assert again is not None and again.clean is not None and not again.reused
    assert [(m["method"], m["kept_reason"]) for m in again.report["marks"]] == [
        ("reverse_blend", None)
    ]


def test_store_version_was_bumped_for_wm05():
    assert store.STORE_VERSION == 3


@pytest.mark.parametrize("sub", [".", "clean"])
def test_clean_dir_inside_inbound_is_refused(dirs, monkeypatch, sub):
    inbound, _clean = dirs
    monkeypatch.setattr(get_watermark_settings(), "watermark_clean_dir", inbound / sub)
    src = _photo(inbound / "5" / "5" / "00.jpg", marked=True)
    with pytest.raises(RuntimeError):
        store.clean_root()
    with pytest.raises(RuntimeError):
        store.ensure_clean(str(src))
    assert store.clean_or_original(src) == src  # the site keeps showing the original
    assert store.is_current(src)
