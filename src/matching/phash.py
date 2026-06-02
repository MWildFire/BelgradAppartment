"""Perceptual hashing for near-duplicate photo matching.

A pHash is a 64-bit fingerprint of an image that is robust to:
- Re-compression (JPEG → JPEG at different quality).
- Downscaling.
- Small color/brightness shifts.

It is NOT robust to: cropping, mirroring, watermarks covering >10% of the image.
For those cases we fall back to CLIP embeddings (see clip_embed.py — Phase 6).
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import httpx
import imagehash
from PIL import Image, UnidentifiedImageError


def compute_phash_from_bytes(image_bytes: bytes) -> str:
    """Return pHash as a hex string. Raises UnidentifiedImageError on garbage input."""
    with Image.open(BytesIO(image_bytes)) as img:
        img = img.convert("RGB")
        return str(imagehash.phash(img))


def compute_phash_from_path(path: Path) -> str:
    with Image.open(path) as img:
        img = img.convert("RGB")
        return str(imagehash.phash(img))


def hamming_distance(hash_a: str, hash_b: str) -> int:
    """Hamming distance between two pHash hex strings (0..64)."""
    return int(imagehash.hex_to_hash(hash_a) - imagehash.hex_to_hash(hash_b))


def is_similar(hash_a: str, hash_b: str, max_hamming: int = 6) -> bool:
    return hamming_distance(hash_a, hash_b) <= max_hamming


async def download_and_hash(
    url: str,
    save_to: Path,
    client: httpx.AsyncClient,
    *,
    platform: str | None = None,
    listing_external_id: str | None = None,
) -> tuple[Path, str] | None:
    """Download image to `save_to`, compute pHash. Returns None on failure.

    If `platform` is given, every attempt (ok or failed) is also recorded in the per-platform
    photo-download stats log — `otz photo-stats` aggregates from there.
    """
    save_to.parent.mkdir(parents=True, exist_ok=True)

    def _record(status: str, error: str | None = None) -> None:
        if not platform:
            return
        try:
            from src.jobs.photo_stats import record_photo_download

            record_photo_download(
                platform,
                listing_external_id=listing_external_id or "",
                url=url,
                status=status,  # type: ignore[arg-type]
                error=error,
            )
        except Exception:
            pass  # logger is best-effort, never break the download path

    try:
        resp = await client.get(url)
        resp.raise_for_status()
    except (httpx.HTTPError, httpx.InvalidURL) as e:
        _record("failed", f"{type(e).__name__}: {e}"[:200])
        return None

    content = resp.content
    try:
        phash = compute_phash_from_bytes(content)
    except (UnidentifiedImageError, OSError) as e:
        _record("failed", f"decode: {type(e).__name__}: {e}"[:200])
        return None

    save_to.write_bytes(content)
    _record("ok")
    return save_to, phash
