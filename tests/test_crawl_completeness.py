import json

import httpx
import pytest

import src.scrapers.nekretnine as nekretnine_module
from src.scrapers.cetiri_zida import CetiriZidaScraper
from src.scrapers.cityexpert import CityExpertScraper
from src.scrapers.halooglasi import HalooglasiScraper
from src.scrapers.nekretnine import NekretnineScraper


def nek_page(ids, total, page=1, pages=1):
    data = {
        "results": [{"realEstate": {"id": i, "title": f"Flat {i}"}} for i in ids],
        "totalAds": total,
        "currentPage": page,
        "maxPages": pages,
    }
    blob = json.dumps(
        {"props": {"pageProps": {"dehydratedState": {"queries": [{"state": {"data": data}}]}}}}
    )
    return f'<script id="__NEXT_DATA__">{blob}</script>'


@pytest.mark.asyncio
async def test_newest_order_finds_new_ads_before_incremental_known_page_stop(monkeypatch):
    monkeypatch.setattr(
        nekretnine_module.get_settings(), "nekretnine_list_url",
        "https://www.nekretnine.rs/izdavanje-stanova/beograd/?zona=1&zona=2&empty=",
    )
    requested_pages = []

    def handler(request):
        params = request.url.params
        page = int(params.get("pag", 1))
        requested_pages.append(page)
        assert params.get_list("zona") == ["1", "2"]
        assert params["empty"] == ""
        newest = params.get("criterio") == "data" and params.get("ordine") == "desc"
        # Relevance puts two known pages before fresh ads, which would stop this walk early.
        ordered = [[5, 6], [1, 2], [3, 4]] if newest else [[1, 2], [3, 4], [5, 6]]
        return httpx.Response(200, text=nek_page(ordered[page - 1], 6, page=page, pages=3))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = NekretnineScraper(client=client)
        source.page_delay_sec = 0
        rows = await source.list_recent(max_pages=5, known_ids={"1", "2", "3", "4"})

    assert [row.external_id for row in rows] == ["5", "6", "1", "2", "3", "4"]
    assert requested_pages == [1, 2, 3]
    assert source._stopped_known


@pytest.mark.parametrize("query", ["criterio=prezzo&ordine=asc", "ordine=asc", "criterio="])
@pytest.mark.asyncio
async def test_explicit_search_sort_is_preserved(query, monkeypatch):
    url = f"https://www.nekretnine.rs/izdavanje-stanova/beograd/?{query}&zona=1#map"
    monkeypatch.setattr(nekretnine_module.get_settings(), "nekretnine_list_url", url)
    async with httpx.AsyncClient() as client:
        source = NekretnineScraper(client=client)
        assert source.list_url == url


@pytest.mark.asyncio
async def test_nekretnine_complete_payload_and_late_challenge_have_distinct_evidence():
    for broken in (False, True):

        def handler(request, broken=broken):
            page = int(request.url.params.get("pag", 1))
            html = (
                nek_page([1, 2], 4 if broken else 2, pages=2 if broken else 1)
                if page == 1
                else "<html>challenge</html>"
            )
            return httpx.Response(200, text=html)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            source = NekretnineScraper(client=client)
            source.page_delay_sec = source.retry_backoff_sec = 0
            rows = await source.list_recent(max_pages=10)
        assert len(rows) == 2
        assert source._crawl_complete == (not broken)
        assert source._site_total_trusted
        assert source._pages_attempted == (2 if broken else 1)
        assert source._pages_failed == int(broken)


@pytest.mark.asyncio
async def test_4zida_complete_source_total_is_not_replaced_by_observations():
    items = [
        {
            "item": {
                "url": f"https://www.4zida.rs/izdavanje-stanova/beograd/stan/{i}",
                "name": "Flat",
            }
        }
        for i in [1, 2]
    ]
    for total in (2, 20, None):
        header = f"<p>{total} oglasa</p>" if total is not None else ""
        html = (
            header
            + '<script type="application/ld+json">'
            + json.dumps({"@type": "ItemList", "itemListElement": items})
            + "</script>"
        )
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request, html=html: httpx.Response(200, text=html))
        ) as client:
            source = CetiriZidaScraper(client=client)
            source.page_delay_sec = source.retry_backoff_sec = 0
            await source.list_recent(max_pages=2)
        assert source._site_total == total
        assert source._crawl_complete == (total == 2)
        assert source._site_total_trusted == (total is not None)


@pytest.mark.asyncio
async def test_cityexpert_complete_and_wrong_page_identity():
    for page in (1, 2):
        payload = {
            "result": [
                {
                    "propId": 123,
                    "price": 500,
                    "size": 50,
                    "structure": "2.0",
                    "street": "Street",
                    "municipality": "Vracar",
                }
            ],
            "info": {"pageNumber": page, "pageCount": 1, "documentCount": 1},
        }
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request, payload=payload: httpx.Response(200, json=payload)
            )
        ) as client:
            source = CityExpertScraper(client=client)
            source.page_delay_sec = 0
            await source.list_recent(max_pages=2)
        assert source._crawl_complete == (page == 1)
        assert source._pages_failed == (page != 1)


@pytest.mark.asyncio
async def test_halooglasi_uses_source_total_and_page_identity_without_starting_browser():
    for page in (1, 2):
        payload = {
            "Ads": [
                {
                    "Id": "5425647413192",
                    "Title": "Flat",
                    "RelativeUrl": "/nekretnine/izdavanje-stanova/flat/5425647413192",
                }
            ],
            "PageNumber": page,
            "TotalPages": 1,
            "TotalCount": 1,
        }
        html = "<script>QuidditaEnvironment.serverListData = " + json.dumps(payload) + ";</script>"
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request, html=html: httpx.Response(200, text=html))
        ) as client:
            source = HalooglasiScraper(client=client)
            source._context = object()  # no browser launch; print-host payload satisfies the path
            await source.list_recent(max_pages=2)
        assert source._crawl_complete == (page == 1)
        assert source._pages_failed == (page != 1)
