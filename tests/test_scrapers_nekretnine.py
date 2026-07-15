"""Unit tests for the nekretnine.rs scraper.

These hit only the pure parsers (parse_list, parse_detail) against stored HTML fixtures —
no network involved. Fixtures are captured from the post-migration Next.js site (data in the
`__NEXT_DATA__` JSON blob).
"""

from __future__ import annotations

import json
import re

import httpx
import pytest

import src.scrapers.nekretnine as nekretnine_mod
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


def test_parse_list_uses_canonical_numeric_url(nekretnine_list_html: str) -> None:
    previews = NekretnineScraper.parse_list(nekretnine_list_html)
    assert previews, "list page should yield previews"
    for p in previews:
        # URL must be slug-independent: /oglasi/<digits>/ with NOTHING after the id. The
        # platform periodically rewrites slugs, so a stored slug-URL 404s while the listing
        # is still live; the canonical numeric form is immune.
        assert re.fullmatch(r"https://www\.nekretnine\.rs/oglasi/\d+/", p.url), p.url


def test_parse_list_drops_slug_url_in_favour_of_canonical() -> None:
    # seo.url is a SLUG form — the parser must ignore it and emit the canonical numeric URL,
    # keeping the slug anchor only as the title.
    html = _list_html_slug([468187])
    previews = NekretnineScraper.parse_list(html)
    assert len(previews) == 1
    assert previews[0].url == "https://www.nekretnine.rs/oglasi/468187/"
    assert previews[0].external_id == "468187"


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


def _list_html(ids: list[int], max_pages: int = 99, total_ads: int | None = None) -> str:
    results = [
        {
            "realEstate": {"id": i, "title": f"Stan {i}"},
            "seo": {"url": f"https://www.nekretnine.rs/oglasi/{i}/", "anchor": f"Stan {i}"},
        }
        for i in ids
    ]
    payload: dict = {"results": results, "maxPages": max_pages}
    if total_ads is not None:
        payload["totalAds"] = total_ads
    data = {
        "props": {
            "pageProps": {
                "dehydratedState": {
                    "queries": [{"state": {"data": payload}}]
                }
            }
        }
    }
    blob = json.dumps(data)
    return f'<html><body><script id="__NEXT_DATA__" type="application/json">{blob}</script></body></html>'


