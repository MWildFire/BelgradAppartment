"""Unit tests for the cityexpert.rs scraper.

cityexpert is an Angular SPA backed by a clean JSON API — no Playwright. The pure parsers
(parse_list / parse_detail) run against stored JSON fixtures, no network:
  - list.json   — a trimmed real `GET /api/Search?req=<json>` response (result[] + info).
  - detail.json — a real `GET /api/PropertyView/{propId}/r` response.

The list_recent tests drive the pagination/retry/abort logic with an injected MockTransport
client (offline), mirroring the nekretnine/4zida suites.
"""

from __future__ import annotations

import json

import httpx
import pytest

from src.db.models import Platform
from src.scrapers.cityexpert import CityExpertScraper

# Detail fixture is propId 28075 — "Krunska, Vračar", structure 3.5, 75 m², 1500 EUR.
DETAIL_URL = "https://cityexpert.rs/izdavanje-nekretnina/beograd/28075/troiposoban-stan-krunska-vracar"


# --------------------------------------------------------------------------- parse_list


def test_parse_list_returns_unique_previews(cityexpert_list_json: str) -> None:
    previews = CityExpertScraper.parse_list(cityexpert_list_json)
    assert len(previews) >= 5, "list page should yield several previews"
    ids = [p.external_id for p in previews]
    assert len(ids) == len(set(ids)), "external_ids must be unique"
    for p in previews:
        assert p.platform is Platform.CITYEXPERT
        assert p.external_id and p.external_id.isdigit(), "external_id = numeric propId"


def test_parse_list_builds_browsable_public_url(cityexpert_list_json: str) -> None:
    previews = CityExpertScraper.parse_list(cityexpert_list_json)
    for p in previews:
        # Public, slug-bearing URL: the slug-less form 302→/404 on cityexpert.
        assert p.url.startswith("https://cityexpert.rs/izdavanje-nekretnina/beograd/")
        assert f"/{p.external_id}/" in p.url, "URL must carry the propId path segment"


def test_parse_list_slug_matches_real_canonical(cityexpert_list_json: str) -> None:
    # propId 78694: structure 1.0, street "Mihaila Bulgakova", municipality "Zvezdara"
    # → canonical slug from the sitemap is jednosoban-stan-mihaila-bulgakova-zvezdara.
    previews = {p.external_id: p for p in CityExpertScraper.parse_list(cityexpert_list_json)}
    p = previews["78694"]
    assert p.url == (
        "https://cityexpert.rs/izdavanje-nekretnina/beograd/78694/"
        "jednosoban-stan-mihaila-bulgakova-zvezdara"
    )


def test_parse_list_handles_missing_payload() -> None:
    assert CityExpertScraper.parse_list("not json") == []
    assert CityExpertScraper.parse_list(json.dumps({"info": {}})) == []


# --------------------------------------------------------------------------- parse_detail


def test_parse_detail_extracts_core_fields(cityexpert_detail_json: str) -> None:
    detail = CityExpertScraper.parse_detail(DETAIL_URL, cityexpert_detail_json)
    assert detail.platform is Platform.CITYEXPERT
    assert detail.external_id == "28075"
    assert detail.url == DETAIL_URL
    assert detail.price_eur == 1500
    assert detail.area_m2 == 75.0
    assert detail.rooms == 3.5  # structure "3.5" → 3.5 rooms
    assert detail.lat is not None and 44.5 < detail.lat < 44.95
    assert detail.lon is not None and 20.0 < detail.lon < 20.7
    assert detail.address_raw == "Krunska"
    # district from municipality / selectedNeighbourhoods
    assert detail.district is not None and "Vračar" in detail.district


def test_parse_detail_fills_all_required_fields(cityexpert_detail_json: str) -> None:
    detail = CityExpertScraper.parse_detail(DETAIL_URL, cityexpert_detail_json)
    for field in CityExpertScraper.REQUIRED_DETAIL_FIELDS:
        assert getattr(detail, field), f"missing required field {field!r}"


def test_parse_detail_photos_are_cdn_urls(cityexpert_detail_json: str) -> None:
    detail = CityExpertScraper.parse_detail(DETAIL_URL, cityexpert_detail_json)
    assert len(detail.photo_urls) >= 5
    for u in detail.photo_urls:
        # CDN pattern: properties/<width>/<bucket>/<propId>/slike/<filename>
        assert u.startswith("https://img.cityexpert.rs/properties/")
        assert "/28000/28075/slike/" in u, "bucket = floor(propId/1000)*1000"
    assert len(detail.photo_urls) == len(set(detail.photo_urls)), "photos deduped"
    # cover image leads the list
    assert detail.photo_urls[0].endswith("28075_-_6639_-_20170812.jpg")


