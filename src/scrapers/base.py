"""Abstract Scraper interface and shared data containers."""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, ClassVar

import httpx

from src.config import get_settings
from src.db.models import Platform

# HTTP-only platform fetches and photo downloads need a conservative desktop identity. Browser
# scrapers intentionally use Chromium's native UA instead: a hard-coded UA can drift from the
# bundled Playwright version and become an anti-bot fingerprint.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


@dataclass(slots=True)
class ListingPreview:
    """Minimal info from a list page — enough to decide whether to fetch details."""

    platform: Platform
    external_id: str
    url: str
    title: str | None = None


@dataclass(slots=True)
class ListingDetail:
    """Full listing data after fetching the detail page."""

    platform: Platform
    external_id: str
    url: str
    price_eur: int | None = None
    area_m2: float | None = None
    rooms: float | None = None
    district: str | None = None
    address_raw: str | None = None
    lat: float | None = None
    lon: float | None = None
    title: str | None = None
    description: str | None = None
    photo_urls: list[str] = field(default_factory=list)
    raw_payload: dict[str, Any] | None = None


class Scraper(abc.ABC):
    """Base contract every platform scraper must satisfy."""

    platform: ClassVar[Platform]
    list_url: ClassVar[str]

    # --- Live-smoke / structure-fingerprint contract (feature A2) ---
    # A healthy list page yields at least this many previews; fewer trips the smoke probe.
    MIN_PREVIEWS: ClassVar[int] = 5
    # A live detail must carry every one of these non-empty (the probe's "is the parser alive?"
    # check). price/area can legitimately be absent on a lone ad — the probe tolerates that by
    # checking several previews; see smoke.run_smoke.
    REQUIRED_DETAIL_FIELDS: ClassVar[tuple[str, ...]] = ("price_eur", "area_m2", "photo_urls")

    @classmethod
    def fingerprint(cls, list_html: str) -> dict[str, Any]:
        """Stable structure-only fingerprint of a list page (keys/types/source, NEVER values).

        Used to detect when a site silently changes its HTML/JSON shape: a drift in the returned
        dict is a WARN signal (markup changed, may still parse but is about to break). Default is
        empty; each scraper overrides with its own extraction. Deterministic & sorted so equal
        structures compare equal regardless of dict ordering.
        """
        return {}

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._owned_client = client is None
        self._client = client or self._make_client()

    @classmethod
    def _make_client(cls) -> httpx.AsyncClient:
        settings = get_settings()
        ua = settings.scraper_user_agent or DEFAULT_USER_AGENT
        proxy = settings.scraper_proxy_url or None
        return httpx.AsyncClient(
            headers={
                "User-Agent": ua,
                "Accept-Language": "sr,en;q=0.9,ru;q=0.8",
                # Browser-navigation headers a bare httpx client omits — their ABSENCE is a cheap
                # fingerprint for anti-bot layers (the nekretnine 403 of 13.07). Values track the
                # Chrome 126 / macOS identity in DEFAULT_USER_AGENT; keep them in sync if the UA
                # bumps. sec-ch-ua / Sec-Fetch-* are what a top-level document navigation sends.
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,image/apng,*/*;q=0.8"
                ),
                "Upgrade-Insecure-Requests": "1",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-User": "?1",
                "Sec-Fetch-Dest": "document",
                "sec-ch-ua": '"Google Chrome";v="126", "Chromium";v="126", "Not/A)Brand";v="24"',
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": '"macOS"',
            },
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
            proxy=proxy,
        )

    async def __aenter__(self) -> Scraper:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owned_client:
            await self._client.aclose()

    @abc.abstractmethod
    async def list_recent(
        self,
        max_pages: int = 5,
        *,
        known_ids: set[str] | None = None,
        early_stop_full_known_pages: int = 2,
    ) -> list[ListingPreview]:
        """Return preview rows from the recent-listings page(s).

        If `known_ids` is provided and the scraper sees `early_stop_full_known_pages` consecutive
        pages where every external_id is already in `known_ids`, it stops paginating — this gives
        cheap incremental scrapes: after the first full scan, subsequent runs hit only new items
        plus a small confirmation tail.
        """

    @abc.abstractmethod
    async def fetch_detail(self, url: str) -> ListingDetail:
        """Fetch and parse a single listing detail page."""