def _list_html_slug(ids: list[int]) -> str:
    """Like _list_html but seo.url is the SLUG form the platform actually serves."""
    results = [
        {
            "realEstate": {"id": i, "title": f"Stan {i}"},
            "seo": {
                "url": f"https://www.nekretnine.rs/oglasi/{i}/dvosobni-stan-obrenovac/",
                "anchor": f"Stan {i}",
            },
        }
        for i in ids
    ]
    data = {
        "props": {
            "pageProps": {
                "dehydratedState": {"queries": [{"state": {"data": {"results": results}}}]}
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
async def test_list_recent_segments_by_price_when_capped() -> None:
    """Full walk (max_pages > page_cap) must fall back to price bands when the site caps the
    unfiltered search — the bug that kept the nightly at ~2000 of ~7700 listings."""
    band_requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.params
        page = int(p.get("pag", "1"))
        if "prezzoMinimo" in p:
            band_requests.append(f"{p.get('prezzoMinimo')}-{p.get('prezzoMassimo', 'inf')}")
            if page > 1:
                return httpx.Response(404)
            band_ids = {"0": [101, 102, 103], "500": [20, 201, 202]}  # 20 overlaps the base walk
            return httpx.Response(200, text=_list_html(band_ids[p["prezzoMinimo"]], total_ads=3))
        if page > 2:
            return httpx.Response(404)  # the site's pagination cap
        ids = [page * 10 + i for i in range(5)]  # page1: 10..14, page2: 20..24
        return httpx.Response(200, text=_list_html(ids, total_ads=120))

    sc = _make_scraper(handler)
    sc.page_cap = 2
    sc.price_bands = ((0, 500), (500, None))
    previews = await sc.list_recent(max_pages=500)

    ids = {p.external_id for p in previews}
    # 10 unfiltered + 3 + 3 from bands, minus the duplicate "20" = 15 unique.
    assert len(previews) == len(ids) == 15
    assert {"101", "102", "103", "201", "202"} <= ids
    assert sc._site_total == 120
    assert sorted(set(band_requests)) == ["0-500", "500-inf"], "both bands must be walked"
    assert sc._skipped_pages == 0, "the cap 404 is end-of-walk, not a skipped page"


@pytest.mark.asyncio
async def test_band_walk_continues_past_all_duplicate_page() -> None:
    """REGRESSION: a band's first page often fully overlaps the unfiltered pass (newest listings
    appear in both). That page must NOT be mistaken for end-of-pagination — the original
    segmentation rollout collected +0 from every band because of exactly this."""
    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.params
        page = int(p.get("pag", "1"))
        if "prezzoMinimo" in p:
            # Band page 1 = pure overlap with the base walk; page 2 holds the new tail.
            ids = [10, 11, 12, 13, 14] if page == 1 else [900, 901]
            return httpx.Response(200, text=_list_html(ids, max_pages=2, total_ads=7))
        if page > 2:
            return httpx.Response(404)
        ids = [page * 10 + i for i in range(5)]
        return httpx.Response(200, text=_list_html(ids, total_ads=50))

    sc = _make_scraper(handler)
    sc.page_cap = 2
    sc.price_bands = ((0, None),)
    previews = await sc.list_recent(max_pages=500)
    ids = {p.external_id for p in previews}
    assert {"900", "901"} <= ids, "band walk must continue past the all-duplicate first page"
    assert len(ids) == 12  # 10 base + 2 band tail


@pytest.mark.asyncio
async def test_walk_stops_when_site_ignores_pag_param() -> None:
    """currentPage mismatch (site re-serving page 1 for every ?pag=N) must stop the walk."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        # Always serves currentPage=1 regardless of ?pag — the original migration bug class.
        data = json.loads(_list_html([1, 2, 3], total_ads=500).split('json">')[1].split("</script>")[0])
        data["props"]["pageProps"]["dehydratedState"]["queries"][0]["state"]["data"]["currentPage"] = 1
        blob = json.dumps(data)
        return httpx.Response(
            200,
            text=f'<html><script id="__NEXT_DATA__" type="application/json">{blob}</script></html>',
        )

    sc = _make_scraper(handler)
    previews = await sc.list_recent(max_pages=50)
    assert {p.external_id for p in previews} == {"1", "2", "3"}
    assert calls["n"] == 2, "walk must stop right after detecting the page-2 mismatch"


@pytest.mark.asyncio
async def test_list_recent_incremental_never_segments() -> None:
    """Small max_pages (the 15-min incremental path) must stay a single cheap walk."""
    saw_band_request = {"v": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if "prezzoMinimo" in request.url.params:
            saw_band_request["v"] = True
        page = int(request.url.params.get("pag", "1"))
        return httpx.Response(200, text=_list_html([page * 10 + i for i in range(3)], total_ads=99999))

    sc = _make_scraper(handler)
    await sc.list_recent(max_pages=2)
    assert saw_band_request["v"] is False


@pytest.mark.asyncio
async def test_fetch_page_404_short_circuits_without_retries() -> None:
    """404 = past the pagination cap — semantic stop, not a transient failure to retry."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("pag", "1"))
        if page >= 2:
            calls["n"] += 1
            return httpx.Response(404)
        return httpx.Response(200, text=_list_html([1, 2, 3]))

    sc = _make_scraper(handler)
    previews = await sc.list_recent(max_pages=10)
    assert {p.external_id for p in previews} == {"1", "2", "3"}
    assert calls["n"] == 1, "404 must not be retried"
    assert sc._skipped_pages == 0


@pytest.mark.asyncio
async def test_list_recent_breaks_on_missing_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # 200 OK but no __NEXT_DATA__ list payload → site-structure-change signal, stop the walk.
        return httpx.Response(200, text="<html><body>no next data here</body></html>")

    sc = _make_scraper(handler)
    previews = await sc.list_recent(max_pages=10)
    assert previews == []
    assert sc._skipped_pages == 0, "a 200 with no payload is a structure change, not a fetch failure"


# ------------------------------------------------------------------ fetch_detail (offline retries)
# fetch_detail must mirror _fetch_page's resilience: retry transient failures (5xx, 429, transport
# errors) but raise IMMEDIATELY on a deterministic 4xx (403 anti-bot block, 404/410 gone) — retrying
# a hard block only burns the IP's reputation and the backoff budget. The 13.07 incident was exactly
# a bare fetch_detail 403-ing and taking the whole smoke probe down with it.

_DETAIL_URL = "https://www.nekretnine.rs/oglasi/468187/"


@pytest.mark.asyncio
async def test_fetch_detail_retries_transient_then_succeeds(nekretnine_detail_html: str) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, text="slow down")  # transient → must retry
        return httpx.Response(200, text=nekretnine_detail_html)

    sc = _make_scraper(handler)  # retry_backoff_sec=0 → retries but never really sleeps
    detail = await sc.fetch_detail(_DETAIL_URL)
    assert detail.external_id == "468187"
    assert calls["n"] == 2, "429 is transient — retried, second attempt succeeds"


@pytest.mark.asyncio
async def test_fetch_detail_raises_after_exhausting_retries_on_5xx() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, text="boom")  # every attempt 5xx

    sc = _make_scraper(handler)
    with pytest.raises(httpx.HTTPStatusError):
        await sc.fetch_detail(_DETAIL_URL)
    assert calls["n"] == sc.fetch_attempts == 3, "5xx retried up to fetch_attempts, then raises"


@pytest.mark.asyncio
async def test_fetch_detail_403_raises_immediately_without_retry(monkeypatch) -> None:
    from src.config import get_settings

    # This test is about the httpx retry policy, not the D6 browser fallback — switch it off.
    monkeypatch.setattr(get_settings(), "nekretnine_playwright_fallback", False)
    calls = {"n": 0}
    slept: list[float] = []

    async def _spy_sleep(sec: float) -> None:
        slept.append(sec)

    monkeypatch.setattr(nekretnine_mod.asyncio, "sleep", _spy_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(403, text="Forbidden")

    sc = _make_scraper(handler)
    sc.retry_backoff_sec = 2.0  # non-zero: prove the block, not a zero backoff, is why we don't sleep
    with pytest.raises(httpx.HTTPStatusError) as ei:
        await sc.fetch_detail(_DETAIL_URL)
    assert ei.value.response.status_code == 403
    assert calls["n"] == 1, "403 is deterministic — must not be retried over httpx"
    assert slept == [], "no backoff sleep on an immediate 403 raise"


# ------------------------------------------------- D6: Playwright fallback for the DataDome 403
# nekretnine is behind DataDome: httpx gets 403 on DETAIL pages (TLS/JS fingerprint) while the LIST
# still returns 200. A real browser (Playwright + stealth — already a dependency, same trick
# halooglasi uses against Cloudflare) gets 200, so a 403 refetches that one page through it.
# The browser is mocked here; the launch path is exercised on the VPS, not in unit tests.


@pytest.mark.asyncio
async def test_fetch_detail_403_falls_back_to_playwright(nekretnine_detail_html: str) -> None:
    calls = {"n": 0}
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(403, text="Forbidden")

    sc = _make_scraper(handler)

    async def fake_pw(url: str) -> str:
        seen["url"] = url
        return nekretnine_detail_html

    sc._playwright_html = fake_pw  # type: ignore[method-assign]
    detail = await sc.fetch_detail(_DETAIL_URL)

    assert detail.external_id == "468187", "the browser HTML parses exactly like the httpx HTML"
    assert seen["url"] == _DETAIL_URL
    assert calls["n"] == 1, "one httpx try, then straight to the browser — no httpx retry on 403"


@pytest.mark.asyncio
async def test_fetch_detail_403_raises_when_browser_also_blocked() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    sc = _make_scraper(handler)

    async def fake_pw(url: str) -> None:
        return None  # browser blocked too → keep the original 403 contract

    sc._playwright_html = fake_pw  # type: ignore[method-assign]
    with pytest.raises(httpx.HTTPStatusError) as ei:
        await sc.fetch_detail(_DETAIL_URL)
    assert ei.value.response.status_code == 403


@pytest.mark.asyncio
async def test_fetch_detail_403_skips_browser_when_disabled(monkeypatch) -> None:
    from src.config import get_settings

    monkeypatch.setattr(get_settings(), "nekretnine_playwright_fallback", False)
    calls = {"pw": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    sc = _make_scraper(handler)

    async def fake_pw(url: str) -> str:
        calls["pw"] += 1
        return "<html/>"

    sc._playwright_html = fake_pw  # type: ignore[method-assign]
    with pytest.raises(httpx.HTTPStatusError):
        await sc.fetch_detail(_DETAIL_URL)
    assert calls["pw"] == 0, "kill switch → never start a browser"


@pytest.mark.asyncio
async def test_fetch_detail_404_does_not_use_browser() -> None:
    """404 = the ad is genuinely gone; only a 403 (the DataDome block) is worth a browser."""
    calls = {"pw": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    sc = _make_scraper(handler)

    async def fake_pw(url: str) -> str:
        calls["pw"] += 1
        return "<html/>"

    sc._playwright_html = fake_pw  # type: ignore[method-assign]
    with pytest.raises(httpx.HTTPStatusError):
        await sc.fetch_detail(_DETAIL_URL)
    assert calls["pw"] == 0


@pytest.mark.asyncio
async def test_fetch_detail_200_never_starts_browser(nekretnine_detail_html: str) -> None:
    """The happy path must never pay the browser cost (seconds per page)."""
    calls = {"pw": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=nekretnine_detail_html)

    sc = _make_scraper(handler)

    async def fake_pw(url: str) -> None:
        calls["pw"] += 1
        return None

    sc._playwright_html = fake_pw  # type: ignore[method-assign]
    await sc.fetch_detail(_DETAIL_URL)
    assert calls["pw"] == 0


@pytest.mark.asyncio
async def test_fetch_detail_404_raises_immediately_without_retry() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404)

    sc = _make_scraper(handler)
    with pytest.raises(httpx.HTTPStatusError) as ei:
        await sc.fetch_detail(_DETAIL_URL)
    assert ei.value.response.status_code == 404
    assert calls["n"] == 1, "404 gone — must not be retried"


@pytest.mark.asyncio
async def test_fetch_detail_sends_referer(nekretnine_detail_html: str) -> None:
    """T1-4: a detail GET must carry a same-site Referer (the list URL) — a Refererless deep-link to
    an oglas is exactly the shape an anti-bot layer flags."""
    captured: dict[str, httpx.Headers] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = request.headers
        return httpx.Response(200, text=nekretnine_detail_html)

    sc = _make_scraper(handler)
    await sc.fetch_detail(_DETAIL_URL)
    assert captured["headers"].get("referer") == sc.list_url
    assert captured["headers"].get("sec-fetch-site") == "same-origin"
