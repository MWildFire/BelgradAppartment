"""Unit tests for the nekretnine.rs scraper.

These hit only the pure parsers (parse_list, parse_detail) against stored HTML fixtures —
no network involved. Fixtures are captured from the post-migration Next.js site (data in the
`__NEXT_DATA__` JSON blob).
"""

from __future__ import annotations

import json

import httpx
import pytest

from src.db.models import Platform
from src.scrapers.nekretnine import NekretnineScraper

# Detail fixture is /oglasi/468187/ — "Dvosobni stan Kralja Petra I, Obrenovac Centar, Beograd".
DETAIL_URL = "https://www.nekretnine.rs/oglasi/468187/"


def test_parse_list_returns_unique_listings(nekretnine_list_html: str) -> None:
    previews = NekretnineScraper.parse_list(nekretnine_list_html)
    assert len(previews) >= 5, "list page should yield several previews"
    ids = [p.external_id for p in previews]
    assert len(ids) == len(set(ids)), "external_ids must be unique"
    for p in previews:
        assert p.platform is Platform.NEKRETNINE
        assert "/oglasi/" in p.url
        assert p.external_id and p.external_id.isdigit()  # new IDs are numeric


def test_parse_list_handles_missing_payload() -> None:
    # No __NEXT_DATA__ → empty list, not a crash.
    assert NekretnineScraper.parse_list("<html><body>nope</body></html>") == []


def test_parse_detail_extracts_core_fields(nekretnine_detail_html: str) -> None:
    detail = NekretnineScraper.parse_detail(DETAIL_URL, nekretnine_detail_html)
    assert detail.platform is Platform.NEKRETNINE
    assert detail.external_id == "468187"
    assert detail.url == DETAIL_URL
    assert detail.price_eur == 350
    assert detail.area_m2 == 50.0
    assert detail.rooms == 2.0
    # coords from location.latitude / location.longitude
    assert detail.lat is not None and 44.5 < detail.lat < 44.95
    assert detail.lon is not None and 20.0 < detail.lon < 20.7
    assert detail.title and "stan" in detail.title.lower()
    assert detail.description and len(detail.description) > 20
    # district = microzone (falls back to macrozone)
    assert detail.district is not None and "Obrenovac" in detail.district


def test_parse_detail_photos_are_deduped_cdn_urls(nekretnine_detail_html: str) -> None:
    detail = NekretnineScraper.parse_detail(DETAIL_URL, nekretnine_detail_html)
    assert len(detail.photo_urls) >= 1
    # New CDN, no signed token.
    for u in detail.photo_urls:
        assert u.startswith("https://pic.nekretnine.rs/image/")
    assert len(detail.photo_urls) == len(set(detail.photo_urls)), "photos must be deduped"


def test_parse_detail_extracts_street_address(nekretnine_detail_html: str) -> None:
    detail = NekretnineScraper.parse_detail(DETAIL_URL, nekretnine_detail_html)
    assert detail.address_raw is not None
    assert "Kralja Petra" in detail.address_raw


def test_parse_detail_missing_payload_is_graceful() -> None:
    detail = NekretnineScraper.parse_detail(DETAIL_URL, "<html>no next data</html>")
    assert detail.platform is Platform.NEKRETNINE
    assert detail.external_id == "468187"
    assert detail.price_eur is None
    assert detail.photo_urls == []


def test_external_id_from_url() -> None:
    assert NekretnineScraper._external_id_from_url("https://www.nekretnine.rs/oglasi/468187/") == "468187"
    assert NekretnineScraper._external_id_from_url("https://www.nekretnine.rs/oglasi/468187/?x=1") == "468187"


def test_number_from_text_handles_locale_separators() -> None:
    # Dot is the THOUSANDS separator on this platform; comma is the decimal.
    assert NekretnineScraper._number_from_text("50 m²") == 50.0
    assert NekretnineScraper._number_from_text("1.250 m²") == 1250.0
    assert NekretnineScraper._number_from_text("10.000 m²") == 10000.0
    assert NekretnineScraper._number_from_text("50,5 m²") == 50.5
    assert NekretnineScraper._number_from_text("2") == 2.0
    assert NekretnineScraper._number_from_text(74) == 74.0
    assert NekretnineScraper._number_from_text(None) is None


def test_extract_next_data_tolerates_nonce_attribute() -> None:
    # CSP can add a nonce / reorder attributes — the regex must still match.
    html = '<script id="__NEXT_DATA__" nonce="abc123" type="application/json">{"a": 1}</script>'
    assert NekretnineScraper._extract_next_data(html) == {"a": 1}


