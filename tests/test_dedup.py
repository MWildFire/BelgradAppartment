"""Tests for A4 cross-platform listing dedup: find_cross_platform_dupes().

Groups ACTIVE listings that are the SAME apartment — defined as sharing ≥ dedup_min_shared_photos
photos whose pHash Hamming distance ≤ dedup_max_hamming — via union-find (symmetric +
transitive). Each group of ≥2 gets a stable dup_group_id (min member id) and exactly one
is_primary=True; the rest are demoted. Singletons keep dup_group_id=NULL, is_primary=True.

DB setup mirrors tests/test_link_verify.py: an in-memory SQLite swapped into the global
session factory.
"""

from __future__ import annotations

import itertools

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import src.db.session as db_session
from src.db.models import Base, Listing, Platform
from src.matching.dedup import find_cross_platform_dupes

_SEED_COUNTER = itertools.count(1)


@pytest_asyncio.fixture
async def factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    orig_engine = db_session._engine
    orig_factory = db_session._session_factory
    db_session._engine = engine
    db_session._session_factory = factory
    try:
        yield factory
    finally:
        await engine.dispose()
        db_session._engine = orig_engine
        db_session._session_factory = orig_factory


# --- pHash helpers -----------------------------------------------------------
# A pHash is 16 hex chars = a 64-bit int. Flipping N low bits of a base hash gives a hash
# exactly N Hamming away from it (and ≥ (base-bit-count - N) away from unrelated hashes).


def _hx(value: int) -> str:
    """64-bit int -> 16-char hex pHash string."""
    return format(value & ((1 << 64) - 1), "016x")


# Distinct, far-apart base hashes (each pair differs in many bits → never "similar").
BASE_A = 0x0000_0000_0000_0000
BASE_B = 0xFFFF_FFFF_0000_0000
BASE_C = 0x0F0F_0F0F_0F0F_0F0F
BASE_D = 0x1234_5678_9ABC_DEF0


def _near(base: int, flip_bits: int) -> str:
    """Hash that is exactly `flip_bits` Hamming away from `base` (flip the lowest bits)."""
    mask = (1 << flip_bits) - 1
    return _hx(base ^ mask)


async def _seed(
    factory,
    *,
    platform: Platform,
    phashes: list[str],
    photo_urls: list[str] | None = None,
    lat: float | None = None,
    lon: float | None = None,
    is_active: bool = True,
) -> int:
    n = next(_SEED_COUNTER)
    async with factory() as s:
        listing = Listing(
            platform=platform,
            external_id=f"ext-{n}",
            url=f"http://x/{n}",
            photo_phashes=phashes,
            photo_urls=photo_urls if photo_urls is not None else [f"u{i}" for i in range(len(phashes))],
            lat=lat,
            lon=lon,
            is_active=is_active,
        )
        s.add(listing)
        await s.flush()
        listing_id = listing.id
        await s.commit()
        return listing_id


async def _fetch(factory, listing_id: int) -> Listing:
    async with factory() as s:
        return await s.get(Listing, listing_id)


@pytest.mark.asyncio
async def test_two_listings_two_shared_photos_grouped(factory) -> None:
    """≥2 near-identical pHashes → one group; primary = the one with more photos."""
    # Listing A: 3 photos (2 shared + 1 extra). Listing B: 2 photos (the 2 shared).
    shared1, shared2 = _near(BASE_A, 1), _near(BASE_B, 2)
    a = await _seed(
        factory, platform=Platform.NEKRETNINE,
        phashes=[shared1, shared2, _near(BASE_C, 0)],
    )
    b = await _seed(
        factory, platform=Platform.HALOOGLASI,
        phashes=[_near(BASE_A, 3), _near(BASE_B, 1)],  # within Hamming 6 of shared1/shared2
    )
    stats = await find_cross_platform_dupes()
    assert stats.groups_found == 1
    assert stats.listings_demoted == 1

    la, lb = await _fetch(factory, a), await _fetch(factory, b)
    gid = min(a, b)
    assert la.dup_group_id == gid
    assert lb.dup_group_id == gid
    # A has 3 photos > B's 2 → A is primary.
    assert la.is_primary is True
    assert lb.is_primary is False


