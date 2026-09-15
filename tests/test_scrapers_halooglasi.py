"""Unit tests for the halooglasi.com scraper.

Pure-parser tests: feed saved HTML, check that QuidditaEnvironment.serverListData /
.CurrentClassified are decoded correctly. Plus the Cloudflare-challenge navigation policy,
driven through a fake Playwright page — no real browser here; the live path is covered by the
smoke-test from CLI.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from src.db.models import Platform
from src.scrapers.halooglasi import HalooglasiScraper

FIXTURES = Path(__file__).parent / "fixtures" / "halooglasi"

# The interstitial halooglasi's Cloudflare actually serves — Serbian, because the browser locale
# is sr-RS. Measured on the VPS 16.07: title 'Sačekajte trenutak...', HTTP 403, cf-turnstile widget.
CHALLENGE_HTML = (
    "<html><head><title>Sačekajte trenutak...</title></head><body>"
    '<div class="main-wrapper"><h2>Obavljanje bezbednosne verifikacije</h2>'
    '<input type="hidden" name="cf-turnstile-response" id="cf-chl-widget-5t9hm_response">'
    '<script src="/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1"></script>'
    "</div></body></html>"
)
GOOD_DETAIL_HTML = (
    "<html><body><script>QuidditaEnvironment.CurrentClassified = "
    '{"Id":"5425647413192","Title":"Zemun"};</script></body></html>'
)


@pytest.fixture
def halooglasi_list_html() -> str:
    return (FIXTURES / "list.html").read_text(encoding="utf-8")


@pytest.fixture
def halooglasi_detail_html() -> str:
    return (FIXTURES / "detail.html").read_text(encoding="utf-8")


@pytest.fixture
def halooglasi_detail_url() -> str:
    return (FIXTURES / "detail_url.txt").read_text(encoding="utf-8").strip()


def test_parse_list_extracts_20_ads(halooglasi_list_html: str) -> None:
    previews = HalooglasiScraper.parse_list(halooglasi_list_html)
    assert len(previews) == 20
    ids = {p.external_id for p in previews}
    assert len(ids) == 20, "external_ids must be unique"
    for p in previews:
        assert p.platform is Platform.HALOOGLASI
        assert p.url.startswith("https://www.halooglasi.com/nekretnine/izdavanje-stanova/")
        # halooglasi ad IDs are 13 digits
        assert p.external_id.isdigit() and len(p.external_id) >= 10


def test_parse_detail_core_fields(
    halooglasi_detail_html: str, halooglasi_detail_url: str
) -> None:
    detail = HalooglasiScraper.parse_detail(halooglasi_detail_url, halooglasi_detail_html)
    assert detail.platform is Platform.HALOOGLASI
    assert detail.external_id == "5425647097326"
    assert detail.price_eur == 400  # cena_d: 400.0 EUR
    assert detail.area_m2 == 50.0
    assert detail.rooms == 2.0  # broj_soba_s: "2.0"
    # coords from GeoLocationRPT: "44.848287,20.401391"
    assert detail.lat == pytest.approx(44.848287, abs=1e-4)
    assert detail.lon == pytest.approx(20.401391, abs=1e-4)
    assert detail.address_raw == "Dobrovoljačka"
    # The source sends both a micro-location and its parent municipality. Persist the parent:
    # the public catalog deliberately exposes only its ten curated districts.
    assert detail.district == "Opština Zemun"
    assert detail.title and "Zemun" in detail.title
    # photos: prefixed with img.halooglasi.com
    assert len(detail.photo_urls) >= 5
    for u in detail.photo_urls:
        assert u.startswith("https://img.halooglasi.com/slike/")


def test_parse_detail_returns_stub_on_missing_var() -> None:
    detail = HalooglasiScraper.parse_detail(
        "https://www.halooglasi.com/nekretnine/izdavanje-stanova/x/1234567890",
        "<html><body>no data here</body></html>",
    )
    assert detail.platform is Platform.HALOOGLASI
    assert detail.external_id == "1234567890"
    assert detail.price_eur is None
    assert detail.lat is None
    assert detail.photo_urls == []


# ------------------------------------------ Cloudflare challenge navigation policy (16.07)
# Measured on the VPS 16.07 against a live detail URL, fresh browser profile each arm:
#
#   goto detail, poll 150s, no reload   → 403, still 'Sačekajte trenutak...'   (never resolves)
#   goto detail, soak 45s, reload       → 403, still the challenge
#                     …reload again     → 200, QuidditaEnvironment.CurrentClassified present ✅
#   goto detail + Referer, fresh ctx    → 403 (a same-site Referer alone changes nothing)
#
# So: Cloudflare grants clearance while the browser sits on the interstitial, but the interstitial
# NEVER navigates to the real page by itself — only a fresh navigation cashes the clearance in.
# That is also why the list walk works while detail did not: the list loop re-`goto`s in the SAME
# context (its 3rd attempt IS the reload), whereas fetch_detail recycled the browser after every
# failure and threw the clearance away — detail failed 9/9 in prod on 16.07.

_DETAIL_URL = "https://www.halooglasi.com/nekretnine/izdavanje-stanova/zemun/5425647413192?kid=4"
_DETAIL_VAR = "QuidditaEnvironment.CurrentClassified"


def test_smsprint_url_keeps_canonical_public_urls_separate() -> None:
    url = "https://www.halooglasi.com/nekretnine/izdavanje-stanova/zemun/5425647413192?kid=4"
    assert HalooglasiScraper._smsprint_url(url) == (
        "https://smsprint.halooglasi.com/nekretnine/izdavanje-stanova/zemun/5425647413192?kid=4"
    )
    assert HalooglasiScraper._smsprint_url("https://example.com/nekretnine") is None


@pytest.mark.asyncio
async def test_smsprint_fetch_requires_the_expected_payload_marker() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "smsprint.halooglasi.com"
        return httpx.Response(200, text=GOOD_DETAIL_HTML)

    scraper = HalooglasiScraper(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    try:
        html = await scraper._fetch_via_smsprint(_DETAIL_URL, ready_marker=_DETAIL_VAR)
    finally:
        await scraper.aclose()
    assert _DETAIL_VAR in html


@pytest.mark.asyncio
async def test_print_catalogue_redirect_stays_unknown_without_browser_retry() -> None:
    from src.scrapers.halooglasi import _MarkerMissing

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/nekretnine/izdavanje-stanova":
            return httpx.Response(302, headers={"location": "/nekretnine/izdavanje-stanova"})
        return httpx.Response(200, text="QuidditaEnvironment.serverListData = {};")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
    scraper = HalooglasiScraper(client=client)
    scraper._context = object()  # No browser method can be called on this sentinel.
    try:
        with pytest.raises(_MarkerMissing, match="availability unknown"):
            await scraper.fetch_detail(_DETAIL_URL)
    finally:
        scraper._context = None
        await scraper.aclose()


@pytest.mark.asyncio
async def test_long_print_walk_does_not_accumulate_view_history_or_discard_session() -> None:
    seen_sizes: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        cookies = request.headers.get("cookie", "")
        seen_sizes.append(len(cookies))
        assert "ASP.NET_SessionId=session" in cookies
        assert "preferences=keep" in cookies
        assert "ViewedAds_20260915" not in cookies
        return httpx.Response(200, text=GOOD_DETAIL_HTML, headers={
            "set-cookie": "ViewedAds_20260915=" + "1234567890123," * 1300 + "; Path=/",
        })

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client.cookies.set("ASP.NET_SessionId", "session", domain="smsprint.halooglasi.com")
    client.cookies.set("preferences", "keep", domain=".halooglasi.com")
    client.cookies.set("ViewedAds_20260914", "old", domain=".halooglasi.com")
    client.cookies.set("ViewedAds_20260915", "unrelated", domain="example.com")
    scraper = HalooglasiScraper(client=client)
    try:
        for _ in range(10):
            assert _DETAIL_VAR in await scraper._fetch_via_smsprint(
                _DETAIL_URL, ready_marker=_DETAIL_VAR,
            )
        assert max(seen_sizes) < 100
        assert client.cookies.get("ViewedAds_20260915", domain="example.com") == "unrelated"
    finally:
        await scraper.aclose()
        await client.aclose()


@pytest.mark.asyncio
async def test_print_error_redirect_records_unknown_without_browser_or_dead_evidence() -> None:
    from src.scrapers.halooglasi import _MarkerMissing

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/404":
            return httpx.Response(302, headers={"location": f"/404?aspxerrorpath={request.url.path}"})
        return httpx.Response(404, text="Page unavailable")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
    scraper = HalooglasiScraper(client=client)
    scraper._context = object()
    try:
        # An HTTPStatusError(404) could be interpreted as removal by the caller;
        # this response must instead enter its ordinary UNKNOWN/backoff path.
        with pytest.raises(_MarkerMissing, match="availability unknown"):
            await scraper.fetch_detail(_DETAIL_URL)
    finally:
        scraper._context = None
        await scraper.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["direct", "other_path", "other_host", "other_origin", "challenge", "payload"])
async def test_unconfirmed_print_errors_keep_canonical_fallback(case: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/404" and case != "direct":
            origin = "/unrelated" if case == "other_origin" else request.url.path
            host = "https://example.com" if case == "other_host" else ""
            path = "/missing" if case == "other_path" else "/404"
            if request.url.path != "/missing":
                return httpx.Response(302, headers={"location": f"{host}{path}?aspxerrorpath={origin}"})
        body = CHALLENGE_HTML if case == "challenge" else GOOD_DETAIL_HTML if case == "payload" else "Missing"
        return httpx.Response(404, text=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
    scraper = HalooglasiScraper(client=client)
    try:
        assert await scraper._fetch_via_smsprint(_DETAIL_URL, ready_marker=_DETAIL_VAR) == ""
    finally:
        await scraper.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [GOOD_DETAIL_HTML, CHALLENGE_HTML, ""])
async def test_print_slug_redirect_preserves_payload_or_browser_fallback(body: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/updated-slug/123":
            return httpx.Response(302, headers={"location": "/updated-slug/123"})
        return httpx.Response(200, text=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
    scraper = HalooglasiScraper(client=client)
    try:
        result = await scraper._fetch_via_smsprint(_DETAIL_URL, ready_marker=_DETAIL_VAR)
        assert result == (body if _DETAIL_VAR in body else "")
    finally:
        await scraper.aclose()


_REMOVED_HTML = (
    '<div class="info-box-expired info-box-expired-full">'
    '<div class="glyphicon glyphicon-exclamation-sign"></div>\n'
    'Nažalost, oglas nije pronađen. Oglas je obrisan ili ste pronašli neispravan link. '
    'Pogledajte druge oglase u ovoj kategoriji.\n</div>'
    '<script>QuidditaEnvironment.serverListData = {"Ads": []};</script>'
)
_REMOVED_DEST = "/nekretnine/izdavanje-stanova?referrer=oglas"


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["", "?kid=1", "?kid=3", "?kid=4"])
async def test_explicit_print_removal_is_propagated_without_browser(query: str) -> None:
    from src.scrapers.base import SourceListingRemovedError

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.query != b"referrer=oglas":
            return httpx.Response(301, headers={"location": _REMOVED_DEST})
        return httpx.Response(200, text=_REMOVED_HTML)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
    scraper = HalooglasiScraper(client=client)
    scraper._context = object()
    try:
        with pytest.raises(SourceListingRemovedError):
            await scraper.fetch_detail(_DETAIL_URL.split("?")[0] + query)
    finally:
        scraper._context = None
        await scraper.aclose()


@pytest.mark.parametrize("case", [
    "no_history", "extra_hop", "302", "different_origin", "post", "absolute_location",
    "other_host", "http", "port", "fragment", "wrong_id", "wrong_path", "extra_query",
    "duplicate_query", "unknown_query", "final_host", "final_query", "final_fragment", "status",
    "no_notice", "wrong_notice", "duplicate_notice", "text_only", "no_payload",
    "bad_payload", "bad_ads", "detail_payload", "challenge",
])
def test_ambiguous_print_signals_never_prove_removal(case: str) -> None:
    from src.scrapers.halooglasi import _explicit_print_not_found

    url = _DETAIL_URL
    replacements = {
        "other_host": ("www.halooglasi.com", "example.com"),
        "http": ("https:", "http:"),
        "port": (".com/", ".com:8443/"),
        "fragment": ("?kid=4", "?kid=4#fragment"),
        "wrong_id": ("5425647413192", "123"),
        "wrong_path": ("izdavanje-stanova", "prodaja-stanova"),
        "extra_query": ("kid=4", "kid=4&x=1"),
        "duplicate_query": ("kid=4", "kid=4&kid=4"),
        "unknown_query": ("kid=4", "kid=999"),
    }
    if case in replacements:
        url = url.replace(*replacements[case])
    origin = HalooglasiScraper._smsprint_url(url) or url
    if case == "different_origin":
        origin = origin.replace("5425647413192", "5425647413193")
    destination = "https://smsprint.halooglasi.com" + _REMOVED_DEST
    if case == "final_host":
        destination = destination.replace("smsprint.halooglasi.com", "example.com")
    if case == "final_query":
        destination += "&other=1"
    if case == "final_fragment":
        destination += "#fragment"
    redirect = httpx.Response(
        302 if case == "302" else 301,
        request=httpx.Request("POST" if case == "post" else "GET", origin),
        headers={"location": destination if case == "absolute_location" else _REMOVED_DEST},
    )
    history = [] if case == "no_history" else [redirect, redirect] if case == "extra_hop" else [redirect]
    body = _REMOVED_HTML
    if case == "no_notice":
        body = body.replace("info-box-expired-full", "other")
    elif case == "wrong_notice":
        body = body.replace("obrisan", "dostupan")
    elif case == "duplicate_notice":
        body += _REMOVED_HTML
    elif case == "text_only":
        body = body.replace('<div class="info-box-expired info-box-expired-full">', "<span>")
    elif case == "no_payload":
        body = body.split("<script>")[0]
    elif case == "bad_payload":
        body = body.replace('{"Ads": []}', '{invalid}')
    elif case == "bad_ads":
        body = body.replace('{"Ads": []}', '{"Ads": null}')
    elif case == "detail_payload":
        body += GOOD_DETAIL_HTML
    elif case == "challenge":
        body += CHALLENGE_HTML
    response = httpx.Response(
        503 if case == "status" else 200, text=body, history=history,
        request=httpx.Request("GET", destination),
    )
    assert not _explicit_print_not_found(url, response)


class _FakePage:
    """Playwright-Page stand-in: serves `sequence[n]` for the n-th navigation (goto, then reloads)."""

    def __init__(self, sequence: list[str]) -> None:
        self._sequence = sequence
        self._nav = 0
        self.reloads = 0
        self.closed = False

    async def goto(self, _url: str, **_kw: Any) -> None:
        return None

    async def reload(self, **_kw: Any) -> None:
        self.reloads += 1
        self._nav += 1

    async def content(self) -> str:
        return self._sequence[min(self._nav, len(self._sequence) - 1)]

    async def wait_for_timeout(self, _ms: float) -> None:
        return None

    async def close(self) -> None:
        self.closed = True


class _FakeContext:
    def __init__(self, page: _FakePage) -> None:
        self._page = page

    async def new_page(self) -> _FakePage:
        return self._page


def _scraper_with(page: _FakePage) -> HalooglasiScraper:
    sc = HalooglasiScraper()
    sc._context = _FakeContext(page)  # type: ignore[assignment]

    async def _no_mirror(*_args: Any, **_kwargs: Any) -> str:
        return ""

    sc._fetch_via_smsprint = _no_mirror  # type: ignore[method-assign]
    return sc


@pytest.fixture
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Collapse the retry backoff so the retry-policy tests don't actually wait seconds."""
    import asyncio

    async def _instant(_secs: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _instant)


