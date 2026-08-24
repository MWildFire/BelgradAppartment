"""Tests for the liveness sweep (A3): verify_active_links.

`verify_active_links` HEAD-checks the URLs of ACTIVE listings that have a PENDING or
PUBLISHED match (i.e. potentially shown on the site) and deactivates the ones that are
definitively gone (404/410). Transient failures (timeouts, 5xx) and anti-bot responses
(403/405) must NOT deactivate — only definitive gone-statuses do.

DB setup mirrors tests/test_orchestrator_reseen.py: an in-memory SQLite swapped into the
global session factory. HTTP is faked with httpx.MockTransport — no network.
"""

from __future__ import annotations

import itertools

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import src.db.session as db_session
import src.scrapers.orchestrator as orch
from src.db.models import (
    Base,
    InboundPost,
    InboundStatus,
    Listing,
    Match,
    MatchStatus,
    Platform,
    SiteListing,
    SourceChannel,
)
from src.scrapers.orchestrator import hide_site_listings_for_listing, verify_active_links

_SEED_COUNTER = itertools.count(1)


@pytest_asyncio.fixture
async def factory():
    """In-memory DB swapped into the global session factory; yields the factory."""
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


async def _seed_listing(
    factory,
    *,
    url: str,
    external_id: str = "ext1",
    platform: Platform = Platform.NEKRETNINE,
    is_active: bool = True,
    with_match: bool = True,
    match_status: MatchStatus = MatchStatus.PENDING,
    site_listing: bool = False,
    site_public: bool = True,
    site_featured: bool = False,
) -> int:
    """Insert a listing (and optionally a match + SiteListing overlay); return the listing id.

    `site_listing=True` attaches a SiteListing to the created match (requires with_match),
    so X2 tests can assert the public card is hidden when the listing is deactivated.
    """
    async with factory() as s:
        listing = Listing(
            platform=platform,
            external_id=external_id,
            url=url,
            photo_urls=[],
            photo_phashes=[],
            is_active=is_active,
        )
        s.add(listing)
        await s.flush()
        listing_id = listing.id
        if with_match:
            n = next(_SEED_COUNTER)
            ch = SourceChannel(tg_id=-n, username=f"srcchan-{n}", is_active=True)
            s.add(ch)
            await s.flush()
            ib = InboundPost(
                source_channel_id=ch.id,
                tg_message_id=n,
                photo_paths=[],
                photo_phashes=[],
                status=InboundStatus.MATCHED,
            )
            s.add(ib)
            await s.flush()
            match = Match(
                inbound_post_id=ib.id,
                listing_id=listing_id,
                confidence=0.9,
                matched_photo_count=1,
                method="phash",
                status=match_status,
            )
            s.add(match)
            await s.flush()
            if site_listing:
                s.add(
                    SiteListing(
                        match_id=match.id,
                        is_public=site_public,
                        is_featured=site_featured,
                    )
                )
        await s.commit()
    return listing_id


async def _site_state(factory, listing_id: int) -> tuple[bool, bool] | None:
    """Return (is_public, is_featured) for the SiteListing on `listing_id`'s match, or None."""
    async with factory() as s:
        row = (
            await s.execute(
                select(SiteListing)
                .join(Match, Match.id == SiteListing.match_id)
                .where(Match.listing_id == listing_id)
            )
        ).scalar_one_or_none()
        return None if row is None else (row.is_public, row.is_featured)


