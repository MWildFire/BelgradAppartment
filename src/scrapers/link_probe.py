"""Bounded, source-aware GET/browser probes; never a legacy-proxy HEAD shortcut."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from src.db.models import Platform
from src.scrapers.base import SourceListingRemovedError


@dataclass(frozen=True)
class LinkObservation:
    status: str
    errored: bool = False
    reason: str = "payload_verified"


class SourceProbeSession:
    def __init__(
        self,
        platform: Platform,
        *,
        factory=None,
        timeout_seconds: float = 30,
        startup_timeout_seconds: float = 60,
    ):
        self.platform = platform
        self.factory = factory
        self.timeout_seconds = timeout_seconds
        self.startup_timeout_seconds = startup_timeout_seconds
        self.scraper = None
        self.startup_error = None

    async def __aenter__(self):
        if self.factory is None:
            from src.scrapers import SCRAPERS

            self.factory = SCRAPERS[self.platform]
        try:
            self.scraper = self.factory()
            async with asyncio.timeout(self.startup_timeout_seconds):
                await self.scraper.__aenter__()
        except Exception as exc:
            self.startup_error = type(exc).__name__
            if self.scraper is not None:
                await self.scraper.aclose()
                self.scraper = None
        return self

    async def probe(self, url: str) -> LinkObservation:
        if self.scraper is None:
            return LinkObservation("INCONCLUSIVE", True, self.startup_error or "source_unavailable")
        try:
            async with asyncio.timeout(self.timeout_seconds):
                detail = await self.scraper.fetch_detail(url)
        except SourceListingRemovedError as exc:
            return LinkObservation("DEAD", False, exc.reason)
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in {404, 410}:
                return LinkObservation("DEAD", False, f"http_{status}")
            return LinkObservation("INCONCLUSIVE", True, type(exc).__name__)
        if (
            not detail.external_id
            or not detail.title
            or detail.price_eur is None
            or not detail.photo_urls
        ):
            return LinkObservation("INCONCLUSIVE", True, "listing_payload_unverified")
        return LinkObservation("ACTIVE")

    async def __aexit__(self, *_exc):
        if self.scraper is not None:
            await self.scraper.__aexit__(*_exc)
            self.scraper = None
