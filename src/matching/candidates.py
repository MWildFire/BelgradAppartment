"""Batch-level candidate snapshot for the matcher.

`match_all_pending` used to re-run the full active-listings SELECT (and re-parse every hex
pHash) once **per inbound** — N table scans per historical batch. `load_candidate_cache`
loads the lightweight scan rows ONCE; `match_inbound(..., cache=...)` then skips its
per-call SELECT and hex parsing entirely.

CLIP embeddings are deliberately NOT cached: ~22k active listings x ~20 KB blob ≈ 440 MB
resident, which would undo the chunked-loading fix that brought matcher peak RAM from
415 MB down to ~30 MB. Stage 2 keeps fetching `photo_embeddings` in bounded chunks per
inbound; only the cheap scalar fields and pre-parsed pHash ints live in the cache
(~a few MB for 22k listings).
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db import Listing


@dataclass(frozen=True, slots=True)
class CandidateRow:
    """One active listing reduced to exactly what the CPU scan needs.

    Attribute names match the matcher's text cross-check contract
    (price_eur / area_m2 / district).
    """

    id: int
    phash_ints: tuple[int, ...]
    price_eur: int | None
    area_m2: float | None
    district: str | None
    has_embs: bool
    # A4 — cross-platform dedup primary flag. Defaults True so ungrouped listings (the vast
    # majority) behave exactly as before; only used as a final tie-break in _select_best.
    is_primary: bool = True


@dataclass(slots=True)
class CandidateCache:
    """Snapshot of active listings, taken once per batch.

    Listings are not modified by matching, so within a single-process batch this is
    equivalent to re-querying per inbound — just N-1 fewer table scans.
    """

    rows: list[CandidateRow]


def candidate_scan_select() -> Select:
    """The one lightweight candidates SELECT — shared by the per-call path and the cache
    so both iterate listings in the identical row order (the pHash-vs-CLIP signal choice
    is deliberately order-dependent; see matcher._select_best).

    photo_embeddings BLOBs are NOT selected — only an IS NOT NULL flag (see module docstring).
    """
    return select(
        Listing.id,
        Listing.photo_phashes,
        Listing.price_eur,
        Listing.area_m2,
        Listing.district,
        Listing.photo_embeddings.isnot(None).label("has_embs"),
        Listing.is_primary,
    ).where(Listing.is_active.is_(True))


def parse_phashes(hex_hashes: list[str]) -> list[int]:
    """Hex pHashes → 64-bit ints, silently dropping malformed entries.

    Every pHash in this system is str(imagehash.phash()) = exactly 16 hex chars; anything else
    is a corrupt row whose short hash would XOR into phantom near-zero distances — skip it
    (the old imagehash path raised on length mismatch, which skipped the pair too).
    """
    out: list[int] = []
    for h in hex_hashes:
        if not isinstance(h, str) or len(h) != 16:
            continue
        try:
            out.append(int(h, 16))
        except ValueError:
            continue
    return out


def scan_rows_from_db(db_rows: Sequence[Any]) -> list[CandidateRow]:
    """Convert raw SELECT rows to CandidateRow, pre-parsing hex pHashes to ints (CPU —
    callers run this off the event loop). Preserves row order."""
    return [
        CandidateRow(
            id=r.id,
            phash_ints=tuple(parse_phashes(r.photo_phashes)) if r.photo_phashes else (),
            price_eur=r.price_eur,
            area_m2=r.area_m2,
            district=r.district,
            has_embs=bool(r.has_embs),
            is_primary=bool(getattr(r, "is_primary", True)),
        )
        for r in db_rows
    ]


async def load_candidate_cache(session: AsyncSession) -> CandidateCache:
    """Load the candidate snapshot once for a whole batch (W2-D2)."""
    db_rows = (await session.execute(candidate_scan_select())).all()
    rows = await asyncio.to_thread(scan_rows_from_db, db_rows)
    return CandidateCache(rows=rows)
