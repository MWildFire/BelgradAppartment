"""Concurrent clean-photo rebuilds must publish complete, independent WebP files."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from PIL import Image

from src.site import photos


def test_parallel_rebuilds_use_independent_temporary_files(tmp_path, monkeypatch):
    original = tmp_path / "photo.jpg"
    Image.new("RGB", (40, 30), "red").save(original)
    monkeypatch.setattr(photos, "_variant_source", lambda path: path)
    # Force two rebuilders past the existing-file check, then pause after encoding.
    monkeypatch.setattr(photos, "VARIANT_WIDTHS", {"card": 40})
    barrier = Barrier(2)
    real_save = Image.Image.save

    def save_together(image, fp, *args, **kwargs):
        result = real_save(image, fp, *args, **kwargs)
        if hasattr(fp, "flush"):
            fp.flush()
        barrier.wait(timeout=10)
        return result

    monkeypatch.setattr(Image.Image, "save", save_together)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda _: photos.process_photo(original, "photo.jpg", tmp_path), range(2),
        ))
    assert all(entry is not None for entry, _, _ in results)
    with Image.open(tmp_path / "photo.jpg.card.webp") as generated:
        generated.load()
        assert generated.size == (40, 30)
    assert not list(tmp_path.glob("*.tmp"))


def test_failed_variant_encoding_removes_its_temporary_file(tmp_path, monkeypatch):
    original = tmp_path / "photo.jpg"
    Image.new("RGB", (40, 30), "red").save(original)
    monkeypatch.setattr(photos, "_variant_source", lambda path: path)

    def broken_save(image, fp, *args, **kwargs):
        if hasattr(fp, "write"):
            fp.write(b"partial")
        else:
            Path(fp).write_bytes(b"partial")
        raise OSError("synthetic encoding failure")

    monkeypatch.setattr(Image.Image, "save", broken_save)
    entry, _, _ = photos.process_photo(original, "photo.jpg", tmp_path)
    assert entry is None
    assert not list(tmp_path.glob("*.tmp"))
    assert not list(tmp_path.glob("*.webp"))
