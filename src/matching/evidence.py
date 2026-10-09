"""Immutable accepted photo evidence, aligned with the actual independent CLIP pairs."""

from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image

from src.db import InboundPost, Listing
from src.matching.candidates import parse_phashes
from src.matching.clip_embed import unpack_embeddings
from src.matching.llm_validator import LLMValidatorError
from src.matching.pairs import clip_pairs
from src.matching.phash import compute_phash_from_bytes
from src.matching.photo_manifest import PreparedPhoto

MAX_ALBUM_BYTES = 32 * 1024 * 1024
MAX_ALBUM_FILES = 64


def _read(path: Path) -> PreparedPhoto:
    try:
        if path.stat().st_size > 20 * 1024 * 1024:
            raise ValueError("image too large")
        with path.open("rb") as stream:
            raw = stream.read(20 * 1024 * 1024 + 1)
        if len(raw) > 20 * 1024 * 1024:
            raise ValueError("image too large")
        return PreparedPhoto(path, raw, hashlib.sha256(raw).hexdigest())
    except (OSError, ValueError) as exc:
        raise LLMValidatorError("photo_unavailable", verdict="invalid_evidence") from exc


def _resolve(
    paths: list[Path],
    hashes: list[str],
    manifest: list[dict[str, str]] | None = None,
    directory: Path | None = None,
) -> list[PreparedPhoto]:
    if len(parse_phashes(hashes)) != len(hashes):
        raise LLMValidatorError("malformed_hash", verdict="invalid_evidence")
    if manifest:
        if len(manifest) != len(hashes):
            raise LLMValidatorError("manifest_length", verdict="invalid_evidence")
        aligned = []
        total_bytes = 0
        for entry, phash in zip(manifest, hashes, strict=True):
            path = Path(entry["path"])
            if directory is not None:
                if path.name != entry["path"]:
                    raise LLMValidatorError("manifest_path", verdict="invalid_evidence")
                path = directory / path
            photo = _read(path)
            total_bytes += len(photo.raw)
            if total_bytes > MAX_ALBUM_BYTES:
                raise LLMValidatorError("evidence_budget", verdict="invalid_evidence")
            if entry["phash"] != phash or photo.sha256 != entry["sha256"]:
                raise LLMValidatorError("manifest_changed", verdict="invalid_evidence")
            aligned.append(photo)
        return aligned

    # Conservative legacy migration: resolve compacted accepted hashes by content, never
    # URL index or glob order. Different bytes with the same perceptual hash are ambiguous.
    if len(paths) > MAX_ALBUM_FILES:
        raise LLMValidatorError("evidence_budget", verdict="invalid_evidence")
    total_bytes = 0
    needed = {h.lower() for h in hashes}
    by_hash: dict[str, PreparedPhoto] = {}
    ambiguous: set[str] = set()
    for path in sorted(paths):
        try:
            photo = _read(path)
            total_bytes += len(photo.raw)
            if total_bytes > MAX_ALBUM_BYTES:
                break
            with Image.open(BytesIO(photo.raw)) as header:
                if header.width * header.height > 30_000_000:
                    raise ValueError("too many pixels")
            phash = compute_phash_from_bytes(photo.raw)
            if phash not in needed:
                continue
            if phash in by_hash and by_hash[phash].sha256 != photo.sha256:
                ambiguous.add(phash)
            by_hash.setdefault(phash, photo)
        except (LLMValidatorError, OSError, ValueError, SyntaxError):
            continue
    if total_bytes > MAX_ALBUM_BYTES:
        raise LLMValidatorError("evidence_budget", verdict="invalid_evidence")
    if any(h.lower() not in by_hash or h.lower() in ambiguous for h in hashes):
        raise LLMValidatorError("accepted_photo_missing", verdict="invalid_evidence")
    return [by_hash[h.lower()] for h in hashes]


def _align(
    paths: list[Path],
    hashes: list[str],
    manifest: list[dict[str, str]] | None = None,
    directory: Path | None = None,
) -> list[Path]:
    """Compatibility helper for inspection; runtime uses immutable _resolve results."""
    return [photo.path for photo in _resolve(paths, hashes, manifest, directory)]


def select_evidence(
    inbound: InboundPost,
    listing: Listing,
    settings: Any,
) -> tuple[list[PreparedPhoto], list[PreparedPhoto]]:
    ih, lh = list(inbound.photo_phashes or []), list(listing.photo_phashes or [])
    a = unpack_embeddings(inbound.photo_embeddings) if inbound.photo_embeddings else []
    b = unpack_embeddings(listing.photo_embeddings) if listing.photo_embeddings else []
    if not ih or not lh or len(a) != len(ih) or len(b) != len(lh):
        raise LLMValidatorError("unaligned_evidence", verdict="invalid_evidence")
    pairs = sorted(
        clip_pairs(a, b, settings.clip_similarity_threshold), key=lambda p: (-p[2], p[0], p[1])
    )[:3]
    if len(pairs) < settings.min_matched_photos:
        raise LLMValidatorError("insufficient_pairs", verdict="invalid_evidence")
    for manifest, hashes in ((inbound.photo_evidence, ih), (listing.photo_evidence, lh)):
        if manifest and len(manifest) != len(hashes):
            raise LLMValidatorError("manifest_length", verdict="invalid_evidence")
    ii, li = [i for i, _, _ in pairs], [j for _, j, _ in pairs]
    directory = settings.listings_photos_dir / listing.platform.value / listing.external_id
    ip = _resolve(
        [Path(p) for p in inbound.photo_paths or []],
        [ih[i] for i in ii],
        [inbound.photo_evidence[i] for i in ii] if inbound.photo_evidence else [],
    )
    lp = _resolve(
        list(directory.glob("*.*")),
        [lh[i] for i in li],
        [listing.photo_evidence[i] for i in li] if listing.photo_evidence else [],
        directory,
    )
    return ip, lp


def select_evidence_paths(
    inbound: InboundPost,
    listing: Listing,
    settings: Any,
) -> tuple[list[Path], list[Path]]:
    a, b = select_evidence(inbound, listing, settings)
    return [photo.path for photo in a], [photo.path for photo in b]
