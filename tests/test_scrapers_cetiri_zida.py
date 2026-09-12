"""Unit tests for the 4zida.rs scraper (JSON-LD parsing)."""

from __future__ import annotations

import json

import httpx
import pytest

from src.db.models import Platform
from src.scrapers.cetiri_zida import CetiriZidaScraper

DETAIL_URL = (
    "https://www.4zida.rs/izdavanje-stanova/blok-61-novi-beograd-beograd/"
    "trosoban-stan/650ae06563090cbb0c0c11e7"
)


def test_parse_list_extracts_items(cetiri_zida_list_html: str) -> None:
    previews = CetiriZidaScraper.parse_list(cetiri_zida_list_html)
    assert len(previews) == 20, "ItemList JSON-LD has 20 entries on this page"
    ids = {p.external_id for p in previews}
    assert len(ids) == 20
    for p in previews:
        assert p.platform is Platform.CETIRI_ZIDA
        assert p.url.startswith("https://www.4zida.rs/izdavanje-stanova/")
        # external_id should be the trailing hex ObjectId
        assert len(p.external_id) >= 16


def test_parse_detail_extracts_core_fields(cetiri_zida_detail_html: str) -> None:
    detail = CetiriZidaScraper.parse_detail(DETAIL_URL, cetiri_zida_detail_html)
    assert detail.platform is Platform.CETIRI_ZIDA
    assert detail.external_id == "650ae06563090cbb0c0c11e7"
    assert detail.url == DETAIL_URL
    assert detail.price_eur == 1600
    assert detail.area_m2 == 90.0
    assert detail.rooms == 3
    assert detail.lat == 44.8039
    assert detail.lon == 20.3906
    assert detail.address_raw == "Jurija Gagarina"
    assert detail.title and "90" in detail.title
    # Photos collected from Apartment.image array, INCLUDING the cover photo that is referenced
    # as {"@id": "...#primary-image"} and resolved via the top-level ImageObject block.
    assert len(detail.photo_urls) == 13
    # The cover lives behind resizer URLs with a base64 path: "local:///<listing>/c041ec7153_wm".
    cover_b64_tail = "L2MwNDFlYzcxNTNfd20"
    assert any(cover_b64_tail in u for u in detail.photo_urls), "cover photo must be resolved via @id"
    for url in detail.photo_urls:
        assert url.startswith("https://")


def test_district_derived_from_url(cetiri_zida_detail_html: str) -> None:
    detail = CetiriZidaScraper.parse_detail(DETAIL_URL, cetiri_zida_detail_html)
    assert detail.district == "Blok 61 Novi Beograd Beograd"


# --------------------------------------------------------------------------- list_recent (offline)
# Exercise the retry + consecutive-failure abort the resilience fix added (was `except: break`).


def _list_html(ids: list[str], total: int | None = None) -> str:
    elements = [
        {"item": {"url": f"https://www.4zida.rs/izdavanje-stanova/beograd/stan/{i}", "name": f"Stan {i}"}}
        for i in ids
    ]
    blob = json.dumps({"@type": "ItemList", "itemListElement": elements})
    header = f"<p>{total:,} oglasa</p>".replace(",", ".") if total is not None else ""
    return f'<html><body>{header}<script type="application/ld+json">{blob}</script></body></html>'


def _make_scraper(handler) -> CetiriZidaScraper:
    sc = CetiriZidaScraper(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    sc.page_delay_sec = 0.0
    sc.retry_backoff_sec = 0.0
    return sc


@pytest.mark.asyncio
async def test_list_recent_skips_isolated_failure_but_continues() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("strana", "1"))
        if page == 2:
            return httpx.Response(500, text="boom")
        ids = [f"id{page}"] if page <= 3 else []
        return httpx.Response(200, text=_list_html(ids))

    sc = _make_scraper(handler)
    previews = await sc.list_recent(max_pages=10)
    assert {p.external_id for p in previews} == {"id1", "id3"}
    assert sc._skipped_pages == 1


@pytest.mark.asyncio
async def test_list_recent_aborts_after_consecutive_failures() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, text="down")

    sc = _make_scraper(handler)
    previews = await sc.list_recent(max_pages=100)
    assert previews == []
    assert sc._skipped_pages == sc.max_consecutive_failures
    assert calls["n"] == sc.max_consecutive_failures * sc.fetch_attempts


