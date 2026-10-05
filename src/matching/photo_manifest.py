"""Accepted album identity: ordered paths + SHA-256, aligned with hashes/embeddings."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PreparedPhoto:
    path: Path
    raw: bytes
    sha256: str


def build_manifest(
    paths: Sequence[str | Path],
    hashes: Sequence[str],
    *,
    relative: bool = False,
) -> list[dict[str, str]]:
    if len(paths) != len(hashes):
        return []
    result = []
    try:
        for path, phash in zip(paths, hashes, strict=True):
            path = Path(path)
            result.append(
                {
                    "path": path.name if relative else str(path),
                    "phash": phash,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            )
    except OSError:
        # Old fixtures / unavailable albums remain explicitly unmanifested and must pass
        # conservative legacy evidence resolution before any provider request.
        return []
    return result