@pytest.mark.asyncio
async def test_single_shared_photo_not_grouped(factory) -> None:
    """Sharing only ONE similar photo is too weak (stock/agency) → NOT grouped."""
    shared = _near(BASE_A, 1)
    a = await _seed(factory, platform=Platform.NEKRETNINE, phashes=[shared, _near(BASE_C, 0)])
    b = await _seed(factory, platform=Platform.HALOOGLASI, phashes=[_near(BASE_A, 2), _near(BASE_D, 0)])
    stats = await find_cross_platform_dupes()
    assert stats.groups_found == 0

    la, lb = await _fetch(factory, a), await _fetch(factory, b)
    assert la.dup_group_id is None and lb.dup_group_id is None
    assert la.is_primary is True and lb.is_primary is True


@pytest.mark.asyncio
async def test_three_listings_transitive_one_group(factory) -> None:
    """A~B (share photos X,Y), B~C (share photos Y,Z) → union-find merges all three."""
    x1, x2 = _near(BASE_A, 1), _near(BASE_B, 1)  # A & B share these
    z1, z2 = _near(BASE_C, 1), _near(BASE_D, 1)  # B & C share these
    a = await _seed(factory, platform=Platform.NEKRETNINE, phashes=[x1, x2])
    b = await _seed(
        factory, platform=Platform.HALOOGLASI,
        phashes=[_near(BASE_A, 2), _near(BASE_B, 2), z1, z2],  # shares X-set with A, Z-set with C
    )
    c = await _seed(factory, platform=Platform.CETIRI_ZIDA, phashes=[_near(BASE_C, 2), _near(BASE_D, 2)])
    stats = await find_cross_platform_dupes()
    assert stats.groups_found == 1
    assert stats.listings_demoted == 2

    rows = [await _fetch(factory, i) for i in (a, b, c)]
    gid = min(a, b, c)
    assert all(r.dup_group_id == gid for r in rows)
    # B has 4 photos > A/C (2 each) → B is primary.
    primaries = [r.id for r in rows if r.is_primary]
    assert primaries == [b]


@pytest.mark.asyncio
async def test_dissimilar_listings_no_group(factory) -> None:
    """Totally dissimilar listings → no group; all stay is_primary, dup_group_id NULL."""
    a = await _seed(factory, platform=Platform.NEKRETNINE, phashes=[_near(BASE_A, 0), _near(BASE_B, 0)])
    b = await _seed(factory, platform=Platform.HALOOGLASI, phashes=[_near(BASE_C, 0), _near(BASE_D, 0)])
    stats = await find_cross_platform_dupes()
    assert stats.groups_found == 0
    la, lb = await _fetch(factory, a), await _fetch(factory, b)
    assert la.dup_group_id is None and lb.dup_group_id is None
    assert la.is_primary is True and lb.is_primary is True


@pytest.mark.asyncio
async def test_primary_tiebreak_photos_then_coords_then_id(factory) -> None:
    """Primary selection order: most photos → has coords → lowest id."""
    # All three share the same 2 photos. Tune photo counts / coords to probe the tiebreak.
    s1, s2 = _near(BASE_A, 1), _near(BASE_B, 1)

    # Equal photo count (2 each); only B has coords → B wins on the coords tiebreak.
    a = await _seed(factory, platform=Platform.NEKRETNINE, phashes=[s1, s2])
    b = await _seed(factory, platform=Platform.HALOOGLASI, phashes=[_near(BASE_A, 2), _near(BASE_B, 2)],
                    lat=44.8, lon=20.4)
    c = await _seed(factory, platform=Platform.CETIRI_ZIDA, phashes=[_near(BASE_A, 3), _near(BASE_B, 3)])
    stats = await find_cross_platform_dupes()
    assert stats.groups_found == 1
    rows = [await _fetch(factory, i) for i in (a, b, c)]
    assert [r.id for r in rows if r.is_primary] == [b]


@pytest.mark.asyncio
async def test_write_false_does_not_persist(factory) -> None:
    """write=False reports groups but mutates nothing (safe read-only report mode)."""
    s1, s2 = _near(BASE_A, 1), _near(BASE_B, 1)
    a = await _seed(factory, platform=Platform.NEKRETNINE, phashes=[s1, s2])
    b = await _seed(factory, platform=Platform.HALOOGLASI,
                    phashes=[_near(BASE_A, 2), _near(BASE_B, 2)])
    stats = await find_cross_platform_dupes(write=False)
    # Grouping still computed + reported.
    assert stats.groups_found == 1
    assert len(stats.groups) == 1
    assert stats.groups[0].dup_group_id == min(a, b)
    assert stats.groups[0].primary_id in (a, b)
    # But nothing written: both listings stay ungrouped, is_primary True.
    la, lb = await _fetch(factory, a), await _fetch(factory, b)
    assert la.dup_group_id is None and lb.dup_group_id is None
    assert la.is_primary is True and lb.is_primary is True


