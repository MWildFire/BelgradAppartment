"""Read-model: list_public / get_public / facets / map_points / similar."""

from __future__ import annotations

import pytest

from src.site import readmodel as rm
from src.site.readmodel import Facets, Filters, SiteListingView


async def _list(site_db: str, **kw):  # type: ignore[no-untyped-def]
    filters = Filters(**{k: v for k, v in kw.items() if k not in ("sort", "page", "page_size")})
    return await rm.list_public(
        filters,
        sort=kw.get("sort", "new"),
        page=kw.get("page", 1),
        page_size=kw.get("page_size", 24),
        database_url=site_db,
    )


@pytest.mark.asyncio
async def test_list_public_excludes_private(site_db: str) -> None:
    items, total = await _list(site_db)
    ids = {v.id for v in items}
    # site_listing 4 is is_public=False -> excluded.
    assert 4 not in ids
    assert total == 3
    assert all(isinstance(v, SiteListingView) for v in items)


@pytest.mark.asyncio
async def test_list_public_default_sort_new(site_db: str) -> None:
    items, _ = await _list(site_db)
    # newest posted_at first. id=1 has published_at 2026-06-10; ids 2,3 use created_at (06,07).
    # So order by posted_at desc: id1(06-10), id3(06-07), id2(06-06).
    assert [v.id for v in items] == [1, 3, 2]


@pytest.mark.asyncio
async def test_list_public_sort_price(site_db: str) -> None:
    asc, _ = await _list(site_db, sort="price_asc")
    assert [v.price_eur for v in asc] == [500, 600, 800]
    desc, _ = await _list(site_db, sort="price_desc")
    assert [v.price_eur for v in desc] == [800, 600, 500]


@pytest.mark.asyncio
async def test_filter_vnj(site_db: str) -> None:
    items, total = await _list(site_db, vnj=True)
    assert {v.id for v in items} == {1}
    assert total == 1
    assert items[0].for_vnj is True


@pytest.mark.asyncio
async def test_filter_white_card(site_db: str) -> None:
    items, _total = await _list(site_db, wc=True)
    assert {v.id for v in items} == {2}
    assert items[0].for_white_card is True


@pytest.mark.asyncio
async def test_filter_district_cyrillic(site_db: str) -> None:
    items, _ = await _list(site_db, district=["Врачар"])
    # listings 1 and 3 are Vracar (display Врачар).
    assert {v.id for v in items} == {1, 3}


@pytest.mark.asyncio
async def test_filter_price_range(site_db: str) -> None:
    items, _ = await _list(site_db, price_min=550, price_max=850)
    assert {v.price_eur for v in items} == {600, 800}


@pytest.mark.asyncio
async def test_filter_rooms_multi(site_db: str) -> None:
    items, _ = await _list(site_db, rooms=["studio", "1"])
    # studio = rooms 0 (listing 3), "1" = rooms 1 (listing 2).
    assert {v.id for v in items} == {2, 3}


@pytest.mark.asyncio
async def test_filter_rooms_3plus(site_db: str) -> None:
    # listing 4 has rooms 3 but is private; among public none has rooms>=3.
    items, _ = await _list(site_db, rooms=["3+"])
    assert items == []


@pytest.mark.asyncio
async def test_pagination(site_db: str) -> None:
    page1, total = await _list(site_db, page=1, page_size=2)
    page2, _ = await _list(site_db, page=2, page_size=2)
    assert total == 3
    assert len(page1) == 2
    assert len(page2) == 1
    assert {v.id for v in page1}.isdisjoint({v.id for v in page2})


@pytest.mark.asyncio
async def test_rented_included_and_flagged(site_db: str) -> None:
    items, _ = await _list(site_db)
    by_id = {v.id for v in items}
    assert 3 in by_id  # rented but NOT hidden
    rented = next(v for v in items if v.id == 3)
    assert rented.is_rented is True
    active = next(v for v in items if v.id == 1)
    assert active.is_rented is False


@pytest.mark.asyncio
async def test_view_field_mapping(site_db: str) -> None:
    items, _ = await _list(site_db, vnj=True)
    v = items[0]
    assert v.id == 1
    assert v.price_eur == 600
    assert v.area_m2 == 45.0
    assert v.rooms == "2+1"
    assert v.district == "Врачар"
    assert v.lat == 44.8 and v.lon == 20.47
    assert v.address is None  # district-only MVP (DECISIONS #1)
    assert v.channel_url == "https://t.me/RentBelgrad"  # default, no per-listing url
    # photos are public /photos/... paths, never remote.
    assert v.photos and all(p.startswith("/photos/") for p in v.photos)
    assert v.posted_at.startswith("2026-06-10")  # from published_at