def _client(handler) -> httpx.AsyncClient:
    """An AsyncClient backed by a MockTransport with follow_redirects, like the real one."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)


async def _is_active(factory, listing_id: int) -> bool:
    async with factory() as s:
        row = (await s.execute(select(Listing).where(Listing.id == listing_id))).scalar_one()
        return row.is_active


@pytest.mark.asyncio
async def test_404_deactivates(factory):
    lid = await _seed_listing(factory, url="https://nekretnine.rs/gone/")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    stats = await verify_active_links(client=_client(handler))

    assert stats.checked == 1
    assert stats.deactivated == 1
    assert stats.errors == 0
    assert await _is_active(factory, lid) is False


@pytest.mark.asyncio
async def test_410_deactivates(factory):
    lid = await _seed_listing(factory, url="https://nekretnine.rs/gone/")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(410)

    stats = await verify_active_links(client=_client(handler))

    assert stats.deactivated == 1
    assert await _is_active(factory, lid) is False


@pytest.mark.asyncio
async def test_200_stays_active(factory):
    lid = await _seed_listing(factory, url="https://nekretnine.rs/live/")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    stats = await verify_active_links(client=_client(handler))

    assert stats.checked == 1
    assert stats.deactivated == 0
    assert stats.errors == 0
    assert await _is_active(factory, lid) is True


@pytest.mark.asyncio
async def test_redirect_to_200_stays_active(factory):
    """301 → canonical that lands on 200 is fine (follow_redirects handles it)."""
    lid = await _seed_listing(factory, url="https://nekretnine.rs/old/")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/old/":
            return httpx.Response(301, headers={"Location": "https://nekretnine.rs/new/"})
        return httpx.Response(200)

    stats = await verify_active_links(client=_client(handler))

    assert stats.deactivated == 0
    assert stats.errors == 0
    assert await _is_active(factory, lid) is True


@pytest.mark.asyncio
async def test_timeout_does_not_deactivate(factory):
    """Transient failure (timeout) must count as an error but never nuke a live listing."""
    lid = await _seed_listing(factory, url="https://nekretnine.rs/slow/")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out", request=request)

    stats = await verify_active_links(client=_client(handler))

    assert stats.deactivated == 0
    assert stats.errors == 1
    assert await _is_active(factory, lid) is True


@pytest.mark.asyncio
async def test_500_does_not_deactivate(factory):
    """A 5xx is a server hiccup, not a definitive gone-status — keep the listing active."""
    lid = await _seed_listing(factory, url="https://nekretnine.rs/oops/")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    stats = await verify_active_links(client=_client(handler))

    assert stats.deactivated == 0
    assert stats.errors == 1
    assert await _is_active(factory, lid) is True


@pytest.mark.asyncio
async def test_anti_bot_405_403_does_not_deactivate(factory):
    """A bare HEAD against a Cloudflare-fronted platform may 405/403 — inconclusive, never
    deactivate on an anti-bot response."""
    lid405 = await _seed_listing(
        factory, url="https://halooglasi.com/a/", external_id="a"
    )
    lid403 = await _seed_listing(
        factory, url="https://halooglasi.com/b/", external_id="b"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(405 if request.url.path == "/a/" else 403)

    stats = await verify_active_links(client=_client(handler))

    assert stats.checked == 2
    assert stats.deactivated == 0
    assert stats.errors == 2
    assert await _is_active(factory, lid405) is True
    assert await _is_active(factory, lid403) is True


@pytest.mark.asyncio
async def test_listing_without_match_not_checked(factory):
    """A listing with no match is not potentially on the site — must be excluded."""
    lid = await _seed_listing(
        factory, url="https://nekretnine.rs/nomatch/", with_match=False
    )

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(404)

    stats = await verify_active_links(client=_client(handler))

    assert stats.checked == 0
    assert calls == []
    assert await _is_active(factory, lid) is True


@pytest.mark.asyncio
async def test_inactive_listing_not_checked(factory):
    """An already-inactive listing is excluded even if it has a match."""
    lid = await _seed_listing(
        factory, url="https://nekretnine.rs/dead/", is_active=False
    )

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(404)

    stats = await verify_active_links(client=_client(handler))

    assert stats.checked == 0
    assert calls == []


@pytest.mark.asyncio
async def test_platform_filter(factory):
    """--platform restricts the sweep to one platform."""
    nek = await _seed_listing(
        factory, url="https://nekretnine.rs/x/", external_id="n",
        platform=Platform.NEKRETNINE,
    )
    cz = await _seed_listing(
        factory, url="https://cetiri.example/y/", external_id="c",
        platform=Platform.CETIRI_ZIDA,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    stats = await verify_active_links(
        client=_client(handler), platform=Platform.NEKRETNINE
    )

    assert stats.checked == 1
    assert await _is_active(factory, nek) is False
    assert await _is_active(factory, cz) is True


@pytest.mark.asyncio
async def test_already_passed_deadline_skips_everything(factory):
    """A deadline already in the past (shared budget exhausted by heal/rolling) → no HEADs,
    every matched listing counted as skipped, nothing deactivated."""
    import time as _time

    lid = await _seed_listing(factory, url="https://nekretnine.rs/gone/")
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(404)

    stats = await verify_active_links(client=_client(handler), deadline=_time.monotonic() - 1)

    assert stats.checked == 0
    assert stats.skipped == 1
    assert stats.deactivated == 0
    assert calls == []
    assert await _is_active(factory, lid) is True


@pytest.mark.asyncio
async def test_future_deadline_checks_all(factory):
    """A deadline comfortably in the future does not cap the sweep — all rows checked, skipped=0."""
    import time as _time

    lid = await _seed_listing(factory, url="https://nekretnine.rs/gone/")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    stats = await verify_active_links(client=_client(handler), deadline=_time.monotonic() + 3600)

    assert stats.checked == 1
    assert stats.skipped == 0
    assert stats.deactivated == 1


@pytest.mark.asyncio
async def test_published_match_is_selected(factory):
    """A PUBLISHED match also counts as potentially on the site."""
    lid = await _seed_listing(
        factory, url="https://nekretnine.rs/pub/", match_status=MatchStatus.PUBLISHED
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    stats = await verify_active_links(client=_client(handler))

    assert stats.checked == 1
    assert await _is_active(factory, lid) is False


# --------------------------------------------------------------------------- X2: auto-hide


@pytest.mark.asyncio
async def test_hide_helper_hides_public_card(factory):
    """The pure hide helper flips is_public/is_featured False for a listing's public cards."""
    import src.db.session as db_session

    lid = await _seed_listing(
        factory, url="https://nekretnine.rs/x/", site_listing=True,
        site_public=True, site_featured=True,
    )
    async with db_session.get_session() as s:
        n = await hide_site_listings_for_listing(s, lid)
    assert n == 1
    assert await _site_state(factory, lid) == (False, False)