def test_parse_detail_missing_payload_is_graceful() -> None:
    detail = CityExpertScraper.parse_detail(DETAIL_URL, "not json")
    assert detail.platform is Platform.CITYEXPERT
    assert detail.external_id == "28075"
    assert detail.price_eur is None
    assert detail.photo_urls == []


def test_structure_to_rooms_mapping() -> None:
    f = CityExpertScraper._structure_to_rooms
    assert f("0.5") == 0.5  # garsonjera / studio
    assert f("1.0") == 1.0
    assert f("1.5") == 1.5
    assert f("2.0") == 2.0
    assert f("3.5") == 3.5
    assert f("5+") == 5.0
    assert f("OTHER") is None
    assert f(None) is None
    assert f("") is None


def test_external_id_from_url() -> None:
    assert CityExpertScraper._external_id_from_url(DETAIL_URL) == "28075"
    assert (
        CityExpertScraper._external_id_from_url(
            "https://cityexpert.rs/izdavanje-nekretnina/beograd/78694/jednosoban-stan"
        )
        == "78694"
    )


# --------------------------------------------------------------------------- fingerprint


def test_fingerprint_stable(cityexpert_list_json: str) -> None:
    fp = CityExpertScraper.fingerprint(cityexpert_list_json)
    assert fp["source"] == "cityexpert-api"
    assert "propId" in fp["result_keys"] and "structure" in fp["result_keys"]
    assert fp["result_keys"] == sorted(fp["result_keys"])
    assert fp["n"] >= 1


def test_fingerprint_empty_on_garbage() -> None:
    assert CityExpertScraper.fingerprint("nope") == {}
    assert CityExpertScraper.fingerprint(json.dumps({"info": {}})) == {}


# --------------------------------------------------------------------------- list_recent (offline)


def _list_json(ids: list[int], page: int, page_count: int) -> str:
    result = [
        {
            "propId": i,
            "structure": "2.0",
            "street": "Test Street",
            "municipality": "Vračar",
            "price": 500.0,
            "size": 50,
            "coverPhoto": f"cityexpert.rs_-_nekretnina_id_-_{i}.jpg",
        }
        for i in ids
    ]
    payload = {
        "result": result,
        "info": {
            "pageNumber": page,
            "pageCount": page_count,
            "documentCount": page_count * 12,
        },
    }
    return json.dumps(payload)


def _make_scraper(handler) -> CityExpertScraper:
    sc = CityExpertScraper(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    sc.page_delay_sec = 0.0
    sc.retry_backoff_sec = 0.0
    return sc


def _page_of(request: httpx.Request) -> int:
    """The current page is encoded inside the url-encoded ?req= JSON object."""
    import urllib.parse

    raw = request.url.params.get("req", "{}")
    obj = json.loads(urllib.parse.unquote(raw))
    return int(obj.get("currentPage", 1))


@pytest.mark.asyncio
async def test_list_recent_paginates_and_dedupes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        page = _page_of(request)
        ids = list(range(1, 13)) if page == 1 else list(range(12, 24))  # 12 overlaps
        return httpx.Response(200, text=_list_json(ids, page, page_count=2))

    sc = _make_scraper(handler)
    previews = await sc.list_recent(max_pages=10)
    ids = [p.external_id for p in previews]
    assert len(ids) == len(set(ids)), "must dedupe across pages"
    assert len(ids) == 23, "1..23 unique after overlap dedupe"
    assert sc._skipped_pages == 0
    assert sc._site_total == 24


@pytest.mark.asyncio
async def test_list_recent_stops_at_page_count() -> None:
    fetched: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = _page_of(request)
        fetched.append(page)
        return httpx.Response(200, text=_list_json([page * 100 + i for i in range(5)], page, page_count=2))

    sc = _make_scraper(handler)
    await sc.list_recent(max_pages=50)
    assert fetched == [1, 2], "must not fetch beyond info.pageCount"


@pytest.mark.asyncio
async def test_list_recent_skips_isolated_failure_but_continues() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        page = _page_of(request)
        if page == 2:
            return httpx.Response(500, text="boom")
        ids = [page * 10] if page <= 3 else []
        return httpx.Response(200, text=_list_json(ids, page, page_count=99))

    sc = _make_scraper(handler)
    previews = await sc.list_recent(max_pages=10)
    assert {p.external_id for p in previews} == {"10", "30"}
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
async def test_list_recent_early_stops_on_known_ids() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        page = _page_of(request)
        ids = [1, 2, 3] if page == 1 else [4, 5, 6]
        return httpx.Response(200, text=_list_json(ids, page, page_count=99))

    sc = _make_scraper(handler)
    # All page-1 ids known → after early_stop_full_known_pages the walk stops.
    previews = await sc.list_recent(
        max_pages=10, known_ids={"1", "2", "3"}, early_stop_full_known_pages=1
    )
    assert {p.external_id for p in previews} == {"1", "2", "3"}


@pytest.mark.asyncio
async def test_list_recent_breaks_on_empty_page() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        page = _page_of(request)
        ids = [1, 2, 3] if page == 1 else []
        return httpx.Response(200, text=_list_json(ids, page, page_count=99))

    sc = _make_scraper(handler)
    previews = await sc.list_recent(max_pages=10)
    assert {p.external_id for p in previews} == {"1", "2", "3"}


# --------------------------------------------------------------------------- fetch_detail (offline)


@pytest.mark.asyncio
async def test_fetch_detail_hits_property_view_api(cityexpert_detail_json: str) -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, text=cityexpert_detail_json)

    sc = _make_scraper(handler)
    detail = await sc.fetch_detail(DETAIL_URL)
    # The robust re-fetch hits the slug-independent API, not the public slug URL.
    assert requested == ["https://cityexpert.rs/api/PropertyView/28075/r"]
    assert detail.external_id == "28075"
    assert detail.price_eur == 1500
    assert detail.url == DETAIL_URL  # stored URL stays the browsable public one


