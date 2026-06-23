"""A4 — cross-platform listing dedup by pHash (periodic batch job, NOT the per-inbound path).

The same apartment is often posted on several platforms with different prices/slugs/photos.
`find_cross_platform_dupes()` groups ACTIVE listings that are the same apartment, picks a
"primary" representative per group, and tags every member with a shared `dup_group_id` +
`is_primary` flag. It NEVER merges rows — it only tags, so a human (or the site / the matcher
tie-break) can prefer the primary while the raw listings stay intact.

Same-apartment relation (symmetric): two listings are the same apartment iff they share
≥ `dedup_min_shared_photos` photos whose pHash Hamming distance ≤ `dedup_max_hamming`.
A single shared photo is too weak — agency/stock photos get reused across unrelated listings,
so the default minimum is 2. Groups are the connected components of that relation (union-find),
which makes grouping transitive: A~B and B~C put A, B, C in one group even if A and C share no
photo directly.

Primary selection within a group: prefer most photos → has coords → lowest id (stable final
tiebreak). All members get `dup_group_id` = min listing id in the group; the primary keeps
`is_primary=True`, the rest are demoted to False. Singletons stay ungrouped (dup_group_id NULL,
is_primary True).

The O(N²) pHash comparison is pure CPU and runs via `asyncio.to_thread`, exactly like the
matcher's scan stages, so the event loop stays responsive.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from sqlalchemy import select, update

from src.config import get_settings
from src.db import Listing, get_session
from src.matching.candidates import parse_phashes

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DedupRow:
    """One active listing reduced to what the grouping scan needs."""

    id: int
    phash_ints: tuple[int, ...]
    photo_count: int
    has_coords: bool


@dataclass(frozen=True, slots=True)
class DedupGroup:
    """One detected duplicate group, for reporting. `dup_group_id` = min member id."""

    dup_group_id: int
    primary_id: int
    member_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class DedupStats:
    """Summary of one dedup run."""

    active_scanned: int
    groups_found: int
    listings_grouped: int
    listings_demoted: int
    groups: tuple[DedupGroup, ...] = ()


def _shares_enough_photos(
    a: tuple[int, ...], b: tuple[int, ...], max_hamming: int, min_shared: int
) -> bool:
    """True iff ≥ min_shared photos of `a` each have a near-duplicate (Hamming ≤ max_hamming)
    in `b`. XOR + popcount, identical to imagehash's hamming but ~40x faster (see matcher)."""
    if len(a) < min_shared or not b:
        return False
    shared = 0
    for ia in a:
        for ib in b:
            if (ia ^ ib).bit_count() <= max_hamming:
                shared += 1
                break
        if shared >= min_shared:
            return True
    return False


