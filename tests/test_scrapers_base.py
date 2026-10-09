"""Tests for the shared Scraper base — HTTP client construction (anti-403 hardening, T1-4).

A plain httpx client with just User-Agent + Accept-Language is a cheap fingerprint for anti-bot
layers: a real Chrome navigation always ships Accept, Sec-Fetch-*, sec-ch-ua*, and
Upgrade-Insecure-Requests. Their ABSENCE (not their exact values) is the tell. These tests pin the
presence of that browser-shaped header set without asserting brittle exact strings.
"""

from __future__ import annotations

import pytest

from src.config import get_settings
from src.scrapers.base import DEFAULT_USER_AGENT, Scraper
from src.scrapers.cetiri_zida import CetiriZidaScraper
from src.scrapers.nekretnine import NekretnineScraper


@pytest.mark.asyncio
async def test_make_client_sends_browser_shaped_headers() -> None:
    client = Scraper._make_client()
    try:
        h = client.headers
        # Browser-navigation headers a bare httpx client omits (their absence is the 403 tell).
        assert h["Accept"].startswith("text/html")
        assert "image/webp" in h["Accept"]
        assert h["Upgrade-Insecure-Requests"] == "1"
        assert h["Sec-Fetch-Mode"] == "navigate"
        assert h["Sec-Fetch-Dest"] == "document"
        assert h["Sec-Fetch-Site"]  # present (top-level nav default)
        assert "Chrome" in h["sec-ch-ua"]
        assert h["sec-ch-ua-mobile"] == "?0"
        assert h["sec-ch-ua-platform"]
        # Pre-existing headers must survive the addition.
        assert h["User-Agent"] == DEFAULT_USER_AGENT
        assert h["Accept-Language"].startswith("sr")
    finally:
        await client.aclose()


def test_proxy_is_reserved_for_nekretnine(monkeypatch) -> None:
    """Configured proxy must not leak into sources that work directly from the VPS."""
    captured: list[dict] = []

    class _SpyClient:
        def __init__(self, **kwargs):
            captured.append(kwargs)

    monkeypatch.setattr("src.scrapers.base.httpx.AsyncClient", _SpyClient)
    monkeypatch.setattr(get_settings(), "nekretnine_proxy_primary_url", "http://eu-proxy:3128")

    NekretnineScraper._make_client()
    CetiriZidaScraper._make_client()

    assert captured[0]["proxy"] == "http://eu-proxy:3128"
    assert captured[1]["proxy"] is None


def test_explicit_4zida_route_is_isolated_and_direct_override_wins(monkeypatch) -> None:
    from src.scrapers.cityexpert import CityExpertScraper
    from src.scrapers.halooglasi import HalooglasiScraper

    captured = []

    class SpyClient:
        def __init__(self, **kwargs):
            captured.append(kwargs.get("proxy"))

    monkeypatch.setattr("src.scrapers.base.httpx.AsyncClient", SpyClient)
    settings = get_settings()
    monkeypatch.setattr(settings, "cetiri_zida_proxy_url", "http://four:secret@isp.test:12323")
    monkeypatch.setattr(settings, "nekretnine_proxy_primary_url", "http://nek.test:3128")
    monkeypatch.setattr(settings, "scraper_proxy_url", "http://exhausted-metered.test:12323")
    for cls in (CetiriZidaScraper, NekretnineScraper, HalooglasiScraper, CityExpertScraper, Scraper):
        cls._make_client()
    CetiriZidaScraper._make_client(proxy_override=None)
    assert captured == [
        "http://four:secret@isp.test:12323", "http://nek.test:3128", None, None, None, None,
    ]
    assert "four:secret" not in repr(settings)
