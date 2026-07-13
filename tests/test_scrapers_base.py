"""Tests for the shared Scraper base — HTTP client construction (anti-403 hardening, T1-4).

A plain httpx client with just User-Agent + Accept-Language is a cheap fingerprint for anti-bot
layers: a real Chrome navigation always ships Accept, Sec-Fetch-*, sec-ch-ua*, and
Upgrade-Insecure-Requests. Their ABSENCE (not their exact values) is the tell. These tests pin the
presence of that browser-shaped header set without asserting brittle exact strings.
"""

from __future__ import annotations

import pytest

from src.scrapers.base import DEFAULT_USER_AGENT, Scraper


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
