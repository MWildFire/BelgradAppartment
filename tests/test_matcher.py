"""Integration tests for matcher.match_inbound against an in-memory SQLite."""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import src.db.session as db_session
from src.db.models import (
    Base,
    InboundPost,
    InboundStatus,
    Listing,
    Match,
    MatchStatus,
    Platform,
    SourceChannel,
)
from src.matching.matcher import match_all_pending, match_inbound


@pytest_asyncio.fixture
async def db():
    """Spin up a fresh in-memory SQLite DB, swap it in for the global session factory."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    orig_engine = db_session._engine
    orig_factory = db_session._session_factory
    db_session._engine = engine
    db_session._session_factory = factory
    try:
        yield engine
    finally:
        await engine.dispose()
        db_session._engine = orig_engine
        db_session._session_factory = orig_factory


def make_phash(bits: str) -> str:
    """Pack a 64-bit binary string into a 16-char hex pHash."""
    assert len(bits) == 64
    return f"{int(bits, 2):016x}"


def flip(hex_hash: str, positions: list[int]) -> str:
    """Toggle specific bits in a hex hash — useful to produce a 'close' near-duplicate."""
    val = int(hex_hash, 16)
    for pos in positions:
        val ^= 1 << pos
    return f"{val:016x}"


async def _seed(db) -> int:
    """Create one inbound post + matching listing; return inbound_id."""
    async with db.begin() as conn:
        pass  # placeholder
    factory = async_sessionmaker(db, expire_on_commit=False)
    base_phashes = [
        make_phash("0" * 64),
        make_phash("1" * 8 + "0" * 56),
        make_phash("01" * 32),
        make_phash("11110000" * 8),
    ]
    inbound_hashes = base_phashes
    # Listing photos: 2 close (1-bit flips), 1 totally different
    listing_hashes = [
        flip(base_phashes[0], [3]),   # within hamming 1
        flip(base_phashes[1], [10, 20]),  # within hamming 2
        make_phash("10101010" * 8),  # unrelated
    ]
    decoy_hashes = [make_phash("11110000" * 8), make_phash("0" * 64)]
    # decoy: matches photos 3 and 0 (2 matches) — but we want best to be 2-photo one
    # Actually decoy might tie with target — make decoy only match 1 photo to be safe.

    async with factory() as s:
        ch = SourceChannel(tg_id=-1001, username="test", is_active=True)
        s.add(ch)
        await s.flush()
        target = Listing(
            platform=Platform.NEKRETNINE,
            external_id="target",
            url="https://example.com/target",
            photo_urls=["u1", "u2", "u3"],
            photo_phashes=listing_hashes,
        )
        decoy = Listing(
            platform=Platform.CETIRI_ZIDA,
            external_id="decoy",
            url="https://example.com/decoy",
            photo_urls=["u1"],
            photo_phashes=[make_phash("0" * 64)],  # matches only inbound[0]
        )
        s.add_all([target, decoy])
        inbound = InboundPost(
            source_channel_id=ch.id,
            tg_message_id=1,
            photo_paths=[f"/tmp/{i}.jpg" for i in range(len(inbound_hashes))],
            photo_phashes=inbound_hashes,
            status=InboundStatus.NEW,
        )
        s.add(inbound)
        await s.commit()
        return inbound.id, target.id, decoy.id


@pytest.mark.asyncio
async def test_match_inbound_picks_best_listing(db):
    inbound_id, target_id, decoy_id = await _seed(db)
    result = await match_inbound(inbound_id)
    assert result.status == "matched"
    assert result.listing_id == target_id
    assert result.matched_photos == 2  # photos 0 and 1 match the target


@pytest.mark.asyncio
async def test_match_inbound_no_match_when_listings_diverge(db):
    factory = async_sessionmaker(db, expire_on_commit=False)
    async with factory() as s:
        ch = SourceChannel(tg_id=-2002, username="x", is_active=True)
        s.add(ch)
        await s.flush()
        # listing with phashes far from inbound's
        far = Listing(
            platform=Platform.NEKRETNINE,
            external_id="far",
            url="https://x/far",
            photo_urls=["a"],
            photo_phashes=[make_phash("1010" * 16)],
        )
        inbound = InboundPost(
            source_channel_id=ch.id,
            tg_message_id=2,
            photo_paths=["/tmp/a.jpg", "/tmp/b.jpg"],
            photo_phashes=[make_phash("0" * 64), make_phash("1" * 64)],
            status=InboundStatus.NEW,
        )
        s.add_all([far, inbound])
        await s.commit()
        iid = inbound.id

    result = await match_inbound(iid)
    assert result.status == "no_match"
    assert result.listing_id is None

    # Inbound status should be updated to NO_MATCH
    async with factory() as s:
        row = await s.get(InboundPost, iid)
        assert row.status == InboundStatus.NO_MATCH


@pytest.mark.asyncio
async def test_match_is_idempotent(db):
    inbound_id, target_id, _ = await _seed(db)
    r1 = await match_inbound(inbound_id)
    r2 = await match_inbound(inbound_id)
    assert r1.match_id == r2.match_id
    # only one Match row
    factory = async_sessionmaker(db, expire_on_commit=False)
    async with factory() as s:
        from sqlalchemy import func, select

        n = (await s.execute(select(func.count()).select_from(Match))).scalar_one()
        assert n == 1


@pytest.mark.asyncio
async def test_match_all_pending_processes_only_new(db):
    factory = async_sessionmaker(db, expire_on_commit=False)
    async with factory() as s:
        ch = SourceChannel(tg_id=-3003, username="y", is_active=True)
        s.add(ch)
        await s.flush()
        listing = Listing(
            platform=Platform.HALOOGLASI,
            external_id="z",
            url="https://x/z",
            photo_urls=["a"],
            photo_phashes=[make_phash("1100" * 16), make_phash("0011" * 16)],
        )
        new_post = InboundPost(
            source_channel_id=ch.id,
            tg_message_id=1,
            photo_paths=["a.jpg", "b.jpg"],
            photo_phashes=[make_phash("1100" * 16), make_phash("0011" * 16)],
            status=InboundStatus.NEW,
        )
        already_matched = InboundPost(
            source_channel_id=ch.id,
            tg_message_id=2,
            photo_paths=["a.jpg"],
            photo_phashes=[make_phash("1100" * 16)],
            status=InboundStatus.MATCHED,
        )
        s.add_all([listing, new_post, already_matched])
        await s.commit()

    results = await match_all_pending()
    # Only the NEW one runs.
    assert len(results) == 1
    assert results[0].status == "matched"
