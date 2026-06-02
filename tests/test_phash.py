"""Tests for the pHash module — synthesised tiny images, no network."""

from __future__ import annotations

from io import BytesIO

from PIL import Image

from src.matching.phash import compute_phash_from_bytes, hamming_distance, is_similar


def _img_bytes(color: tuple[int, int, int], size: int = 64) -> bytes:
    img = Image.new("RGB", (size, size), color=color)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def test_phash_is_deterministic() -> None:
    img = _img_bytes((128, 64, 32))
    assert compute_phash_from_bytes(img) == compute_phash_from_bytes(img)


def test_phash_different_images_differ() -> None:
    a = compute_phash_from_bytes(_img_bytes((10, 10, 10)))
    b = compute_phash_from_bytes(_img_bytes((250, 250, 250)))
    # Solid-color images often share a pHash → use a gradient to force divergence.
    img = Image.new("RGB", (64, 64))
    for x in range(64):
        for y in range(64):
            img.putpixel((x, y), (x * 4 % 256, y * 4 % 256, 100))
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=85)
    c = compute_phash_from_bytes(buf.getvalue())
    assert a != c or b != c, "at least one comparison should differ"


def test_hamming_and_is_similar() -> None:
    h1 = "ffffffffffffffff"
    h2 = "fffffffffffffff0"
    assert hamming_distance(h1, h2) == 4
    assert is_similar(h1, h2, max_hamming=6) is True
    assert is_similar(h1, h2, max_hamming=2) is False
