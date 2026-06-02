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
    # Photos collected from Apartment.image array.
    assert len(detail.photo_urls) >= 5
    for url in detail.photo_urls:
        assert url.startswith("https://")


def test_district_derived_from_url(cetiri_zida_detail_html: str) -> None:
    detail = CetiriZidaScraper.parse_detail(DETAIL_URL, cetiri_zida_detail_html)
    assert detail.district == "Blok 61 Novi Beograd Beograd"


# --------------------------------------------------------------------------- list_recent (offline)
# Exercise the retry + consecutive-failure abort the resilience fix added (was `except: break`).


def _list_html(ids: list[str]) -> str:
    elements = [
        {"item": {"url": f"https://www.4zida.rs/izdavanje-stanova/beograd/stan/{i}", "name": f"Stan {i}"}}
        for i in ids
    ]
    blob = json.dumps({"@type": "ItemList", "itemListElement": elements})
    return f'<html><body><script type="application/ld+json">{blob}</script></body></html>'


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
    assert sc._skipped_pages == sc.max_consecutive_failures == 3
    assert calls["n"] == sc.max_consecutive_failures * sc.fetch_attempts