@pytest.mark.asyncio
async def test_hide_helper_invalidates_cache_when_it_hides(factory):
    """Hiding a card must bust the in-process read-model cache so it disappears at once."""
    import src.db.session as db_session
    from src.site import cache

    lid = await _seed_listing(
        factory, url="https://nekretnine.rs/x/", site_listing=True, site_public=True,
    )
    cache.catalog_cache.set(("k",), "v")
    async with db_session.get_session() as s:
        await hide_site_listings_for_listing(s, lid)
    assert cache.catalog_cache.get(("k",)) is cache.MISS


@pytest.mark.asyncio
async def test_hide_helper_no_hide_keeps_cache(factory):
    """No rows hidden (already hidden) → leave the cache intact."""
    import src.db.session as db_session
    from src.site import cache

    lid = await _seed_listing(
        factory, url="https://nekretnine.rs/x/", site_listing=True, site_public=False,
    )
    cache.catalog_cache.set(("k",), "v")
    async with db_session.get_session() as s:
        await hide_site_listings_for_listing(s, lid)
    assert cache.catalog_cache.get(("k",)) == "v"


@pytest.mark.asyncio
async def test_hide_helper_idempotent_on_already_hidden(factory):
    """A card already hidden (is_public False) yields rowcount 0 — no churn."""
    import src.db.session as db_session

    lid = await _seed_listing(
        factory, url="https://nekretnine.rs/x/", site_listing=True, site_public=False,
    )
    async with db_session.get_session() as s:
        n = await hide_site_listings_for_listing(s, lid)
    assert n == 0


