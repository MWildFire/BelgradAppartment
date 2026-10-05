"""Cross-platform identity from independent photo evidence.

The indexed scan proposes direct, evidenced links, subject to a fixed edge budget. The durable
ledger retains accepted links across activity and photo changes, with explicit review
and revocation. Materialized group keys use historical Listing anchors; primary selection
prefers an active source. A shared stock photo alone never creates an edge.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import select

from src.config import get_settings
from src.db import Listing, get_session
from src.matching.candidates import parse_phashes
from src.matching.pairs import phash_pairs

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DedupRow:
    """One active listing reduced to what the grouping scan needs."""

    id: int
    phash_ints: tuple[int, ...]
    photo_count: int
    has_coords: bool
    is_active: bool = True
    manifest_sha: tuple[str, ...] = ()


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
    """Only independent one-to-one photo pairs count as identity evidence."""
    return len(phash_pairs(a, b, max_hamming)) >= min_shared


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


def _group_rows_quadratic(
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



def _find_photo_edges(
    rows: list[DedupRow], max_hamming: int, min_shared: int
) -> list[tuple[int, int]]:
    """Find the same connected components using an exact photo candidate index.

    Split 64 bits into h+1 disjoint chunks. Two hashes at distance <=h must share
    at least one whole chunk, so candidate retrieval cannot discard a matching
    photo. Every candidate still receives the original XOR/popcount check.
    """
    if (
        not 0 <= max_hamming <= 8
        or min_shared <= 0
        or any(h < 0 or h >= 1 << 64 for row in rows for h in row.phash_ints)
    ):
        # Preserve the existing semantics for unusual configurations or malformed ints.
        uf = _UnionFind(len(rows))
        edges = []
        for i, a in enumerate(rows):
            for j in range(i + 1, len(rows)):
                if _shares_enough_photos(
                    a.phash_ints, rows[j].phash_ints, max_hamming, min_shared,
                ):
                    uf.union(i, j)
                    edges.append((min(a.id, rows[j].id), max(a.id, rows[j].id)))
                    if len(edges) > 25000:
                        raise RuntimeError("identity_edge_budget")
        return edges

    owners: dict[int, set[int]] = defaultdict(set)
    for i, row in enumerate(rows):
        for h in row.phash_ints:
            owners[h].add(i)

    parts = max_hamming + 1
    slices = [
        (64 * i // parts, (1 << (64 * (i + 1) // parts - 64 * i // parts)) - 1)
        for i in range(parts)
    ]
    buckets: list[dict[int, list[int]]] = [defaultdict(list) for _ in slices]
    for h in owners:
        for index, (shift, mask) in zip(buckets, slices, strict=True):
            index[(h >> shift) & mask].append(h)

    targets_by_hash: dict[int, tuple[int, ...]] = {}
    uf = _UnionFind(len(rows))
    edges = []
    for i, row in enumerate(rows):
        if len(row.phash_ints) < min_shared:
            continue
        possible: set[int] = set()
        for h in set(row.phash_ints):
            if h not in targets_by_hash:
                candidates: set[int] = set()
                for index, (shift, mask) in zip(buckets, slices, strict=True):
                    candidates.update(index[(h >> shift) & mask])
                targets: set[int] = set()
                for candidate in candidates:
                    if (h ^ candidate).bit_count() <= max_hamming:
                        targets.update(owners[candidate])
                targets_by_hash[h] = tuple(targets)
            possible.update(targets_by_hash[h])
        for j in sorted(possible):
            if j > i and _shares_enough_photos(
                row.phash_ints, rows[j].phash_ints, max_hamming, min_shared,
            ):
                uf.union(i, j)
                edges.append((min(row.id, rows[j].id), max(row.id, rows[j].id)))
                if len(edges) > 25000:
                    raise RuntimeError("identity_edge_budget")

    return edges


def _group_rows(rows: list[DedupRow], max_hamming: int, min_shared: int) -> list[list[DedupRow]]:
    uf = _UnionFind(len(rows))
    positions = {row.id: i for i, row in enumerate(rows)}
    for left, right in _find_photo_edges(rows, max_hamming, min_shared):
        uf.union(positions[left], positions[right])
    groups: dict[int, list[DedupRow]] = defaultdict(list)
    for i, row in enumerate(rows):
        groups[uf.find(i)].append(row)
    return [members for members in groups.values() if len(members) >= 2]


def _pick_primary(members: list[DedupRow]) -> DedupRow:
    """Primary = most photos → has coords → lowest id (stable final tiebreak)."""
    return max(members, key=lambda r: (r.is_active, r.photo_count, r.has_coords, -r.id))


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
    *, max_hamming: int | None = None, min_shared_photos: int | None = None, write: bool = True,
    review_edge: tuple[int, int, bool, str] | None = None,
) -> DedupStats:
    """Reconcile durable evidenced identities; active primary is a separate selection.

    review_edge=(left, right, confirmed, reason) explicitly changes one edge. Revoking
    a triangle edge may leave another connecting path; returned groups show the result.
    """
    from src.matching.identity import reconcile

    if review_edge is not None and not write:
        raise ValueError("An edge review requires write=True")
    settings = get_settings()
    max_h = settings.dedup_max_hamming if max_hamming is None else max_hamming
    min_shared = settings.dedup_min_shared_photos if min_shared_photos is None else min_shared_photos
    async with get_session() as session:
        db_rows = (await session.execute(select(
            Listing.id, Listing.photo_phashes, Listing.photo_urls, Listing.lat, Listing.lon,
            Listing.is_active, Listing.photo_evidence,
        ).order_by(Listing.id))).all()
    rows = [DedupRow(
        id=r.id, phash_ints=tuple(parse_phashes(r.photo_phashes or [])),
        photo_count=len(r.photo_urls or []), has_coords=r.lat is not None and r.lon is not None,
        is_active=r.is_active, manifest_sha=tuple(p["sha256"] for p in r.photo_evidence or []),
    ) for r in db_rows]
    edges = await asyncio.to_thread(_find_photo_edges, rows, max_h, min_shared)
    assignments, groups, demoted = await reconcile(
        rows, edges, max_h, min_shared, write=write, review_edge=review_edge,
    )
    return DedupStats(sum(r.is_active for r in rows), len(groups), len(assignments),
                      demoted, tuple(groups))