@pytest.mark.asyncio
async def test_note_override(site_db: str) -> None:
    items, _ = await _list(site_db)
    v = next(x for x in items if x.id == 3)
    assert v.note == "Дарья: торг"


@pytest.mark.asyncio
async def test_get_public_returns_view(site_db: str) -> None:
    v = await rm.get_public(1, database_url=site_db)
    assert v is not None and v.id == 1


@pytest.mark.asyncio
async def test_get_public_none_for_private(site_db: str) -> None:
    assert await rm.get_public(4, database_url=site_db) is None


@pytest.mark.asyncio
async def test_get_public_none_for_missing(site_db: str) -> None:
    assert await rm.get_public(999, database_url=site_db) is None


@pytest.mark.asyncio
async def test_facets(site_db: str) -> None:
    f = await rm.facets(database_url=site_db)
    assert isinstance(f, Facets)
    districts = dict(f.districts)
    assert districts.get("Врачар") == 2
    assert districts.get("Нови Београд") == 1
    assert f.price_bounds == (500, 800)
    assert f.counts["vnj"] == 1
    assert f.counts["white_card"] == 1
    assert f.counts["total"] == 3


@pytest.mark.asyncio
async def test_map_points_public_with_coords_only(site_db: str) -> None:
    points = await rm.map_points(database_url=site_db)
    ids = {p["id"] for p in points}
    # listing 2 has no coords -> excluded; listing 4 private -> excluded.
    assert ids == {1, 3}
    p = next(x for x in points if x["id"] == 1)
    assert set(p.keys()) == {"id", "lat", "lon", "price_eur", "rooms", "district", "for_vnj", "url"}
    assert p["url"].endswith("/apt/1")
    assert "http" not in p["url"] or p["url"].startswith("http")  # url may be relative


@pytest.mark.asyncio
async def test_similar_same_district_excluding_self(site_db: str) -> None:
    base = await rm.get_public(1, database_url=site_db)
    assert base is not None
    sims = await rm.similar(base, limit=4, database_url=site_db)
    ids = {v.id for v in sims}
    assert 1 not in ids  # excludes self
    assert ids == {3}  # only other public Vracar listing


# --- A4: cross-platform dedup — catalog hides non-primary group members ---


@pytest.mark.asyncio
async def test_catalog_hides_non_primary_dupes(tmp_path) -> None:
    """Two public site_listings whose underlying listings are the same dup group: only the
    is_primary listing shows in the catalog (one card per apartment, not two)."""
    from datetime import UTC, datetime

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from src.db.models import (
        Base, InboundPost, Listing, Match, MatchStatus, Platform, SiteListing, SourceChannel,
    )

    def _dt(d: int) -> datetime:
        return datetime(2026, 6, d, 12, 0, tzinfo=UTC)

    db_path = tmp_path / "dup.db"
    url = f"sqlite+aiosqlite:///{db_path}"
    engine = create_async_engine(url, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(SourceChannel(id=1, tg_id=111))
        await s.flush()
        # Listings 1 & 2: same dup group (gid=1). 1 is primary, 2 is demoted.
        s.add(Listing(id=1, platform=Platform.NEKRETNINE, external_id="e1", url="http://x/1",
                      price_eur=600, district="Vracar", photo_urls=["u"], dup_group_id=1,
                      is_primary=True))
        s.add(Listing(id=2, platform=Platform.HALOOGLASI, external_id="e2", url="http://x/2",
                      price_eur=650, district="Vracar", photo_urls=["u"], dup_group_id=1,
                      is_primary=False))
        for lid in (1, 2):
            s.add(InboundPost(id=lid, source_channel_id=1, tg_message_id=100 + lid,
                              raw_text="", photo_paths=[f"data/inbound/1/{lid}/00.jpg"]))
        await s.flush()
        for mid in (1, 2):
            s.add(Match(id=mid, inbound_post_id=mid, listing_id=mid, status=MatchStatus.PUBLISHED))
        await s.flush()
        s.add(SiteListing(id=1, match_id=1, is_public=True, created_at=_dt(5), updated_at=_dt(5)))
        s.add(SiteListing(id=2, match_id=2, is_public=True, created_at=_dt(6), updated_at=_dt(6)))
        await s.commit()
    await engine.dispose()

    items, total = await rm.list_public(Filters(), database_url=url)
    ids = {v.id for v in items}
    # The non-primary duplicate (site_listing 2) is hidden; only the primary (1) shows.
    assert ids == {1}
    assert total == 1
    # get_public on the hidden non-primary also returns None.
    assert await rm.get_public(2, database_url=url) is None
