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

    # Resilience knobs (overridable in tests to keep them fast). 4zida rate-limits aggressive
    # walks (429 + soft 200-without-data pages), so pace gentler than the other scrapers — a full
    # segmented walk is ~460 pages, 0.8s ≈ 6 min, irrelevant for a nightly job.
    fetch_attempts: ClassVar[int] = 4
    retry_backoff_sec: ClassVar[float] = 2.0
    page_delay_sec: ClassVar[float] = 0.8
    max_consecutive_failures: ClassVar[int] = 5

    # 4zida hard-caps every search at ~100 pages × 20 = ~2000 results (deeper ?strana values
    # re-serve earlier pages) while holding ~7000 Belgrade rentals. For full walks we segment by
    # price (?skuplje_od / ?jeftinije_od = min/max EUR), each band under the cap, merged with dedup.
    # Effective cap measured at ~99 pages / ~1980 uniques (page 100+ serves a near-static
    # sliding window) — the band-overflow warning threshold must not overstate safe capacity.
    page_cap: ClassVar[int] = 99
    results_cap: ClassVar[int] = 1900
    price_bands: ClassVar[tuple[tuple[int, int | None], ...]] = (
        (0, 300), (300, 400), (400, 500), (500, 600), (600, 700), (700, 800), (800, 900),
        (900, 1100), (1100, 1400), (1400, 2000), (2000, 3000), (3000, None),
    )

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.list_url = get_settings().cetiri_zida_list_url
        self._skipped_pages = 0
        # Site-reported total ("N oglasa" header) for the unfiltered search — coverage signal.
        self._site_total: int | None = None

    async def list_recent(
        self,
        max_pages: int = 5,
        *,
        known_ids: set[str] | None = None,
        early_stop_full_known_pages: int = 2,
    ) -> list[ListingPreview]:
        known_ids = known_ids or set()
        seen: set[str] = set()
        previews: list[ListingPreview] = []
        self._skipped_pages = 0
        self._site_total = None

        # Pass 1 — unfiltered walk (newest-first; also the only pass that sees price-less ads).
        await self._walk(
            self.list_url, max_pages, known_ids, early_stop_full_known_pages,
            seen, previews, is_base=True,
        )

        # Pass 2 — price-band segmentation for full walks the cap truncated.
        wants_full = max_pages > self.page_cap
        if wants_full and self._site_total is None:
            # Can't tell how much the cap hid — segmentation silently skipped would re-create
            # the "walk truncates to ~2000 and passes for healthy" failure. Surface it loudly;
            # the nightly sweep gate also treats a full walk without site_total as unhealthy.
            logger.error(
                "[4zida] full walk requested but the results-count header did not parse — "
                "site_total unknown, segmentation skipped, coverage unverifiable",
            )
        if self._site_total is not None and len(previews) > 1.5 * self._site_total:
            logger.error(
                "[4zida] parsed site_total=%d is implausible (collected %d) — separator change? "
                "Discarding the bogus total.",
                self._site_total, len(previews),
            )
            self._site_total = None
        if wants_full and self._site_total is not None and self._site_total > len(previews):
            logger.info(
                "[4zida] unfiltered walk capped at %d of %d site listings — walking %d price bands",
                len(previews), self._site_total, len(self.price_bands),
            )
            for lo, hi in self.price_bands:
                before = len(previews)
                band_total = await self._walk(
                    self._band_url(lo, hi), self.page_cap, known_ids,
                    early_stop_full_known_pages, seen, previews,
                )
                if band_total is not None and band_total > self.results_cap:
                    logger.warning(
                        "[4zida] band %s-%s holds %d listings > cap %d — narrow price_bands",
                        lo, hi if hi is not None else "∞", band_total, self.results_cap,
                    )
                logger.info(
                    "[4zida] band %s-%s: +%d new (band_total=%s, cumulative=%d)",
                    lo, hi if hi is not None else "∞", len(previews) - before, band_total, len(previews),
                )
            logger.info(
                "[4zida] segmented walk done: %d collected vs site_total=%s",
                len(previews), self._site_total,
            )
        return previews

    async def _walk(
        self,
        base_url: str,
        max_pages: int,
        known_ids: set[str],
        early_stop_full_known_pages: int,
        seen: set[str],
        previews: list[ListingPreview],
        *,
        is_base: bool = False,
    ) -> int | None:
        """Paginate one search (base or price band), accumulating into seen/previews.

        Returns the site-reported total for THIS search (None if not found).
        """
        consecutive_full_known = 0
        consecutive_failures = 0
        search_total: int | None = None
        # Never request past the site's hard pagination cap — deeper pages re-serve old content.
        limit = min(max_pages, self.page_cap)

        page = 1
        while page <= limit:
            sep = "&" if "?" in base_url else "?"
            url = base_url if page == 1 else f"{base_url}{sep}strana={page}"
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
                page += 1
                continue
            page_previews = self.parse_list(html)
            if search_total is None:
                search_total = self._total_from_html(html)
                if is_base and search_total is not None:
                    self._site_total = search_total
                # The site has no maxPages signal; derive the walk bound from its own result
                # count so we stop instead of reading re-served pages past the end.
                if search_total is not None and page_previews:
                    expected = -(-search_total // len(page_previews))  # ceil div
                    limit = min(limit, max(expected, 1))
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
            if not page_previews:
                # search_total == 0 is a genuinely empty band, not a soft failure to retry.
                if search_total and page < limit:
                    # 200 OK but no listings while the site says more pages exist — a soft
                    # rate-limit/error page, NOT the end. Treating it as end-of-pagination is
                    # how the first segmentation rollout silently collected 9% of 4zida.
                    consecutive_failures += 1
                    self._skipped_pages += 1
                    logger.warning(
                        "[4zida] page %d: empty parse below expected end (page %d/%d) — soft "
                        "failure (consecutive=%d)",
                        page, page, limit, consecutive_failures,
                    )
                    if consecutive_failures >= self.max_consecutive_failures:
                        logger.error("[4zida] aborting walk after %d consecutive soft failures", consecutive_failures)
                        break
                    if self.retry_backoff_sec > 0:
                        await asyncio.sleep(self.retry_backoff_sec * consecutive_failures)
                    page += 1
                    continue
                break  # structurally empty page = end of pagination
            # A page with real listings is the only success that resets the failure streak —
            # resetting on mere fetch-200 would let alternating soft-failures run forever.
            consecutive_failures = 0
            # NB: an all-duplicate page is NOT the end — price-band walks legitimately overlap
            # the unfiltered pass; the expected-pages bound above ends the walk instead.
            if known_ids and unknown_this_page == 0 and new_this_page > 0:
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
            page += 1
        return search_total

    def _band_url(self, lo: int, hi: int | None) -> str:
        sep = "&" if "?" in self.list_url else "?"
        params = f"skuplje_od={lo}"
        if hi is not None:
            params += f"&jeftinije_od={hi}"
        return f"{self.list_url}{sep}{params}"

    @staticmethod
    def _total_from_html(html: str) -> int | None:
        """Parse the "6.947 oglasa" results-count header.

        Tolerates dot/comma/space/nbsp thousands separators — a separator change that made the
        regex match only the trailing digits would silently understate the total, shrink the
        walk bound and skip segmentation while still passing the coverage health check.
        """
        m = re.search(r"(\d[\d.,  ]*)\s*oglas", html)
        if not m:
            return None
        digits = re.sub(r"\D", "", m.group(1))
        if not digits:
            return None
        try:
            return int(digits)
        except ValueError:
            return None

    async def fetch_detail(self, url: str) -> ListingDetail:
        resp = await self._client.get(url)
        resp.raise_for_status()
        return self.parse_detail(url, resp.text)

    async def _fetch_page(self, url: str) -> str | None:
        """GET a list page with bounded retries. Returns HTML, or None if it ultimately failed.

        429s get a much longer backoff — hammering through a rate limit only digs deeper."""
        for attempt in range(self.fetch_attempts):
            try:
                resp = await self._client.get(url)
                resp.raise_for_status()
                return resp.text
            except Exception as e:
                status = getattr(getattr(e, "response", None), "status_code", None)
                logger.warning(
                    "[4zida] GET %s attempt %d/%d failed: %s%s",
                    url, attempt + 1, self.fetch_attempts, type(e).__name__,
                    f" (HTTP {status})" if status else "",
                )
                if attempt + 1 < self.fetch_attempts and self.retry_backoff_sec > 0:
                    backoff = self.retry_backoff_sec * (attempt + 1)
                    if status == 429:
                        backoff = max(backoff * 5, 10.0 * (attempt + 1))
                    await asyncio.sleep(backoff)
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
    """Walk JSON-LD looking for the Apartment image array (full-size webp URLs).

    The COVER photo is referenced as {"@id": "...#primary-image"} inside the array, with the
    actual URL in a separate top-level ImageObject block — resolve it via an @id map, or every
    listing silently loses its cover (the photo Telegram reposts usually lead with), and
    single-photo listings end up with photo_urls=[] entirely.
    """
    id_map: dict[str, str] = {}
    for b in blocks:
        if b.get("@type") == "ImageObject" and isinstance(b.get("@id"), str):
            u = b.get("url") or b.get("contentUrl")
            if isinstance(u, str) and u.startswith("https://"):
                id_map[b["@id"]] = u

    photos: list[str] = []
    seen: set[str] = set()

    def _add(url: object) -> None:
        if isinstance(url, str) and url.startswith("https://") and url not in seen:
            seen.add(url)
            photos.append(url)

    for b in blocks:
        if b.get("@type") != "Apartment":
            continue
        images = b.get("image")
        if not isinstance(images, list):
            continue
        for img in images:
            if isinstance(img, str):
                _add(img)
            elif isinstance(img, dict):
                _add(img.get("url") or img.get("contentUrl") or id_map.get(img.get("@id") or ""))
    return photos