@pytest.mark.asyncio
async def test_inactive_listings_excluded(factory) -> None:
    """Only ACTIVE listings are grouped; an inactive same-apartment row is ignored."""
    s1, s2 = _near(BASE_A, 1), _near(BASE_B, 1)
    a = await _seed(factory, platform=Platform.NEKRETNINE, phashes=[s1, s2])
    b = await _seed(factory, platform=Platform.HALOOGLASI,
                    phashes=[_near(BASE_A, 2), _near(BASE_B, 2)], is_active=False)
    stats = await find_cross_platform_dupes()
    assert stats.groups_found == 0
    la = await _fetch(factory, a)
    assert la.dup_group_id is None and la.is_primary is True


@pytest.mark.asyncio
async def test_dupes_report_cli_renders_group(factory, capsys) -> None:
    """`dupes-report` CLI helper prints each group with the primary marked; --no-write."""
    from src.cli import _dupes_report

    s1, s2 = _near(BASE_A, 1), _near(BASE_B, 1)
    a = await _seed(factory, platform=Platform.NEKRETNINE, phashes=[s1, s2, _near(BASE_C, 0)])
    b = await _seed(factory, platform=Platform.HALOOGLASI,
                    phashes=[_near(BASE_A, 2), _near(BASE_B, 2)])
    await _dupes_report(write=False, hamming=None, min_shared=None)
    out = capsys.readouterr().out
    assert "groups: 1" in out
    assert f"group {min(a, b)}" in out
    assert "★ primary" in out  # A (3 photos) is primary
    # read-only: nothing persisted.
    la, lb = await _fetch(factory, a), await _fetch(factory, b)
    assert la.dup_group_id is None and lb.dup_group_id is None


def test_index_matches_pairwise_groups_with_repeated_and_uneven_photos():
    """The directed photo count and transitive closure must remain identical."""
    import random
    from src.matching.dedup import DedupRow, _group_rows, _group_rows_quadratic

    rng = random.Random(7365)
    for _ in range(120):
        bases = [rng.getrandbits(64) for _ in range(12)]
        rows = []
        for i in range(24):
            hashes = []
            for _ in range(rng.randrange(0, 8)):
                h = rng.choice(bases)
                for bit in rng.sample(range(64), rng.randrange(0, 8)):
                    h ^= 1 << bit
                hashes.append(h)
            if i % 4 == 0 and hashes:
                hashes.extend([hashes[0]] * 2)
            rows.append(DedupRow(i, tuple(hashes), len(hashes), bool(i % 2)))
        distance = rng.choice([0, 1, 3, 6, 8])
        minimum = rng.choice([1, 2, 3, 5])
        assert _group_rows(rows, distance, minimum) == _group_rows_quadratic(
            rows, distance, minimum,
        )


@pytest.mark.parametrize("distance", [0, 1, 6, 8])
def test_index_finds_matches_across_bit_chunk_boundaries(distance):
    from src.matching.dedup import DedupRow, _group_rows, _group_rows_quadratic

    # Spread differences through distinct chunks, including the highest bit.
    bits = [63, 0, 9, 18, 27, 36, 45, 54]
    delta = sum(1 << bit for bit in bits[:distance])
    rows = [
        DedupRow(1, (BASE_D, BASE_D), 2, False),
        DedupRow(2, (BASE_D ^ delta,), 1, False),
        DedupRow(3, (BASE_D ^ delta ^ (1 << 62),), 1, False),
    ]
    actual = _group_rows(rows, distance, 2)
    assert actual == _group_rows_quadratic(rows, distance, 2)
    assert [r.id for r in actual[0]] == [1, 2]


@pytest.mark.parametrize("distance,minimum", [(-1, 2), (9, 2), (64, 2), (6, 0)])
def test_index_preserves_unusual_parameter_semantics(distance, minimum):
    from src.matching.dedup import DedupRow, _group_rows, _group_rows_quadratic

    rows = [DedupRow(1, (0, 1), 2, False), DedupRow(2, (3,), 1, False)]
    assert _group_rows(rows, distance, minimum) == _group_rows_quadratic(
        rows, distance, minimum,
    )


def test_index_preserves_out_of_range_hash_semantics():
    from src.matching.dedup import DedupRow, _group_rows, _group_rows_quadratic

    rows = [
        DedupRow(1, (-1, -1), 2, False),
        DedupRow(2, (1 << 65, 1 << 65), 2, False),
    ]
    assert _group_rows(rows, 6, 2) == _group_rows_quadratic(rows, 6, 2)