class _UnionFind:
    """Index-based union-find over the row list (path compression + union by size)."""

    __slots__ = ("parent", "size")

    def __init__(self, n: int) -> None:
        self.parent = list(range(n))
        self.size = [1] * n

    def find(self, x: int) -> int:
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self.size[ra] < self.size[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        self.size[ra] += self.size[rb]


def _group_rows(
    rows: list[DedupRow], max_hamming: int, min_shared: int
) -> list[list[DedupRow]]:
    """Connected components of the same-apartment relation. Pure CPU (O(N²) over rows × photos).

    The relation is symmetric, so we only test the upper triangle and union both directions
    implicitly. Returns only multi-member groups (singletons are not duplicates).
    """
    uf = _UnionFind(len(rows))
    for i in range(len(rows)):
        ai = rows[i].phash_ints
        if not ai:
            continue
        for j in range(i + 1, len(rows)):
            bj = rows[j].phash_ints
            if not bj:
                continue
            # Symmetric: share ≥min in either direction (a∈b OR b∈a) — guard against the
            # rare case where one listing has fewer photos than min_shared.
            if _shares_enough_photos(ai, bj, max_hamming, min_shared) or _shares_enough_photos(
                bj, ai, max_hamming, min_shared
            ):
                uf.union(i, j)

    buckets: dict[int, list[DedupRow]] = {}
    for idx, row in enumerate(rows):
        buckets.setdefault(uf.find(idx), []).append(row)
    return [members for members in buckets.values() if len(members) >= 2]


def _pick_primary(members: list[DedupRow]) -> DedupRow:
    """Primary = most photos → has coords → lowest id (stable final tiebreak)."""
    return max(members, key=lambda r: (r.photo_count, r.has_coords, -r.id))


def _compute_assignments(
    rows: list[DedupRow], max_hamming: int, min_shared: int
) -> tuple[list[tuple[int, int, bool]], list[DedupGroup], int]:
    """Pure-CPU core — runs in a worker thread.

    Returns (assignments, groups, listings_demoted) where each assignment is
    (listing_id, dup_group_id, is_primary) for every member of a multi-listing group.
    """
    groups = _group_rows(rows, max_hamming, min_shared)
    assignments: list[tuple[int, int, bool]] = []
    out_groups: list[DedupGroup] = []
    demoted = 0
    for members in groups:
        gid = min(r.id for r in members)
        primary = _pick_primary(members)
        for r in members:
            is_primary = r.id == primary.id
            if not is_primary:
                demoted += 1
            assignments.append((r.id, gid, is_primary))
        out_groups.append(
            DedupGroup(
                dup_group_id=gid,
                primary_id=primary.id,
                member_ids=tuple(sorted(r.id for r in members)),
            )
        )
    return assignments, out_groups, demoted


async def find_cross_platform_dupes(
    *, max_hamming: int | None = None, min_shared_photos: int | None = None, write: bool = True
) -> DedupStats:
    """Group ACTIVE listings that are the same apartment and tag dup_group_id + is_primary.

    Idempotent: every run first resets all active listings to ungrouped (dup_group_id NULL,
    is_primary True), then re-tags from the current photo sets — so listings that left a group
    (e.g. one went inactive, or photos changed) are cleaned up automatically.

    `write=False` computes the grouping but persists nothing (safe read-only — used by the
    `dupes-report` CLI so it can be run any time without mutating the DB).
    """
    settings = get_settings()
    max_h = settings.dedup_max_hamming if max_hamming is None else max_hamming
    min_shared = settings.dedup_min_shared_photos if min_shared_photos is None else min_shared_photos

    async with get_session() as s:
        db_rows = (
            await s.execute(
                select(
                    Listing.id,
                    Listing.photo_phashes,
                    Listing.photo_urls,
                    Listing.lat,
                    Listing.lon,
                ).where(Listing.is_active.is_(True))
            )
        ).all()

        rows = [
            DedupRow(
                id=r.id,
                phash_ints=tuple(parse_phashes(r.photo_phashes)) if r.photo_phashes else (),
                photo_count=len(r.photo_urls) if r.photo_urls else 0,
                has_coords=r.lat is not None and r.lon is not None,
            )
            for r in db_rows
        ]

        # Heavy O(N²) comparison off the event loop (same pattern as matcher scan stages).
        assignments, groups, demoted = await asyncio.to_thread(
            _compute_assignments, rows, max_h, min_shared
        )

        if write:
            # Reset all active listings to ungrouped, then apply the fresh assignments. Reset +
            # re-tag (rather than diffing) keeps the job idempotent and self-healing.
            await s.execute(
                update(Listing)
                .where(Listing.is_active.is_(True))
                .values(dup_group_id=None, is_primary=True)
            )
            # Batch the re-tag: group the assignments by their (dup_group_id, is_primary) value
            # and issue ONE UPDATE … WHERE id IN (…) per distinct value (≤ 2 per group) instead
            # of one UPDATE per listing (N+1).
            by_value: dict[tuple[int, bool], list[int]] = {}
            for listing_id, gid, is_primary in assignments:
                by_value.setdefault((gid, is_primary), []).append(listing_id)
            for (gid, is_primary), ids in by_value.items():
                await s.execute(
                    update(Listing)
                    .where(Listing.id.in_(ids))
                    .values(dup_group_id=gid, is_primary=is_primary)
                )
        else:
            # Read-only: roll back any session state so nothing is persisted.
            await s.rollback()

    stats = DedupStats(
        active_scanned=len(rows),
        groups_found=len(groups),
        listings_grouped=len(assignments),
        listings_demoted=demoted,
        groups=tuple(groups),
    )
    logger.info(
        "[dedup] scanned=%d groups=%d grouped=%d demoted=%d (hamming≤%d, min_shared=%d)",
        stats.active_scanned, stats.groups_found, stats.listings_grouped,
        stats.listings_demoted, max_h, min_shared,
    )
    return stats
