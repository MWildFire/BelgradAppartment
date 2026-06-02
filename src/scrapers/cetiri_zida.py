"""Scraper for 4zida.rs.

The list page renders a single JSON-LD `ItemList` block with full preview data — price, area,
rooms, primary image, URL. The detail page has a richer JSON-LD graph (Apartment + Place + Offer)
with full image array and rounded GeoCoordinates.

So we parse JSON-LD throughout — no DOM scraping, very stable.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, ClassVar
from urllib.parse import urljoin

from src.config import get_settings
from src.db.models import Platform
from src.scrapers.base import ListingDetail, ListingPreview, Scraper

logger = logging.getLogger(__name__)

BASE_URL = "https://www.4zida.rs"
_JSON_LD_RE = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.DOTALL
)


class CetiriZidaScraper(Scraper):
    platform: ClassVar[Platform] = Platform.CETIRI_ZIDA

    # Resilience knobs (overridable in tests to keep them fast).
    fetch_attempts: ClassVar[int] = 3
    retry_backoff_sec: ClassVar[float] = 2.0
    page_delay_sec: ClassVar[float] = 0.4
    max_consecutive_failures: ClassVar[int] = 3

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.list_url = get_settings().cetiri_zida_list_url
        self._skipped_pages = 0

    async def list_recent(
        self,
        max_pages: int = 5,
        *,
        known_ids: set[str] | None = None,
        early_stop_full_known_pages: int = 2,
    ) -> list[ListingPreview]:
        seen: set[str] = set()
        previews: list[ListingPreview] = []
        known_ids = known_ids or set()
        consecutive_full_known = 0
        consecutive_failures = 0
        self._skipped_pages = 0
        for page in range(1, max_pages + 1):
            url = self.list_url if page == 1 else f"{self.list_url}?strana={page}"
            html = await self._fetch_page(url)
            if html is None:
                # A single transient error (429/timeout/5xx) must not abort the whole walk —
                # that silently truncates the active set. Retry a few pages, then give up. Count
                # the skip so a partial under-run is surfaced rather than passing for healthy.
                consecutive_failures += 1
                self._skipped_pages += 1
                logger.warning(
                    "[4zida] page %d fetch failed (consecutive=%d, skipped_total=%d)",
                    page, consecutive_failures, self._skipped_pages,
                )
                if consecutive_failures >= self.max_consecutive_failures:
                    logger.error("[4zida] aborting walk after %d consecutive fetch failures", consecutive_failures)
                    break
                continue
            consecutive_failures = 0
            page_previews = self.parse_list(html)
            new_this_page = 0
            unknown_this_page = 0
            for prev in page_previews:
                if prev.external_id in seen:
                    continue
                seen.add(prev.external_id)
                previews.append(prev)
                new_this_page += 1
                if prev.external_id not in known_ids:
                    unknown_this_page += 1
            if new_this_page == 0:
                break
            if known_ids and unknown_this_page == 0:
                consecutive_full_known += 1
                if consecutive_full_known >= early_stop_full_known_pages:
                    logger.info(
                        "[4zida] incremental early-stop at page %d", page,
                    )
                    break
            else:
                consecutive_full_known = 0
            if self.page_delay_sec > 0:
                await asyncio.sleep(self.page_delay_sec)  # pace the walk to avoid 429s
        return previews

    async def fetch_detail(self, url: str) -> ListingDetail:
        resp = await self._client.get(url)
        resp.raise_for_status()
        return self.parse_detail(url, resp.text)

    async def _fetch_page(self, url: str) -> str | None:
        """GET a list page with bounded retries. Returns HTML, or None if it ultimately failed."""
        for attempt in range(self.fetch_attempts):
            try:
                resp = await self._client.get(url)
                resp.raise_for_status()
                return resp.text
            except Exception as e:
                logger.warning(
                    "[4zida] GET %s attempt %d/%d failed: %s",
                    url, attempt + 1, self.fetch_attempts, type(e).__name__,
                )
                if attempt + 1 < self.fetch_attempts and self.retry_backoff_sec > 0:
                    await asyncio.sleep(self.retry_backoff_sec * (attempt + 1))
        return None

    # ---- Pure parsers ----

    @classmethod
    def parse_list(cls, html: str) -> list[ListingPreview]:
        blocks = _extract_json_ld(html)
        item_list = _find_first(blocks, lambda b: b.get("@type") == "ItemList")
        if not item_list:
            return []
        elements = item_list.get("itemListElement", [])
        previews: list[ListingPreview] = []
        for el in elements:
            if not isinstance(el, dict):
                continue
            item = el.get("item") if isinstance(el.get("item"), dict) else el
            url = item.get("url") or el.get("url")
            if not isinstance(url, str):
                continue
            external_id = _external_id_from_url(url)
            if not external_id:
                continue
            title = item.get("name") if isinstance(item.get("name"), str) else None
            previews.append(
                ListingPreview(
                    platform=Platform.CETIRI_ZIDA,
                    external_id=external_id,
                    url=urljoin(BASE_URL, url),
                    title=title,
                )
            )
        return previews

    @classmethod
    def parse_detail(cls, url: str, html: str) -> ListingDetail:
        external_id = _external_id_from_url(url) or url.rstrip("/").split("/")[-1]
        blocks = _extract_json_ld(html)

        apartment = _find_first(blocks, lambda b: b.get("@type") == "Apartment")
        offer = _find_first(
            blocks,
            lambda b: b.get("@type") == "Offer"
            and isinstance(b.get("@id"), str)
            and "#offer" in b["@id"],
        )
        # Place whose @id ends with #place (not the org's location)
        place = _find_first(
            blocks,
            lambda b: b.get("@type") == "Place"
            and isinstance(b.get("@id"), str)
            and b["@id"].endswith("#place"),
        )
        listing = _find_first(blocks, lambda b: b.get("@type") == "RealEstateListing")

        price_eur = _safe_int(offer.get("price")) if offer else None
        title = (
            (apartment or {}).get("name") or (listing or {}).get("name")
        )
        description = (apartment or {}).get("description") or (listing or {}).get("description")

        area_m2: float | None = None
        rooms: float | None = None
        address_raw: str | None = None
        if apartment:
            # Detail page uses `area` (QuantitativeValue); list page uses `floorSize`. Handle both.
            for area_key in ("area", "floorSize"):
                area_field = apartment.get(area_key)
                if isinstance(area_field, dict) and area_field.get("value") is not None:
                    area_m2 = _safe_float(area_field.get("value"))
                    break
                if isinstance(area_field, int | float):
                    area_m2 = float(area_field)
                    break
            rooms_val = apartment.get("numberOfRooms")
            if rooms_val is not None:
                rooms = _safe_float(rooms_val)
            addr = apartment.get("address")
            if isinstance(addr, dict):
                address_raw = addr.get("streetAddress") or None

        lat: float | None = None
        lon: float | None = None
        # Apartment may carry geo directly; otherwise look at the #place block.
        for source in (apartment, place):
            if not source:
                continue
            geo = source.get("geo")
            if isinstance(geo, dict):
                lat = _safe_float(geo.get("latitude"))
                lon = _safe_float(geo.get("longitude"))
                if lat is not None and lon is not None:
                    break

        photo_urls = _collect_4zida_photos(blocks)
        district = _district_from_url(url)

        return ListingDetail(
            platform=Platform.CETIRI_ZIDA,
            external_id=external_id,
            url=url,
            price_eur=price_eur,
            area_m2=area_m2,
            rooms=rooms,
            district=district,
            address_raw=address_raw,
            lat=lat,
            lon=lon,
            title=title,
            description=description,
            photo_urls=photo_urls,
            raw_payload={"json_ld_count": len(blocks)},
        )


# ---- shared utilities ----


def _extract_json_ld(html: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw in _JSON_LD_RE.findall(html):
        text = raw.strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            cleaned = re.sub(r"[\x00-\x1f\x7f]", "", text)
            try:
                data = json.loads(cleaned)
            except json.JSONDecodeError:
                continue
        if isinstance(data, dict):
            out.append(data)
            graph = data.get("@graph")
            if isinstance(graph, list):
                for item in graph:
                    if isinstance(item, dict):
                        out.append(item)
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    out.append(item)
    return out


def _find_first(
    blocks: list[dict[str, Any]], predicate: Any
) -> dict[str, Any] | None:
    for b in blocks:
        try:
            if predicate(b):
                return b
        except Exception:
            continue
    return None


def _external_id_from_url(url: str) -> str | None:
    tail = url.rstrip("/").split("/")[-1]
    if re.fullmatch(r"[0-9a-fA-F]{16,}", tail):
        return tail
    return tail if tail else None


def _district_from_url(url: str) -> str | None:
    # URLs look like /izdavanje-stanova/<lokacija>/<tip>/<id>
    # Take the <lokacija> slug and humanise it.
    parts = [p for p in url.replace(BASE_URL, "").split("/") if p]
    if len(parts) >= 2 and parts[0] == "izdavanje-stanova":
        slug = parts[1]
        return slug.replace("-", " ").title()
    return None


def _safe_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(float(str(value).replace(",", ".")))
    except (TypeError, ValueError):
        return None


def _safe_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _collect_4zida_photos(blocks: list[dict[str, Any]]) -> list[str]:
    """Walk JSON-LD looking for the Apartment image array (full-size webp URLs)."""
    photos: list[str] = []
    seen: set[str] = set()
    for b in blocks:
        if b.get("@type") != "Apartment":
            continue
        images = b.get("image")
        if not isinstance(images, list):
            continue
        for img in images:
            if isinstance(img, str) and img.startswith("https://") and img not in seen:
                seen.add(img)
                photos.append(img)
            elif isinstance(img, dict):
                url = img.get("url") or img.get("contentUrl")
                if isinstance(url, str) and url.startswith("https://") and url not in seen:
                    seen.add(url)
                    photos.append(url)
    return photos