@pytest.mark.parametrize("status", ["FINISHED", "NOT-AVAILABLE", "KAPARISANA-RENT"])
def test_retained_unavailable_detail_is_not_active(cityexpert_detail_json, status):
    from src.scrapers.base import SourceListingInactiveError

    payload = json.loads(cityexpert_detail_json)
    payload["status"] = status
    assert payload["price"] and payload["size"] and payload["onsite"]
    with pytest.raises(SourceListingInactiveError):
        CityExpertScraper.parse_detail(DETAIL_URL, json.dumps(payload))


@pytest.mark.parametrize("status", [None, "", "UNKNOWN", "DRAFT", "published", 101, [], {}])
def test_unconfirmed_status_never_becomes_verified(cityexpert_detail_json, status):
    payload = json.loads(cityexpert_detail_json)
    payload["status"] = status
    with pytest.raises(ValueError, match="availability_unconfirmed"):
        CityExpertScraper.parse_detail(DETAIL_URL, json.dumps(payload))


@pytest.mark.parametrize("field,value", [
    ("propId", 999), ("propId", "28075"), ("propId", None), ("propId", True),
    ("cityId", 2), ("cityId", "1"), ("cityId", None), ("cityId", True),
    ("rentOrSale", "s"), ("rentOrSale", None),
])
@pytest.mark.parametrize("status", ["PUBLISHED", "FINISHED"])
def test_wrong_detail_identity_never_changes_availability(cityexpert_detail_json, field, value, status):
    payload = json.loads(cityexpert_detail_json)
    payload.update(status=status)
    payload[field] = value
    with pytest.raises(ValueError, match="identity_unconfirmed"):
        CityExpertScraper.parse_detail(DETAIL_URL, json.dumps(payload))


@pytest.mark.parametrize("url", [
    DETAIL_URL.replace("https:", "http:"),
    DETAIL_URL.replace("cityexpert.rs", "cityexpert.rs.example.com"),
    DETAIL_URL.replace("cityexpert.rs", "user@cityexpert.rs"),
    DETAIL_URL.replace("cityexpert.rs", "cityexpert.rs:443"),
    DETAIL_URL.replace("izdavanje-nekretnina", "prodaja-nekretnina"),
    DETAIL_URL + "?unknown=1", DETAIL_URL + "#unknown",
])
def test_inactive_signal_requires_canonical_rental_context(cityexpert_detail_json, url):
    payload = json.loads(cityexpert_detail_json)
    payload["status"] = "FINISHED"
    with pytest.raises(ValueError, match="identity_unconfirmed"):
        CityExpertScraper.parse_detail(url, json.dumps(payload))


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["FINISHED", "NOT-AVAILABLE", "KAPARISANA-RENT"])
async def test_fetch_inactive_api_propagates_without_retry(cityexpert_detail_json, status):
    from src.scrapers.base import SourceListingInactiveError

    payload = json.loads(cityexpert_detail_json)
    payload["status"] = status
    requested = []
    def handler(request):
        requested.append(str(request.url))
        return httpx.Response(200, json=payload)
    sc = _make_scraper(handler)
    try:
        with pytest.raises(SourceListingInactiveError):
            await sc.fetch_detail(DETAIL_URL)
    finally:
        await sc._client.aclose()
    assert requested == ["https://cityexpert.rs/api/PropertyView/28075/r"]
