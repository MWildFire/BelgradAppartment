"""Durable identity, explicit reversible reviews, namespace and inactive-source safety."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from src.db import Listing, Platform, get_session
from src.db.models import ApartmentIdentity, IdentityEvent, ListingIdentityEdge
from src.matching.dedup import find_cross_platform_dupes


async def seed(albums):
    async with get_session() as s:
        rows = [
            Listing(
                platform=Platform.NEKRETNINE,
                external_id=str(i),
                url=f"https://example.test/{i}",
                photo_phashes=hashes,
                photo_urls=[f"photo{j}" for j in range(len(hashes))],
            )
            for i, hashes in enumerate(albums)
        ]
        s.add_all(rows)
        await s.flush()
        return [r.id for r in rows]


async def groups():
    async with get_session() as s:
        return {
            r.id: (r.dup_group_id, r.is_primary, r.is_active)
            for r in (await s.execute(select(Listing))).scalars()
        }


async def test_photos_and_activity_changes_do_not_erase_confirmed_identity(db):
    a, b = await seed([["0" * 16, "f" * 16], ["0" * 16, "f" * 16]])
    await find_cross_platform_dupes()
    original = await groups()
    async with get_session() as s:
        (await s.get(Listing, a)).is_active = False
        (await s.get(Listing, b)).photo_phashes = ["5" * 16, "a" * 16]
    await find_cross_platform_dupes()
    after = await groups()
    assert after[a][0] == after[b][0] == original[a][0] == a
    assert after[b][1] and not after[a][1]
    async with get_session() as s:
        # Same query used by sibling availability consumers sees the live source.
        live = list(
            (
                await s.execute(
                    select(Listing.id).where(
                        Listing.dup_group_id == after[a][0], Listing.is_active.is_(True)
                    )
                )
            ).scalars()
        )
        assert live == [b]
        (await s.get(Listing, a)).is_active = True
        edge = await s.get(ListingIdentityEdge, (a, b))
        assert edge.evidence["left_hashes"] == [0, (1 << 64) - 1]
        assert len(edge.evidence["pairs"]) == 2
    await find_cross_platform_dupes()
    assert (await groups())[a][0] == a


async def test_revoke_tombstone_survives_rescan_and_reconfirm_is_explicit(db):
    a, b = await seed([["0" * 16, "f" * 16], ["0" * 16, "f" * 16]])
    await find_cross_platform_dupes()
    await find_cross_platform_dupes(review_edge=(a, b, False, "synthetic operator correction"))
    split = await groups()
    assert split[a][0] != split[b][0]
    await find_cross_platform_dupes()
    assert await groups() == split
    await find_cross_platform_dupes(review_edge=(b, a, True, "synthetic re-review"))
    joined = await groups()
    assert joined[a][0] == joined[b][0]
    await find_cross_platform_dupes(review_edge=(a, b, False, "split again"))
    assert await groups() == split
    async with get_session() as s:
        identities = list((await s.execute(select(ApartmentIdentity))).scalars())
        assert len(identities) == 2 and all(i.merged_into is None for i in identities)
        reviews = list(
            (
                await s.execute(select(IdentityEvent).where(IdentityEvent.kind == "edge_review"))
            ).scalars()
        )
        assert len(reviews) == 3


async def test_legacy_group_number_is_not_evidence_and_identity_id_cannot_collide(db):
    x, a, b = await seed([["a" * 16], ["0" * 16, "f" * 16], ["0" * 16, "f" * 16]])
    async with get_session() as s:
        for lid in (x, a, b):
            (await s.get(Listing, lid)).dup_group_id = 99
    await find_cross_platform_dupes()
    result = await groups()
    async with get_session() as s:
        identity = (await s.execute(select(ApartmentIdentity))).scalar_one()
        assert identity.id == x  # intentionally collides with the unrelated listing ID
        assert identity.anchor_listing_id == a
    assert result[x][0] is None
    assert result[a][0] == result[b][0] == a


async def test_triangle_edge_revocation_does_not_claim_a_split(db):
    a, _b, c = await seed([["0" * 16, "f" * 16]] * 3)
    await find_cross_platform_dupes()
    async with get_session() as s:
        edges = list((await s.execute(select(ListingIdentityEdge))).scalars())
        assert len(edges) == 3  # natural triangle; no synthetic ledger insertion
    await find_cross_platform_dupes(review_edge=(a, c, False, "remove one triangle edge"))
    assert len({value[0] for value in (await groups()).values()}) == 1


async def test_duplicate_photos_and_empty_review_do_not_create_edges(db):
    a, b = await seed([["0" * 16] * 2, ["0" * 16] * 2])
    assert (await find_cross_platform_dupes()).groups_found == 0
    async with get_session() as s:
        assert list((await s.execute(select(ListingIdentityEdge))).scalars()) == []
    with pytest.raises(ValueError):
        await find_cross_platform_dupes(review_edge=(a, b, False, ""))


async def test_purge_protects_identity_anchors_and_revoked_endpoints(db, tmp_path, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import text

    from src.cli import _purge_inactive
    from src.config import get_settings

    a, b = await seed([["0" * 16, "f" * 16]] * 2)
    await find_cross_platform_dupes()
    await find_cross_platform_dupes(review_edge=(a, b, False, "synthetic reviewed edge"))
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    async with get_session() as s:
        await s.execute(text("PRAGMA foreign_keys=ON"))
        for lid in (a, b):
            listing = await s.get(Listing, lid)
            listing.is_active = False
            listing.last_seen_at = datetime.now(UTC) - timedelta(days=90)
    report = await _purge_inactive(older_than_days=30, dry_run=True)
    assert report.to_purge == 0 and len(report.protected) == 2
    result = await _purge_inactive(older_than_days=30, dry_run=False)
    assert result.deleted == 0
    assert not (tmp_path / "archive").exists()
    assert len(await groups()) == 2