@pytest.mark.asyncio
async def test_404_hides_public_card(factory):
    lid = await _seed_listing(
        factory, url="https://nekretnine.rs/gone/", site_listing=True, site_public=True,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    stats = await verify_active_links(client=_client(handler))

    assert stats.deactivated == 1
    assert stats.site_listings_hidden == 1
    assert await _is_active(factory, lid) is False
    assert await _site_state(factory, lid) == (False, False)


@pytest.mark.asyncio
async def test_410_hides_public_card(factory):
    lid = await _seed_listing(
        factory, url="https://nekretnine.rs/gone/", site_listing=True, site_public=True,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(410)

    stats = await verify_active_links(client=_client(handler))

    assert stats.deactivated == 1
    assert stats.site_listings_hidden == 1
    assert await _site_state(factory, lid) == (False, False)


@pytest.mark.asyncio
async def test_inconclusive_does_not_hide(factory):
    """5xx/timeout/403 keep the listing active AND the public card live; hidden==0."""
    lid500 = await _seed_listing(
        factory, url="https://x/500/", external_id="a", site_listing=True, site_public=True,
    )
    lid403 = await _seed_listing(
        factory, url="https://x/403/", external_id="b", site_listing=True, site_public=True,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503 if request.url.path == "/500/" else 403)

    stats = await verify_active_links(client=_client(handler))

    assert stats.deactivated == 0
    assert stats.site_listings_hidden == 0
    assert await _is_active(factory, lid500) is True
    assert await _site_state(factory, lid500) == (True, False)
    assert await _site_state(factory, lid403) == (True, False)


@pytest.mark.asyncio
async def test_404_without_site_listing_no_error(factory):
    """A deactivated listing with no SiteListing hides nothing and raises nothing."""
    lid = await _seed_listing(
        factory, url="https://nekretnine.rs/gone/", site_listing=False,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    stats = await verify_active_links(client=_client(handler))

    assert stats.deactivated == 1
    assert stats.site_listings_hidden == 0
    assert await _is_active(factory, lid) is False


@pytest.mark.asyncio
async def test_404_already_hidden_card_hidden_count_zero(factory):
    """Re-checking a 404 whose card is already hidden deactivates again but hides nothing."""
    lid = await _seed_listing(
        factory, url="https://nekretnine.rs/gone/", site_listing=True, site_public=False,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    stats = await verify_active_links(client=_client(handler))

    assert stats.deactivated == 1
    assert stats.site_listings_hidden == 0
    assert await _site_state(factory, lid) == (False, False)


@pytest.mark.asyncio
async def test_404_clears_featured_flag(factory):
    """A hidden card must never remain the hero — is_featured is cleared too."""
    lid = await _seed_listing(
        factory, url="https://nekretnine.rs/gone/", site_listing=True,
        site_public=True, site_featured=True,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    await verify_active_links(client=_client(handler))

    assert await _site_state(factory, lid) == (False, False)


# --------------------------------------------------------------- T1-5: proxy plumbing into HEADs


@pytest.mark.asyncio
async def test_make_side_client_uses_proxy_when_configured(monkeypatch):
    """T1-5 (factory): the orchestrator's side-client honors SCRAPER_PROXY_URL."""
    from src.config import get_settings

    monkeypatch.setattr(get_settings(), "scraper_proxy_url", "http://eu-proxy:3128")

    captured: dict = {}

    class _Spy:
        def __init__(self, **kw):
            captured.update(kw)

    monkeypatch.setattr(orch.httpx, "AsyncClient", _Spy)
    orch._make_side_client(headers={"User-Agent": "x"}, timeout=10.0, use_proxy=True)
    assert captured["proxy"] == "http://eu-proxy:3128"
    assert captured["headers"] == {"User-Agent": "x"}
    assert captured["follow_redirects"] is True


@pytest.mark.asyncio
async def test_make_side_client_no_proxy_when_unset(monkeypatch):
    from src.config import get_settings

    monkeypatch.setattr(get_settings(), "scraper_proxy_url", "")

    captured: dict = {}

    class _Spy:
        def __init__(self, **kw):
            captured.update(kw)

    monkeypatch.setattr(orch.httpx, "AsyncClient", _Spy)
    orch._make_side_client()
    assert captured["proxy"] is None


@pytest.mark.asyncio
async def test_verify_links_owned_client_routes_through_proxy(factory, monkeypatch):
    """Owned liveness client uses proxy only for Nekretnine URLs."""
    from src.config import get_settings

    monkeypatch.setattr(get_settings(), "scraper_proxy_url", "http://eu-proxy:3128")

    captured: dict = {}

    class _SpyClient:
        def __init__(self, **kw):
            captured.update(kw)

        async def head(self, url, **kw):
            return httpx.Response(200, request=httpx.Request("HEAD", url))

        async def aclose(self):
            return None

    monkeypatch.setattr(orch.httpx, "AsyncClient", _SpyClient)

    await _seed_listing(factory, url="https://nekretnine.rs/live/")
    stats = await verify_active_links()  # client=None → owns_client path builds via _make_side_client
    assert captured.get("proxy") == "http://eu-proxy:3128"
    assert stats.checked == 1


@pytest.mark.asyncio
async def test_verify_links_owned_client_bypasses_proxy_for_other_platform(factory, monkeypatch):
    """A direct source must not depend on the paid proxy during the weekly liveness sweep."""
    from src.config import get_settings

    monkeypatch.setattr(get_settings(), "scraper_proxy_url", "http://eu-proxy:3128")
    captured: list[dict] = []

    class _SpyClient:
        def __init__(self, **kw):
            captured.append(kw)

        async def head(self, url, **kw):
            return httpx.Response(200, request=httpx.Request("HEAD", url))

        async def aclose(self):
            return None

    monkeypatch.setattr(orch.httpx, "AsyncClient", _SpyClient)
    await _seed_listing(
        factory,
        url="https://cityexpert.rs/live/",
        external_id="cityexpert-ext1",
        platform=Platform.CITYEXPERT,
    )

    stats = await verify_active_links()

    assert len(captured) == 1
    assert captured[0]["proxy"] is None
    assert stats.checked == 1


@pytest.mark.asyncio
async def test_404_kill_switch_off_leaves_card_public(factory, monkeypatch):
    """NIGHTLY_AUTO_HIDE_SITE_LISTINGS=0 → 404 deactivates the listing but keeps the card public."""
    from src.config import get_settings

    monkeypatch.setattr(get_settings(), "nightly_auto_hide_site_listings", False)
    lid = await _seed_listing(
        factory, url="https://nekretnine.rs/gone/", site_listing=True, site_public=True,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    stats = await verify_active_links(client=_client(handler))

    assert stats.deactivated == 1
    assert stats.site_listings_hidden == 0
    assert await _is_active(factory, lid) is False
    assert await _site_state(factory, lid) == (True, False)  # card untouched
