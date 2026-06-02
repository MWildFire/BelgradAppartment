"""Scraper for halooglasi.com.

The site sits behind Cloudflare's "Just a moment…" challenge, so plain httpx is a 403. We use
Playwright with a persistent user-data dir so the cf_clearance cookie sticks across runs.

Halooglasi happens to embed all search-results data as a single JSON blob in the page —
`QuidditaEnvironment.serverListData = {... "Ads": [...]}`. Detail pages do the same with
`QuidditaEnvironment.CurrentClassified = {...}`. We extract both via balanced-JSON decoding,
so parsing itself is offline and unit-testable.

Image URLs come back as paths relative to `https://img.halooglasi.com`.
Coordinates live in `GeoLocationRPT: "<lat>,<lon>"`.
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

BASE_URL = "https://www.halooglasi.com"
IMG_BASE = "https://img.halooglasi.com"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_LIST_VAR = "QuidditaEnvironment.serverListData"
_DETAIL_VAR = "QuidditaEnvironment.CurrentClassified"

ROOMS_BY_TEXT = {
    "garsonjera": 1.0,
    "jednosoban": 1.0,
    "1.0": 1.0,
    "1": 1.0,
    "jednoiposoban": 1.5,
    "1.5": 1.5,
    "dvosoban": 2.0,
    "2.0": 2.0,
    "2": 2.0,
    "dvoiposoban": 2.5,
    "2.5": 2.5,
    "trosoban": 3.0,
    "3.0": 3.0,
    "3": 3.0,
    "troiposoban": 3.5,
    "3.5": 3.5,
    "cetvorosoban": 4.0,
    "četvorosoban": 4.0,
    "4.0": 4.0,
    "4": 4.0,
}


class HalooglasiScraper(Scraper):
    platform: ClassVar[Platform] = Platform.HALOOGLASI

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        settings = get_settings()
        self.list_url = settings.halooglasi_list_url
        self._user_data_dir = settings.data_dir / "playwright" / "halooglasi"
        self._user_data_dir.mkdir(parents=True, exist_ok=True)
        self._pw = None
        self._context = None
        # Cloudflare's "Just a moment…" challenge often denies the headless-shell binary.
        # Default to headed so it passes on macOS; on Linux VPS use xvfb-run or override via env.
        # Toggle with HALOOGLASI_HEADLESS=true in .env when you really need headless.
        import os
        self._headless = os.environ.get("HALOOGLASI_HEADLESS", "false").lower() == "true"

    async def __aenter__(self) -> HalooglasiScraper:
        from playwright.async_api import async_playwright
        from playwright_stealth import Stealth

        self._pw = await async_playwright().start()
        # Stealth patches: navigator.webdriver=undefined, real chrome.runtime, proper sec-ch-ua,
        # webgl vendor, plugin list, etc. — what Cloudflare actually checks. Hook the running
        # Playwright instance so every new page gets the patches applied at navigation time.
        stealth = Stealth(
            navigator_user_agent_override=USER_AGENT,
            navigator_languages_override=("sr-RS", "sr", "en-US", "en"),
            init_scripts_only=False,
        )
        stealth.hook_playwright_context(self._pw)

        self._context = await self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self._user_data_dir),
            headless=self._headless,
            user_agent=USER_AGENT,
            viewport={"width": 1366, "height": 900},
            locale="sr-RS",
            timezone_id="Europe/Belgrade",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
        )
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._context is not None:
            await self._context.close()
            self._context = None
        if self._pw is not None:
            await self._pw.stop()
            self._pw = None

    async def list_recent(
        self,
        max_pages: int = 5,
        *,
        known_ids: set[str] | None = None,
        early_stop_full_known_pages: int = 2,
    ) -> list[ListingPreview]:
        if self._context is None:
            raise RuntimeError("HalooglasiScraper must be used as async context manager")
        seen: set[str] = set()
        previews: list[ListingPreview] = []
        consecutive_failures = 0
        known_ids = known_ids or set()
        consecutive_full_known = 0

        for page_num in range(1, max_pages + 1):
            url = self.list_url if page_num == 1 else f"{self.list_url}?page={page_num}"

            # Single-page retry with browser restart if Chromium reports a network/resource error.
            html = ""
            for attempt in range(3):
                try:
                    html = await self._fetch_with_browser(url, ready_marker=_LIST_VAR)
                    break
                except Exception as e:
                    logger.warning(
                        "[halooglasi] page %d attempt %d failed: %s — recycling browser",
                        page_num, attempt + 1, type(e).__name__,
                    )
                    await self._recycle_browser()
                    await asyncio.sleep(3.0)

            if not html or _LIST_VAR not in html:
                consecutive_failures += 1
                logger.warning("[halooglasi] page %d: no data after retries (consec_fail=%d)",
                               page_num, consecutive_failures)
                if consecutive_failures >= 5:
                    logger.error("[halooglasi] giving up after 5 consecutive failures")
                    break
                continue
            consecutive_failures = 0

            page_previews = self.parse_list(html)
            added = 0
            unknown_this_page = 0
            for prev in page_previews:
                if prev.external_id in seen:
                    continue
                seen.add(prev.external_id)
                previews.append(prev)
                added += 1
                if prev.external_id not in known_ids:
                    unknown_this_page += 1
            if added == 0:
                # Empty page = end of pagination.
                break
            if known_ids and unknown_this_page == 0:
                consecutive_full_known += 1
                if consecutive_full_known >= early_stop_full_known_pages:
                    logger.info(
                        "[halooglasi] incremental early-stop at page %d", page_num,
                    )
                    break
            else:
                consecutive_full_known = 0
            await asyncio.sleep(1.5)
        return previews

    async def _recycle_browser(self) -> None:
        """Restart the persistent context — helps with Chromium ERR_NETWORK_IO_SUSPENDED."""
        if self._context is not None:
            try:
                await self._context.close()
            except Exception:
                pass
            self._context = None
        self._context = await self._pw.chromium.launch_persistent_context(  # type: ignore[union-attr]
            user_data_dir=str(self._user_data_dir),
            headless=self._headless,
            user_agent=USER_AGENT,
            viewport={"width": 1366, "height": 900},
            locale="sr-RS",
            timezone_id="Europe/Belgrade",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
        )

    async def fetch_detail(self, url: str) -> ListingDetail:
        if self._context is None:
            raise RuntimeError("HalooglasiScraper must be used as async context manager")
        # Same retry+recycle pattern as list pages — Chromium ERR_NETWORK_IO_SUSPENDED
        # can hit a single detail page and then propagate to the rest if we don't restart.
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                html = await self._fetch_with_browser(url, ready_marker=_DETAIL_VAR)
                return self.parse_detail(url, html)
            except Exception as e:
                last_error = e
                logger.warning(
                    "[halooglasi] detail %s attempt %d failed: %s",
                    url, attempt + 1, type(e).__name__,
                )
                await self._recycle_browser()
                await asyncio.sleep(2.0)
        if last_error is not None:
            raise last_error
        raise RuntimeError("unreachable")

    # ---- Browser helpers ----

    async def _fetch_with_browser(self, url: str, *, ready_marker: str) -> str:
        """Open `url`, wait until `ready_marker` appears in the page HTML, return content.

        Why content-polling instead of `wait_for_function`: halooglasi defines
        `QuidditaEnvironment.serverListData = {...}` with a bare `var` at the top of an inline
        script. JS `typeof` of a `var` in a different evaluate context isn't reliable across
        engines; HTML substring check is. Also: Cloudflare-challenge pages don't contain the
        marker, so the same wait elegantly handles "challenge still in progress".
        """
        assert self._context is not None
        page = await self._context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            # Poll page content for the marker — up to 30s total, ~600ms steps.
            html = ""
            for _ in range(50):
                html = await page.content()
                if ready_marker in html:
                    break
                if "Just a moment" in html or "challenge-platform" in html and ready_marker not in html:
                    # CF challenge still loading; give it more time.
                    await page.wait_for_timeout(800)
                    continue
                await page.wait_for_timeout(600)
            if ready_marker not in html:
                logger.warning("[halooglasi] %s marker '%s' not found after wait", url, ready_marker)
        finally:
            await page.close()
        return html

    # ---- Pure parsers ----

    @classmethod
    def parse_list(cls, html: str) -> list[ListingPreview]:
        data = _extract_json_var(html, _LIST_VAR)
        if not isinstance(data, dict):
            return []
        ads = data.get("Ads") or []
        previews: list[ListingPreview] = []
        for ad in ads:
            if not isinstance(ad, dict):
                continue
            external_id = str(ad.get("Id") or "").strip()
            rel = ad.get("RelativeUrl") or ""
            if not external_id or not isinstance(rel, str) or not rel:
                continue
            title = ad.get("Title") if isinstance(ad.get("Title"), str) else None
            previews.append(
                ListingPreview(
                    platform=Platform.HALOOGLASI,
                    external_id=external_id,
                    url=urljoin(BASE_URL, rel),
                    title=title,
                )
            )
        return previews

    @classmethod
    def parse_detail(cls, url: str, html: str) -> ListingDetail:
        ad = _extract_json_var(html, _DETAIL_VAR)
        if not isinstance(ad, dict):
            external_id = _external_id_from_url(url)
            return ListingDetail(
                platform=Platform.HALOOGLASI,
                external_id=external_id,
                url=url,
            )

        external_id = str(ad.get("Id") or _external_id_from_url(url))
        other: dict[str, Any] = ad.get("OtherFields") or {}

        price_eur = _safe_int(other.get("cena_d"))
        area_m2 = _safe_float(other.get("kvadratura_d"))
        rooms = _parse_rooms(other.get("broj_soba_s"))

        district = (
            other.get("mikrolokacija_s")
            or other.get("lokacija_s")
            or None
        )
        address_raw = other.get("ulica_t") or None
        lat, lon = _parse_geo(ad.get("GeoLocationRPT"))

        title = ad.get("Title") if isinstance(ad.get("Title"), str) else None
        description = (
            ad.get("Text") or ad.get("TextHtml") or ad.get("PrintText")
        )
        if isinstance(description, str):
            # collapse excessive whitespace
            description = re.sub(r"\s+", " ", description).strip()
        else:
            description = None

        photo_urls: list[str] = []
        for raw in ad.get("ImageURLs") or []:
            if not isinstance(raw, str):
                continue
            full = raw if raw.startswith("http") else urljoin(IMG_BASE, raw)
            if full not in photo_urls:
                photo_urls.append(full)

        return ListingDetail(
            platform=Platform.HALOOGLASI,
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
            raw_payload={
                "tip_nekretnine_s": other.get("tip_nekretnine_s"),
                "sprat_s": other.get("sprat_s"),
                "namestenost_s": other.get("namestenost_s"),
                "oglasivac_nekretnine_s": other.get("oglasivac_nekretnine_s"),
            },
        )


# --- helpers ---


def _extract_json_var(html: str, var_path: str) -> Any:
    """Find `<var_path> = {...}` in HTML and decode the JSON object value.

    Uses json.JSONDecoder.raw_decode for safe balanced parsing — robust to braces inside strings.
    """
    pattern = re.escape(var_path) + r"\s*=\s*"
    m = re.search(pattern, html)
    if not m:
        return None
    tail = html[m.end():]
    # Strip leading whitespace before the JSON literal.
    tail = tail.lstrip()
    if not tail.startswith("{"):
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(tail)
        return obj
    except json.JSONDecodeError:
        return None


def _external_id_from_url(url: str) -> str:
    # /nekretnine/izdavanje-stanova/<slug>/<id>?<query>
    path = url.split("?", 1)[0]
    return path.rstrip("/").split("/")[-1]


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


def _parse_rooms(value: object) -> float | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in ROOMS_BY_TEXT:
        return ROOMS_BY_TEXT[text]
    # last resort: parse leading number
    m = re.match(r"(\d+(?:[.,]\d+)?)", text)
    if m:
        return _safe_float(m.group(1))
    return None


def _parse_geo(value: object) -> tuple[float | None, float | None]:
    if not isinstance(value, str):
        return None, None
    parts = [p.strip() for p in value.split(",")]
    if len(parts) != 2:
        return None, None
    return _safe_float(parts[0]), _safe_float(parts[1])