async def test_fetch_with_browser_reloads_past_cf_challenge() -> None:
    """The interstitial earns clearance but never self-navigates — so reload it, don't just poll."""
    page = _FakePage([CHALLENGE_HTML, CHALLENGE_HTML, GOOD_DETAIL_HTML])
    html = await _scraper_with(page)._fetch_with_browser(_DETAIL_URL, ready_marker=_DETAIL_VAR)
    assert _DETAIL_VAR in html, "must return the real page once a reload cashes in the clearance"
    assert page.reloads == 2, "should reload until the marker appears, not poll a static page"
    assert page.closed


async def test_fetch_with_browser_does_not_reload_a_non_challenge_page() -> None:
    """A real page that merely lacks the marker (removed ad / soft error) is not a challenge —
    reloading it just burns a minute per attempt. Hand it back and let the caller decide."""
    plain = "<html><body>Oglas je uklonjen</body></html>"
    page = _FakePage([plain])
    html = await _scraper_with(page)._fetch_with_browser(_DETAIL_URL, ready_marker=_DETAIL_VAR)
    assert _DETAIL_VAR not in html
    assert page.reloads == 0


async def test_fetch_detail_does_not_recycle_browser_on_challenge(_no_sleep: None) -> None:
    """Recycling discards the cf_clearance the challenged attempt just earned. That is precisely
    why detail failed 9/9 in prod while the list walk — which never recycles — recovers."""
    sc = _scraper_with(_FakePage([CHALLENGE_HTML]))  # challenged forever
    recycles = 0

    async def _count_recycle() -> None:
        nonlocal recycles
        recycles += 1

    sc._recycle_browser = _count_recycle  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="detail data marker missing"):
        await sc.fetch_detail(_DETAIL_URL)
    assert recycles == 0, "a challenge must be retried in the SAME context, never recycled"


async def test_fetch_detail_still_recycles_on_real_browser_fault(_no_sleep: None) -> None:
    """A genuine Chromium fault (ERR_NETWORK_IO_SUSPENDED &c) still warrants a browser restart —
    that retry path predates the challenge-reload work and must survive it."""
    sc = _scraper_with(_FakePage([GOOD_DETAIL_HTML]))
    recycles = 0

    async def _count_recycle() -> None:
        nonlocal recycles
        recycles += 1

    async def _boom(*_a: Any, **_kw: Any) -> str:
        raise RuntimeError("net::ERR_NETWORK_IO_SUSPENDED")

    sc._recycle_browser = _count_recycle  # type: ignore[method-assign]
    sc._fetch_with_browser = _boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="ERR_NETWORK_IO_SUSPENDED"):
        await sc.fetch_detail(_DETAIL_URL)
    assert recycles == 3, "every real fault gets a fresh browser"