# --------------------------------------------------------------------------- list_recent (offline)
# These drive the pagination/retry/abort logic that the rewrite exists to fix, using an injected
# MockTransport client — no network. (The original incident was exactly this layer failing silently.)


def _list_html(ids: list[int], max_pages: int = 99) -> str:
    results = [
        {
            "realEstate": {"id": i, "title": f"Stan {i}"},
            "seo": {"url": f"https://www.nekretnine.rs/oglasi/{i}/", "anchor": f"Stan {i}"},
        }
        for i in ids
    ]
    data = {
        "props": {
            "pageProps": {
                "dehydratedState": {
                    "queries": [{"state": {"data": {"results": results, "maxPages": max_pages}}}]
                }
            }
        }
    }
    blob = json.dumps(data)
    return f'<html><body><script id="__NEXT_DATA__" type="application/json">{blob}</script></body></html>'


def _make_scraper(handler) -> NekretnineScraper:
    sc = NekretnineScraper(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    # Make tests fast: no pacing / backoff sleeps.
    sc.page_delay_sec = 0.0
    sc.retry_backoff_sec = 0.0
    return sc


def _page_of(request: httpx.Request) -> int:
    return int(request.url.params.get("pag", "1"))


@pytest.mark.asyncio
async def test_list_recent_paginates_and_dedupes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        page = _page_of(request)
        # page1: 1..25, page2: 25..49 (id 25 overlaps → must dedupe). maxPages bounds at 2.
        ids = list(range(1, 26)) if page == 1 else list(range(25, 50))
        return httpx.Response(200, text=_list_html(ids, max_pages=2))

    sc = _make_scraper(handler)
    previews = await sc.list_recent(max_pages=10)
    ids = [p.external_id for p in previews]
    assert len(ids) == len(set(ids)), "must dedupe across pages"
    assert len(ids) == 49, "1..49 unique after overlap dedupe"
    assert sc._skipped_pages == 0


@pytest.mark.asyncio
async def test_list_recent_stops_at_site_max_pages() -> None:
    fetched: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = _page_of(request)
        fetched.append(page)
        return httpx.Response(200, text=_list_html([page * 1000 + i for i in range(5)], max_pages=2))

    sc = _make_scraper(handler)
    await sc.list_recent(max_pages=50)
    assert fetched == [1, 2], "must not fetch beyond the site-reported maxPages"


@pytest.mark.asyncio
async def test_list_recent_breaks_on_empty_page() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        page = _page_of(request)
        ids = [1, 2, 3] if page == 1 else []
        return httpx.Response(200, text=_list_html(ids, max_pages=99))

    sc = _make_scraper(handler)
    previews = await sc.list_recent(max_pages=10)
    assert {p.external_id for p in previews} == {"1", "2", "3"}


@pytest.mark.asyncio
async def test_list_recent_skips_isolated_failure_but_continues() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        page = _page_of(request)
        if page == 2:
            return httpx.Response(500, text="boom")  # all attempts on page 2 fail
        ids = [page * 10] if page <= 3 else []
        return httpx.Response(200, text=_list_html(ids, max_pages=99))

    sc = _make_scraper(handler)
    previews = await sc.list_recent(max_pages=10)
    # page1 (10) + page3 (30); page2 skipped, not aborted.
    assert {p.external_id for p in previews} == {"10", "30"}
    assert sc._skipped_pages == 1, "skipped page must be counted for observability"


@pytest.mark.asyncio
async def test_list_recent_aborts_after_consecutive_failures() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, text="down")  # every page fails

    sc = _make_scraper(handler)
    previews = await sc.list_recent(max_pages=100)
    assert previews == []
    assert sc._skipped_pages == sc.max_consecutive_failures == 3
    # 3 skipped pages × fetch_attempts each, then abort — must not walk all 100 pages.
    assert calls["n"] == sc.max_consecutive_failures * sc.fetch_attempts


@pytest.mark.asyncio
async def test_list_recent_breaks_on_missing_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # 200 OK but no __NEXT_DATA__ list payload → site-structure-change signal, stop the walk.
        return httpx.Response(200, text="<html><body>no next data here</body></html>")

    sc = _make_scraper(handler)
    previews = await sc.list_recent(max_pages=10)
    assert previews == []
    assert sc._skipped_pages == 0, "a 200 with no payload is a structure change, not a fetch failure"
