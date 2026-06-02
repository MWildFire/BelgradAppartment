"""Scraper for nekretnine.rs.

The site is a Next.js app backed by the IndomioTech real-estate platform (same backend family as
immobiliare.it — note the Italian field names in the JSON). All listing data is embedded
server-side in the `<script id="__NEXT_DATA__" type="application/json">` blob, so plain httpx +
JSON parsing suffices — no JS execution, no DOM scraping.

List page — `props.pageProps.dehydratedState.queries[*].state.data`:
  - `results[]`: each entry has `realEstate` (id, title, price, properties[]) and `seo` (url, anchor).
  - `totalAds`, `currentPage`, `maxPages` describe the result set.
  - Pagination: `?pag=N` appended to the canonical list URL.

Detail page — `props.pageProps.detailData.realEstate`:
  - `properties[0]` carries `location` (lat/lon, address, micro/macrozone), `multimedia.photos`,
    `description`, `surface`, `rooms`.

Photo URLs are now `https://pic.nekretnine.rs/image/<id>/xxl.jpg` — no signed token needed (the old
img.nekretnine.rs/foto/<token>/<id> CDN scheme is gone).

History: nekretnine migrated from the old server-rendered `/stambeni-objekti/stanovi/...` HTML to
this Next.js app in late May 2026. The old list URL now 301-redirects to the canonical
`/izdavanje-stanova/beograd/` **and drops query params on the redirect**, so pagination only works
when the canonical URL is hit directly (see `nekretnine_list_url` in config).
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

BASE_URL = "https://www.nekretnine.rs"

# Tolerate extra/reordered <script> attributes (e.g. a CSP `nonce`) — matching the resilient
# pattern the 4zida scraper uses. The downstream json.loads + isinstance guards catch a bad capture.
_NEXT_DATA_RE = re.compile(
    r'<script[^>]*\bid="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL
)
# Captures one number, possibly with a grouping/decimal separator. nekretnine (IndomioTech) renders
# dot as the THOUSANDS separator and comma as the decimal — handled in `_number_from_text`.
_NUMBER_RE = re.compile(r"\d[\d.,]*")
_THOUSANDS_DOT_RE = re.compile(r"(?<=\d)\.(?=\d{3}(?:\D|$))")
_OGLAS_ID_RE = re.compile(r"/oglasi/(\d+)")


class NekretnineScraper(Scraper):
    platform: ClassVar[Platform] = Platform.NEKRETNINE

    # Resilience knobs (overridable in tests to keep them fast).
    fetch_attempts: ClassVar[int] = 3  # per-page GET attempts before declaring the page failed
    retry_backoff_sec: ClassVar[float] = 2.0  # sleep between per-page retries
    page_delay_sec: ClassVar[float] = 0.4  # pacing between pages on a long walk (avoids 429s)
    max_consecutive_failures: ClassVar[int] = 3  # abort the walk after this many in a row

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.list_url = get_settings().nekretnine_list_url
        # Pages we couldn't fetch and skipped past — surfaced via ScrapeResult so a partial
        # under-run doesn't masquerade as a healthy walk.
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
        consecutive_full_known = 0  # pages where every entry was already in known_ids
        consecutive_failures = 0  # pages we couldn't fetch at all
        site_max_pages: int | None = None
        self._skipped_pages = 0

        page = 1
        while page <= max_pages:
            html = await self._fetch_page(self._page_url(page))
            if html is None:
                # Transient fetch failure: retry a few pages before giving up. A single 429/timeout
                # must NOT abort the whole walk (the old `except: break` silently truncated the
                # active set and is what let nekretnine drop to zero unnoticed). But we DO count the
                # skip so a partial under-run is surfaced rather than silently passing for healthy.
                consecutive_failures += 1
                self._skipped_pages += 1
                logger.warning(
                    "[nekretnine] page %d fetch failed (consecutive=%d, skipped_total=%d)",
                    page, consecutive_failures, self._skipped_pages,
                )
                if consecutive_failures >= self.max_consecutive_failures:
                    logger.error(
                        "[nekretnine] aborting walk after %d consecutive fetch failures at page %d",
                        consecutive_failures, page,
                    )
                    break
                page += 1
                continue
            consecutive_failures = 0

            data = self._list_payload(html)
            if data is None:
                # 200 OK but no listing payload — almost certainly a site-structure change.
                # Log loudly: this is exactly the silent-failure mode we want surfaced.
                logger.error(
                    "[nekretnine] page %d: __NEXT_DATA__ list payload missing — site structure changed?",
                    page,
                )
                break
            if site_max_pages is None:
                mp = data.get("maxPages")
                if isinstance(mp, int) and mp > 0:
                    site_max_pages = mp

            new_this_page = 0
            unknown_this_page = 0
            for prev in self._previews_from_payload(data):
                if prev.external_id in seen:
                    continue
                seen.add(prev.external_id)
                previews.append(prev)
                new_this_page += 1
                if prev.external_id not in known_ids:
                    unknown_this_page += 1

            if new_this_page == 0:
                break  # empty page = end of pagination
            if known_ids and unknown_this_page == 0:
                consecutive_full_known += 1
                if consecutive_full_known >= early_stop_full_known_pages:
                    logger.info(
                        "[nekretnine] incremental early-stop at page %d (%d full-known pages)",
                        page, consecutive_full_known,
                    )
                    break
            else:
                consecutive_full_known = 0
            if site_max_pages is not None and page >= site_max_pages:
                break
            if self.page_delay_sec > 0:
                await asyncio.sleep(self.page_delay_sec)  # pace a long walk to avoid 429s
            page += 1
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
                    "[nekretnine] GET %s attempt %d/%d failed: %s",
                    url, attempt + 1, self.fetch_attempts, type(e).__name__,
                )
                if attempt + 1 < self.fetch_attempts and self.retry_backoff_sec > 0:
                    await asyncio.sleep(self.retry_backoff_sec * (attempt + 1))
        return None

    def _page_url(self, page: int) -> str:
        if page <= 1:
            return self.list_url
        sep = "&" if "?" in self.list_url else "?"
        return f"{self.list_url}{sep}pag={page}"

    # ---- Pure parsers (testable on fixtures) ----

    @classmethod
    def parse_list(cls, html: str) -> list[ListingPreview]:
        """Extract preview rows from a list page's __NEXT_DATA__ blob."""
        data = cls._list_payload(html)
        if data is None:
            return []
        return cls._previews_from_payload(data)

    @classmethod
    def parse_detail(cls, url: str, html: str) -> ListingDetail:
        external_id = cls._external_id_from_url(url)
        nxt = cls._extract_next_data(html)
        real_estate: Any = None
        if isinstance(nxt, dict):
            try:
                real_estate = nxt["props"]["pageProps"]["detailData"]["realEstate"]
            except (KeyError, TypeError):
                real_estate = None
        if not isinstance(real_estate, dict):
            logger.warning("[nekretnine] detail %s: no realEstate payload", url)
            return ListingDetail(platform=Platform.NEKRETNINE, external_id=external_id, url=url)
        return cls._detail_from_real_estate(url, external_id, real_estate)

    # ---- payload helpers ----

    @staticmethod
    def _extract_next_data(html: str) -> dict[str, Any] | None:
        m = _NEXT_DATA_RE.search(html)
        if not m:
            return None
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    @classmethod
    def _list_payload(cls, html: str) -> dict[str, Any] | None:
        """Return the `real-estate-list` data object (results/maxPages/...) from __NEXT_DATA__."""
        nxt = cls._extract_next_data(html)
        if not nxt:
            return None
        try:
            queries = nxt["props"]["pageProps"]["dehydratedState"]["queries"]
        except (KeyError, TypeError):
            return None
        if not isinstance(queries, list):
            return None
        for q in queries:
            if not isinstance(q, dict):
                continue
            data = (q.get("state") or {}).get("data")
            if isinstance(data, dict) and isinstance(data.get("results"), list):
                return data
        return None

    @staticmethod
    def _previews_from_payload(data: dict[str, Any]) -> list[ListingPreview]:
        previews: list[ListingPreview] = []
        seen: set[str] = set()
        for result in data.get("results", []):
            if not isinstance(result, dict):
                continue
            real_estate = result.get("realEstate")
            if not isinstance(real_estate, dict):
                continue
            external_id = str(real_estate.get("id") or "").strip()
            if not external_id or external_id in seen:
                continue
            seo = result.get("seo") if isinstance(result.get("seo"), dict) else {}
            url = seo.get("url")
            if not isinstance(url, str) or not url:
                url = f"{BASE_URL}/oglasi/{external_id}/"
            title = real_estate.get("title") or seo.get("anchor")
            seen.add(external_id)
            previews.append(
                ListingPreview(
                    platform=Platform.NEKRETNINE,
                    external_id=external_id,
                    url=urljoin(BASE_URL, url),
                    title=title if isinstance(title, str) else None,
                )
            )
        return previews

    @classmethod
    def _detail_from_real_estate(
        cls, url: str, external_id: str, real_estate: dict[str, Any]
    ) -> ListingDetail:
        # Prefer the canonical numeric id from the payload; keep parity with parse_list previews.
        rid = real_estate.get("id")
        if rid is not None:
            external_id = str(rid)

        props = real_estate.get("properties")
        prop: dict[str, Any] = props[0] if isinstance(props, list) and props and isinstance(props[0], dict) else {}
        location = prop.get("location") if isinstance(prop.get("location"), dict) else {}

        price_eur = cls._price(real_estate.get("price")) or cls._price(prop.get("price"))

        area_m2 = _safe_float(prop.get("surfaceValue"))
        if area_m2 is None:
            area_m2 = cls._number_from_text(prop.get("surface"))

        rooms = _safe_float(prop.get("roomsValue"))
        if rooms is None:
            rooms = cls._number_from_text(prop.get("rooms"))

        district = location.get("microzone") or location.get("macrozone")
        address_raw = location.get("address")
        lat = _safe_float(location.get("latitude"))
        lon = _safe_float(location.get("longitude"))

        title = real_estate.get("title")
        description = prop.get("description") or prop.get("defaultDescription")
        if isinstance(description, str):
            description = re.sub(r"\s+", " ", description).strip() or None
        else:
            description = None

        return ListingDetail(
            platform=Platform.NEKRETNINE,
            external_id=external_id,
            url=url,
            price_eur=price_eur,
            area_m2=area_m2,
            rooms=rooms,
            district=district if isinstance(district, str) else None,
            address_raw=address_raw if isinstance(address_raw, str) else None,
            lat=lat,
            lon=lon,
            title=title if isinstance(title, str) else None,
            description=description,
            photo_urls=cls._photo_urls(prop),
            raw_payload={
                "contract": real_estate.get("contract"),
                "typology": (real_estate.get("typology") or {}).get("name")
                if isinstance(real_estate.get("typology"), dict)
                else None,
            },
        )

    @staticmethod
    def _photo_urls(prop: dict[str, Any]) -> list[str]:
        """Collect the largest available URL per photo, deduped, preserving order."""
        out: list[str] = []
        seen: set[str] = set()
        multimedia = prop.get("multimedia") if isinstance(prop.get("multimedia"), dict) else {}
        photos = multimedia.get("photos") if isinstance(multimedia.get("photos"), list) else []
        for ph in photos:
            if not isinstance(ph, dict):
                continue
            urls = ph.get("urls") if isinstance(ph.get("urls"), dict) else {}
            best = urls.get("large") or urls.get("medium") or urls.get("small")
            if isinstance(best, str) and best.startswith("http") and best not in seen:
                seen.add(best)
                out.append(best)
        if not out:
            primary = prop.get("photo") if isinstance(prop.get("photo"), dict) else {}
            urls = primary.get("urls") if isinstance(primary.get("urls"), dict) else {}
            best = urls.get("large") or urls.get("medium")
            if isinstance(best, str) and best.startswith("http"):
                out.append(best)
        return out

    @staticmethod
    def _external_id_from_url(url: str) -> str:
        m = _OGLAS_ID_RE.search(url)
        if m:
            return m.group(1)
        return url.split("?", 1)[0].rstrip("/").split("/")[-1]

    @staticmethod
    def _price(price_obj: object) -> int | None:
        if not isinstance(price_obj, dict):
            return None
        return _safe_int(price_obj.get("value"))

    @staticmethod
    def _number_from_text(value: object) -> float | None:
        """Parse a number from site text like "50 m²", "1.250 m²" (1250), or "50,5 m²" (50.5).

        Dot is the thousands separator on this platform, comma the decimal — so "1.250" must not
        become 1.25. We strip grouping dots first, then treat comma as the decimal point.
        """
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        m = _NUMBER_RE.search(str(value))
        if not m:
            return None
        token = _THOUSANDS_DOT_RE.sub("", m.group(0)).replace(",", ".")
        try:
            return float(token)
        except ValueError:
            return None


# ---- shared utilities ----


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
