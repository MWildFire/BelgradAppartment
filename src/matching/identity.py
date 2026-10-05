"""Durable apartment identities and reversible source evidence, independent of activity."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime

from sqlalchemy import select, text, update

from src.db import Listing, get_session
from src.db.models import ApartmentIdentity, IdentityEvent, ListingIdentityEdge
from src.matching.candidates import parse_phashes
from src.matching.dedup import DedupGroup, DedupRow, _pick_primary, _UnionFind
from src.matching.pairs import phash_pairs


async def reconcile(
    rows: list[DedupRow],
    proposed: list[tuple[int, int]],
    max_hamming: int,
    min_shared: int,
    *,
    write: bool,
    review_edge: tuple[int, int, bool, str] | None = None,
) -> tuple[list[tuple[int, int, bool]], list[DedupGroup], int]:
    by_id = {row.id: row for row in rows}
    async with get_session() as session:
        # SQLite serializes scan acceptance, operator tombstones and materialization.
        if write:
            await session.execute(text("BEGIN IMMEDIATE"))
        current = (
            await session.execute(
                select(
                    Listing.id,
                    Listing.photo_phashes,
                    Listing.photo_evidence,
                    Listing.dup_group_id,
                    Listing.is_active,
                    Listing.photo_urls,
                    Listing.lat,
                    Listing.lon,
                )
            )
        ).all()
        for row in current:
            prior = by_id.get(row.id)
            if (
                prior is None
                or prior.phash_ints != tuple(parse_phashes(row.photo_phashes or []))
                or (prior.manifest_sha != tuple(p["sha256"] for p in row.photo_evidence or []))
            ):
                raise RuntimeError("identity_scan_changed_retry")
            # Activity may legitimately change during the CPU scan; use the latest primary facts.
            by_id[row.id] = DedupRow(
                row.id,
                prior.phash_ints,
                len(row.photo_urls or []),
                row.lat is not None and row.lon is not None,
                row.is_active,
                prior.manifest_sha,
            )
        if len(current) != len(rows):
            raise RuntimeError("identity_scan_changed_retry")
        identities = list((await session.execute(select(ApartmentIdentity))).scalars())
        ledger = {
            (edge.left_id, edge.right_id): edge
            for edge in (await session.execute(select(ListingIdentityEdge))).scalars()
        }
        now = datetime.now(UTC)
        if review_edge is not None:
            left, right, confirmed, reason = review_edge
            key = tuple(sorted((left, right)))
            if key not in ledger or not reason.strip():
                raise ValueError("Existing edge and non-empty review reason required")
            edge = ledger[key]
            edge.state = "confirmed" if confirmed else "revoked"
            edge.reviewed_at, edge.review_reason = now, reason.strip()
            session.add(
                IdentityEvent(
                    kind="edge_review",
                    payload={
                        "left": key[0],
                        "right": key[1],
                        "state": edge.state,
                        "reason": reason.strip(),
                    },
                )
            )
        for left, right in proposed:
            # A tombstone is authoritative even when a later automatic scan sees similar photos.
            if (left, right) in ledger:
                continue
            a, b = by_id[left], by_id[right]
            pairs = phash_pairs(a.phash_ints, b.phash_ints, max_hamming)
            if len(pairs) < min_shared:
                continue
            evidence = {
                "version": "independent-phash-v1",
                "max_hamming": max_hamming,
                "min_shared": min_shared,
                "left_hashes": list(a.phash_ints),
                "right_hashes": list(b.phash_ints),
                "pairs": pairs,
                "left_manifest_sha": list(a.manifest_sha),
                "right_manifest_sha": list(b.manifest_sha),
            }
            edge = ListingIdentityEdge(
                left_id=left, right_id=right, state="confirmed", evidence=evidence, created_at=now
            )
            ledger[left, right] = edge
            if write:
                session.add(edge)
                session.add(
                    IdentityEvent(kind="edge_confirmed", payload={"left": left, "right": right})
                )
        positions = {row.id: i for i, row in enumerate(rows)}
        uf = _UnionFind(len(rows))
        for (left, right), edge in ledger.items():
            if edge.state == "confirmed" and left in positions and right in positions:
                uf.union(positions[left], positions[right])
        components: dict[int, list[DedupRow]] = defaultdict(list)
        for scanned_row in rows:
            components[uf.find(positions[scanned_row.id])].append(by_id[scanned_row.id])
        ever_linked = {lid for key in ledger for lid in key}
        assignments, groups = [], []
        demoted = 0
        for members in sorted(components.values(), key=lambda group: min(r.id for r in group)):
            ids = {r.id for r in members}
            anchored = sorted(
                (identity for identity in identities if identity.anchor_listing_id in ids),
                key=lambda identity: identity.id,
            )
            if len(members) == 1 and not anchored and members[0].id not in ever_linked:
                continue
            anchor = (
                anchored[0]
                if anchored
                else ApartmentIdentity(anchor_listing_id=min(ids), created_at=now)
            )
            if not anchored and write:
                session.add(anchor)
                await session.flush()
            # Materialized group keys stay Listing IDs. No collision with unrelated singleton IDs.
            gid = anchor.anchor_listing_id
            primary = _pick_primary(members) if any(r.is_active for r in members) else by_id[gid]
            if write:
                for identity in anchored:
                    target = None if identity is anchor else anchor.id
                    if identity.merged_into != target:
                        session.add(
                            IdentityEvent(
                                kind="identity_alias",
                                payload={
                                    "identity": identity.id,
                                    "before": identity.merged_into,
                                    "after": target,
                                },
                            )
                        )
                    # Direct aliases: splits restore their anchored identity, without cycles.
                    identity.merged_into = target
            for member in members:
                is_primary = member.id == primary.id
                assignments.append((member.id, gid, is_primary))
                demoted += not is_primary
            if len(members) > 1:
                groups.append(DedupGroup(gid, primary.id, tuple(sorted(ids))))
        if write:
            await session.execute(update(Listing).values(dup_group_id=None, is_primary=True))
            by_value: dict[tuple[int, bool], list[int]] = defaultdict(list)
            for lid, gid, primary_flag in assignments:
                by_value[gid, primary_flag].append(lid)
            for (gid, primary_flag), member_ids in by_value.items():
                await session.execute(
                    update(Listing)
                    .where(Listing.id.in_(member_ids))
                    .values(dup_group_id=gid, is_primary=primary_flag)
                )
        else:
            await session.rollback()
        return assignments, groups, demoted
