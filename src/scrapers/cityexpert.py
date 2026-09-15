"""Scraper for cityexpert.rs.

cityexpert is an agency running an Angular SPA backed by a clean JSON API — no JS execution,
no DOM scraping, no Playwright. We use httpx + JSON throughout (like nekretnine / 4zida).

List API
--------
`GET https://cityexpert.rs/api/Search?req=<url-encoded JSON>` returns:
  - `result[]`: each preview has `propId`, `price`, `size` (m²), `structure` (room code),
    `street`, `municipality`, `coverPhoto` (filename), `location` ("lat, lng"), `rentOrSale`.
  - `info`: `{pageNumber, pageCount, documentCount, ...}` — drives pagination + coverage.
The `req` object mirrors the SPA's default search state; the rental case is
`rentOrSale:"r"`, Belgrade `cityId:1`, `serviceType:"p"`, paginated via `currentPage`.

Detail API
----------
`GET https://cityexpert.rs/api/PropertyView/{propId}/r` returns the full property dict:
  `price`, `size`, `structure`, `street`, `municipality`, `selectedNeighbourhoods`,
  `mapLat`/`mapLng`, and `onsite.imageFiles[]` / `onsite.coverImage` (photo filenames).

Photo CDN
---------
Image URLs are reconstructed as
  `https://img.cityexpert.rs/properties/<width>/<bucket>/<propId>/slike/<filename>`
where `<bucket> = (propId // 1000) * 1000` and `<width>` is `1920x` (largest; `470x` is the
thumbnail). The filenames live in the API as `onsite.imageFiles` (detail) / `coverPhoto`
(list) — bare names, no host.

URL stored
----------
We store the public, browsable detail URL
  `https://cityexpert.rs/izdavanje-nekretnina/beograd/{propId}/{slug}`
(the slug-less form 302→/404), reconstructing the canonical slug from
`{structure-word}-stan-{street-slug}-{municipality-slug}` exactly as the site does. The slug
can drift, so — mirroring the A1 nekretnine fix — `fetch_detail` derives the propId from the
URL and re-fetches via the slug-INDEPENDENT API `/api/PropertyView/{propId}/r`. The public URL
is for display; the API URL is the robust re-fetch path.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import unicodedata
from typing import Any, ClassVar
from urllib.parse import quote, urlsplit

from src.config import get_settings
from src.db.models import Platform
from src.scrapers.base import ListingDetail, ListingPreview, Scraper, SourceListingInactiveError

logger = logging.getLogger(__name__)

PUBLIC_BASE = "https://cityexpert.rs"
IMG_BASE = "https://img.cityexpert.rs"
IMG_WIDTH = "1920x"  # largest served size (the only other valid one is the 470x thumbnail)

_PROPID_FROM_URL_RE = re.compile(r"/beograd/(\d+)(?:/|$)")

# structure code → Serbian slug word the site uses in the canonical detail URL.
_STRUCTURE_SLUG: dict[str, str] = {
    "0.5": "garsonjera",
    "1.0": "jednosoban",
    "1.5": "jednoiposoban",
    "2.0": "dvosoban",
    "2.5": "dvoiposoban",
    "3.0": "trosoban",
    "3.5": "troiposoban",
    "4.0": "cetvorosoban",
    "5+": "petosoban-i-veci",
}


class CityExpertScraper(Scraper):
    platform: ClassVar[Platform] = Platform.CITYEXPERT

    # Resilience knobs (overridable in tests to keep them fast).
    fetch_attempts: ClassVar[int] = 3
    retry_backoff_sec: ClassVar[float] = 2.0
    page_delay_sec: ClassVar[float] = 0.4
    max_consecutive_failures: ClassVar[int] = 3

    # The API paginates cleanly (info.pageCount). At ~1200 Belgrade rentals × 12/page that is
    # ~100 pages — well within reach of a single walk, so no price-band segmentation is needed
    # (unlike nekretnine/4zida which hard-cap at ~2000 results).
    page_cap: ClassVar[int] = 250

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        settings = get_settings()
        self.list_url = settings.cityexpert_list_url
        self._api_base = settings.cityexpert_api_base.rstrip("/")
        self._city_id = settings.cityexpert_city_id
        self._results_per_page = settings.cityexpert_results_per_page
        # Surfaced via ScrapeResult so a partial under-run isn't mistaken for a healthy walk.
        self._skipped_pages = 0
        self._site_total: int | None = None  # info.documentCount of the unfiltered search
        self._last_status: int | None = None

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
        self.reset_crawl_evidence()
        self._skipped_pages = 0
        self._site_total = None

        consecutive_full_known = 0
        consecutive_failures = 0
        page_count: int | None = None
        limit = min(max_pages, self.page_cap)

        page = 1
        while page <= limit:
            self.record_page_attempt(observed=len(previews))
            text = await self._fetch_page(self._search_url(page))
            if text is None:
                # Transient fetch failure: skip a few pages before giving up. A single
                # 429/timeout must NOT abort the whole walk (that silently truncates the set);
                # but we count the skip so a partial under-run is surfaced.
                consecutive_failures += 1
                self._skipped_pages += 1
                self.record_crawl_failure("page_fetch_failed")
                logger.warning(
                    "[cityexpert] page %d fetch failed (consecutive=%d, skipped_total=%d)",
                    page, consecutive_failures, self._skipped_pages,
                )
                if consecutive_failures >= self.max_consecutive_failures:
                    logger.error(
                        "[cityexpert] aborting walk after %d consecutive fetch failures at page %d",
                        consecutive_failures, page,
                    )
                    break
                page += 1
                continue
            consecutive_failures = 0

            payload = self._parse_payload(text)
            if payload is None:
                self.record_crawl_failure("page_payload_missing")
                # 200 OK but no Search payload — almost certainly an API/structure change.
                logger.error(
                    "[cityexpert] page %d: Search result payload missing — API structure changed?",
                    page,
                )
                break

            info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
            self.record_source_total(info.get("documentCount"))
            if info.get("pageNumber") != page:
                self.record_crawl_failure("pagination_mismatch")
                break
            if page_count is None:
                pc = info.get("pageCount")
                if isinstance(pc, int) and pc > 0:
                    page_count = pc

            page_previews = self._previews_from_payload(payload)
            if not page_previews:
                if self._site_total != 0:
                    self.record_crawl_failure("unexpected_empty_page")
                break  # structurally empty page = end of pagination

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

            if known_ids and unknown_this_page == 0 and new_this_page > 0:
                consecutive_full_known += 1
                if consecutive_full_known >= early_stop_full_known_pages:
                    self._stopped_known = True
                    logger.info(
                        "[cityexpert] incremental early-stop at page %d (%d full-known pages)",
                        page, consecutive_full_known,
                    )
                    break
            else:
                consecutive_full_known = 0

            if page_count is not None and page >= page_count:
                break
            if self.page_delay_sec > 0:
                await asyncio.sleep(self.page_delay_sec)
            page += 1
        self.finish_crawl_evidence(len(previews))
        return previews

    async def fetch_detail(self, url: str) -> ListingDetail:
        # Re-fetch via the slug-INDEPENDENT API (immune to slug drift), not the public URL.
        external_id = self._external_id_from_url(url)
        resp = await self._client.get(f"{self._api_base}/api/PropertyView/{external_id}/r")
        resp.raise_for_status()
        return self.parse_detail(url, resp.text)

    async def _fetch_page(self, url: str) -> str | None:
        """GET a Search page with bounded retries. Returns body text, or None on final failure.

        Sets `self._last_status` to the final HTTP status (None on transport errors). 429s get a
        much longer backoff — hammering through a rate limit only digs deeper.
        """
        self._last_status = None
        for attempt in range(self.fetch_attempts):
            try:
                resp = await self._client.get(url)
                self._last_status = resp.status_code
                resp.raise_for_status()
                return resp.text
            except Exception as e:
                status = getattr(getattr(e, "response", None), "status_code", None)
                logger.warning(
                    "[cityexpert] GET %s attempt %d/%d failed: %s%s",
                    url, attempt + 1, self.fetch_attempts, type(e).__name__,
                    f" (HTTP {status})" if status else "",
                )
                if attempt + 1 < self.fetch_attempts and self.retry_backoff_sec > 0:
                    backoff = self.retry_backoff_sec * (attempt + 1)
                    if status == 429:
                        backoff = max(backoff * 5, 10.0 * (attempt + 1))
                    await asyncio.sleep(backoff)
        return None

    def _search_url(self, page: int) -> str:
        """Build GET /api/Search?req=<url-encoded JSON> for the given page (rentals, Belgrade)."""
        req = {
            "serviceType": "p",
            "ptId": [],
            "cityId": self._city_id,
            "rentOrSale": "r",
            "currentPage": page,
            "resultsPerPage": self._results_per_page,
            "avFrom": False,
            "underConstruction": False,
            "minPrice": None,
            "maxPrice": None,
            "minSize": None,
            "maxSize": None,
            "searchSource": "regular",
            "sort": "datedsc",
            "polygonsArray": [],
            "structure": [],
        }
        encoded = quote(json.dumps(req, separators=(",", ":")))
        return f"{self._api_base}/api/Search?req={encoded}"

    # ---- Pure parsers (testable on fixtures) ----

    @classmethod
    def parse_list(cls, json_text: str) -> list[ListingPreview]:
        payload = cls._parse_payload(json_text)
        if payload is None:
            return []
        return cls._previews_from_payload(payload)

    @classmethod
    def fingerprint(cls, list_json: str) -> dict[str, Any]:
        """Structure-only fingerprint of the Search result payload (keys, never values)."""
        payload = cls._parse_payload(list_json)
        if payload is None:
            return {}
        result = payload.get("result")
        if not isinstance(result, list) or not result or not isinstance(result[0], dict):
            return {}
        return {
            "source": "cityexpert-api",
            "result_keys": sorted(result[0].keys()),
            "n": len(result),
        }

    @classmethod
    def parse_detail(cls, url: str, json_text: str) -> ListingDetail:
        external_id = cls._external_id_from_url(url)
        try:
            data = json.loads(json_text)
        except (json.JSONDecodeError, TypeError):
            data = None
        if not isinstance(data, dict):
            logger.warning("[cityexpert] detail %s: no PropertyView payload", url)
            return ListingDetail(platform=Platform.CITYEXPERT, external_id=external_id, url=url)

        # PropertyView retains full prices/photos after a rental is no longer offered.
        # HTTP 200 and a complete photo album are not evidence of availability.
        parsed = urlsplit(url)
        prop_id = data.get("propId")
        identity_matches = (
            parsed.scheme == "https"
            and parsed.netloc == "cityexpert.rs"
            and not parsed.query and not parsed.fragment
            and re.fullmatch(r"/izdavanje-nekretnina/beograd/[1-9][0-9]*(?:/[^/]+)?/?", parsed.path)
            and type(prop_id) is int and str(prop_id) == external_id
            and type(data.get("cityId")) is int and data["cityId"] == 1
            and data.get("rentOrSale") == "r"
        )
        if not identity_matches:
            raise ValueError("cityexpert_detail_identity_unconfirmed")
        status = data.get("status")
        if status in ("FINISHED", "NOT-AVAILABLE", "KAPARISANA-RENT"):
            # Each exact status is independently confirmed by the source UI's disabled
            # "Nije u ponudi" action. Temporary reservations can reappear in later lists.
            raise SourceListingInactiveError("cityexpert_listing_not_offered")
        if status != "PUBLISHED":
            raise ValueError("cityexpert_availability_unconfirmed")

        price_eur = _safe_int(data.get("price"))
        area_m2 = _safe_float(data.get("size"))
        rooms = cls._structure_to_rooms(data.get("structure"))

        district = cls._district(data)
        address_raw = data.get("street")
        lat = _safe_float(data.get("mapLat"))
        lon = _safe_float(data.get("mapLng"))

        return ListingDetail(
            platform=Platform.CITYEXPERT,
            external_id=external_id,
            url=url,
            price_eur=price_eur,
            area_m2=area_m2,
            rooms=rooms,
            district=district,
            address_raw=address_raw if isinstance(address_raw, str) else None,
            lat=lat,
            lon=lon,
            title=cls._title(data),
            description=None,  # the API ships add* descriptions but they're often null
            photo_urls=cls._photo_urls(data),
            raw_payload={
                "caseType": data.get("caseType"),
                "status": data.get("status"),
                "rentOrSale": data.get("rentOrSale"),
                "structure": data.get("structure"),
            },
        )

    # ---- payload helpers ----

    @staticmethod
    def _parse_payload(json_text: str) -> dict[str, Any] | None:
        """Return the Search payload dict (must carry a `result` list) or None."""
        try:
            data = json.loads(json_text)
        except (json.JSONDecodeError, TypeError):
            return None
        if isinstance(data, dict) and isinstance(data.get("result"), list):
            return data
        return None

    @classmethod
    def _previews_from_payload(cls, payload: dict[str, Any]) -> list[ListingPreview]:
        previews: list[ListingPreview] = []
        seen: set[str] = set()
        for item in payload.get("result", []):
            if not isinstance(item, dict):
                continue
            prop_id = item.get("propId")
            external_id = str(prop_id).strip() if prop_id is not None else ""
            if not external_id or external_id in seen:
                continue
            seen.add(external_id)
            street = item.get("street") if isinstance(item.get("street"), str) else None
            municipality = (
                item.get("municipality") if isinstance(item.get("municipality"), str) else None
            )
            url = cls._public_url(external_id, item.get("structure"), street, municipality)
            title = ", ".join(p for p in (street, municipality) if p) or None
            previews.append(
                ListingPreview(
                    platform=Platform.CITYEXPERT,
                    external_id=external_id,
                    url=url,
                    title=title,
                )
            )
        return previews

    @classmethod
    def _public_url(
        cls, prop_id: str, structure: object, street: str | None, municipality: str | None
    ) -> str:
        """Reconstruct the canonical browsable detail URL exactly as the site builds it."""
        word = _STRUCTURE_SLUG.get(str(structure)) if structure is not None else None
        parts = [word, "stan", _slugify(street), _slugify(municipality)]
        slug = "-".join(p for p in parts if p)
        base = f"{PUBLIC_BASE}/izdavanje-nekretnina/beograd/{prop_id}"
        return f"{base}/{slug}" if slug else base

    @staticmethod
    def _structure_to_rooms(structure: object) -> float | None:
        """Map cityexpert's structure code to a room count.

        Codes are mostly the room count as a decimal string ("1.0", "2.5", "3.5"). The two
        non-numeric edges: "5+" → 5.0 (5-rooms-and-larger) and "OTHER" → None. "0.5" is a
        studio (garsonjera) → 0.5. Anything unrecognised → None (defensive).
        """
        if structure is None:
            return None
        s = str(structure).strip()
        if s == "5+":
            return 5.0
        try:
            return float(s)
        except ValueError:
            return None

    @staticmethod
    def _district(data: dict[str, Any]) -> str | None:
        """District = municipality + the selected neighbourhood when present.

        e.g. municipality "Vračar", selectedNeighbourhoods ["Cvetni trg ..."] →
        "Vračar — Cvetni trg ...". Falls back to municipality alone.
        """
        municipality = data.get("municipality")
        municipality = municipality.strip() if isinstance(municipality, str) else None
        selected = data.get("selectedNeighbourhoods")
        neigh: str | None = None
        if isinstance(selected, list):
            for s in selected:
                if isinstance(s, str) and s.strip():
                    neigh = s.strip()
                    break
        if municipality and neigh:
            return f"{municipality} — {neigh}"
        return municipality or neigh

    @staticmethod
    def _title(data: dict[str, Any]) -> str | None:
        street = data.get("street") if isinstance(data.get("street"), str) else None
        municipality = (
            data.get("municipality") if isinstance(data.get("municipality"), str) else None
        )
        title = ", ".join(p for p in (street, municipality) if p)
        return title or None

    @classmethod
    def _photo_urls(cls, data: dict[str, Any]) -> list[str]:
        """Build CDN URLs from onsite.imageFiles, cover image first, deduped, order-preserving."""
        prop_id = data.get("propId")
        if prop_id is None:
            return []
        try:
            pid = int(prop_id)
        except (TypeError, ValueError):
            return []
        onsite = data.get("onsite") if isinstance(data.get("onsite"), dict) else {}
        files = onsite.get("imageFiles") if isinstance(onsite.get("imageFiles"), list) else []
        cover = onsite.get("coverImage")

        ordered: list[str] = []
        if isinstance(cover, str) and cover:
            ordered.append(cover)
        for f in files:
            if isinstance(f, str) and f:
                ordered.append(f)

        out: list[str] = []
        seen: set[str] = set()
        for fname in ordered:
            if fname in seen:
                continue
            seen.add(fname)
            out.append(cls._photo_url(pid, fname))
        return out

    @staticmethod
    def _photo_url(prop_id: int, filename: str) -> str:
        bucket = (prop_id // 1000) * 1000
        return f"{IMG_BASE}/properties/{IMG_WIDTH}/{bucket}/{prop_id}/slike/{filename}"

    @staticmethod
    def _external_id_from_url(url: str) -> str:
        m = _PROPID_FROM_URL_RE.search(url)
        if m:
            return m.group(1)
        # Fallbacks: a bare numeric tail or the last path segment.
        tail = url.split("?", 1)[0].rstrip("/").split("/")[-1]
        return tail


# ---- shared utilities ----


def _slugify(value: str | None) -> str | None:
    """Lowercase, ASCII-fold (č→c, ž→z, š→s, ...), hyphenate — matches cityexpert's slugs."""
    if not value:
        return None
    # Serbian Latin special letters that NFKD doesn't decompose cleanly.
    table = str.maketrans({"đ": "dj", "Đ": "dj", "ð": "dj"})
    text = value.translate(table)
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text or None


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