@pytest.mark.asyncio
async def test_list_recent_segments_by_price_when_capped() -> None:
    """Full walk must fan out into skuplje_od/jeftinije_od price bands when the site total
    exceeds what one search returns — 4zida holds ~7000 but serves max ~2000 per search."""
    band_requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.params
        page = int(p.get("strana", "1"))
        if "skuplje_od" in p:
            band_requests.append(f"{p.get('skuplje_od')}-{p.get('jeftinije_od', 'inf')}")
            if page > 1:
                return httpx.Response(200, text=_list_html([], total=3))
            band_ids = {"0": ["a1", "a2"], "600": ["b1", "id1"]}  # id1 overlaps the base walk
            return httpx.Response(200, text=_list_html(band_ids[p["skuplje_od"]], total=3))
        if page > 2:
            # Past the cap 4zida re-serves earlier content; dedup must end the walk.
            return httpx.Response(200, text=_list_html(["id1"], total=120))
        return httpx.Response(200, text=_list_html([f"id{page}"], total=120))

    sc = _make_scraper(handler)
    sc.page_cap = 2
    sc.price_bands = ((0, 600), (600, None))
    previews = await sc.list_recent(max_pages=500)

    ids = {p.external_id for p in previews}
    assert ids == {"id1", "id2", "a1", "a2", "b1"}, "bands merged + deduped"
    assert sc._site_total == 120
    assert sorted(set(band_requests)) == ["0-600", "600-inf"]


@pytest.mark.asyncio
async def test_list_recent_incremental_never_segments() -> None:
    saw_band = {"v": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if "skuplje_od" in request.url.params:
            saw_band["v"] = True
        page = int(request.url.params.get("strana", "1"))
        return httpx.Response(200, text=_list_html([f"p{page}x"], total=99999))

    sc = _make_scraper(handler)
    await sc.list_recent(max_pages=3)
    assert saw_band["v"] is False


@pytest.mark.asyncio
async def test_soft_empty_page_below_expected_end_is_skipped_not_break() -> None:
    """REGRESSION: a 200 page with no ItemList while the site total says more pages exist is a
    rate-limit/error page. Treating it as end-of-pagination silently truncated 4zida to 9%."""
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("strana", "1"))
        if page == 2:
            return httpx.Response(200, text="<html><p>uh oh, no data block</p></html>")
        if page > 3:
            return httpx.Response(200, text=_list_html([], total=6))
        return httpx.Response(200, text=_list_html([f"id{page}a", f"id{page}b"], total=6))

    sc = _make_scraper(handler)
    previews = await sc.list_recent(max_pages=10)
    assert {p.external_id for p in previews} == {"id1a", "id1b", "id3a", "id3b"}
    assert sc._skipped_pages == 1, "the soft-failure page must be counted as skipped"


def test_total_from_html_parses_thousands_dot() -> None:
    assert CetiriZidaScraper._total_from_html("<p>6.947 oglasa</p>") == 6947
    assert CetiriZidaScraper._total_from_html("<p>567 oglasa</p>") == 567
    assert CetiriZidaScraper._total_from_html("<p>no count here</p>") is None


def test_real_legacy_header_ignores_agency_offer_counts(cetiri_zida_list_html: str) -> None:
    assert CetiriZidaScraper._total_from_html(cetiri_zida_list_html) == 6947


@pytest.mark.parametrize("number,expected", [("4.496", 4496), ("216", 216), ("0", 0),
                                             ("4\u00a0496", 4496), ("4,496", 4496)])
def test_rendered_split_counter_takes_precedence_over_seo(number, expected) -> None:
    # Live markup on 2026-09-12: the SEO approximation is not the search total.
    html = ('<meta name="description" content="Pretražite više od 6.500 oglasa">'
            '<a href="/agency"><small><strong>2495 oglasa</strong></small></a>'
            '<span class="inline-flex items-baseline gap-1.5">'
            f'<strong class="text-2xl font-bold text-gray-800">{number}</strong>'
            '<span class="text-xs font-normal text-gray-400">oglasa</span></span>')
    assert CetiriZidaScraper._total_from_html(html) == expected


@pytest.mark.parametrize("html", [
    '<meta name="description" content="više od 6.500 oglasa">',
    '<p>Pretražite više od 6.500 oglasa</p>',
    '<script>"6.500 oglasa"</script>',
    '<template><strong>6.500 oglasa</strong></template>',
    '<strong>216 oglasa</strong><strong>220 oglasa</strong>',
    '<strong>1.5 oglasa</strong>',
])
def test_approximate_hidden_or_conflicting_counts_are_not_trusted(html) -> None:
    assert CetiriZidaScraper._total_from_html(html) is None


@pytest.mark.asyncio
async def test_split_counter_bounds_walk_without_false_empty_page_failure() -> None:
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("strana", "1"))
        requested.append(page)
        html = _list_html([f"id{page}a", f"id{page}b"], total=6)
        html = html.replace('6 oglasa', '<strong>6</strong><span>oglasa</span>')
        html = '<meta content="više od 6.500 oglasa">' + html
        return httpx.Response(200, text=html)

    scraper = _make_scraper(handler)
    previews = await scraper.list_recent(max_pages=10)
    assert requested == [1, 2, 3]
    assert len(previews) == scraper._site_total == 6
    assert scraper._pages_failed == scraper._skipped_pages == 0
