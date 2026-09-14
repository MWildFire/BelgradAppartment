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
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from src.config import get_settings
from src.db.models import Platform
from src.scrapers.base import ListingDetail, ListingPreview, Scraper

logger = logging.getLogger(__name__)

BASE_URL = "https://www.halooglasi.com"
# Public Halooglasi print host.  It serves the same server-rendered Quiddita payloads as `www`
# without Cloudflare's interactive gate.  It is used only as a read fallback; all stored/public
# listing URLs stay on the canonical www host.
SMSPRINT_BASE_URL = "https://smsprint.halooglasi.com"
IMG_BASE = "https://img.halooglasi.com"

_LIST_VAR = "QuidditaEnvironment.serverListData"
_DETAIL_VAR = "QuidditaEnvironment.CurrentClassified"

# Cloudflare interstitial fingerprints. 'Sačekajte trenutak' is the Serbian 'Just a moment' — the
# context runs at locale sr-RS, so that is the title the site actually serves us; matching only the
# English string would misread every real challenge as a soft error page.
_CHALLENGE_MARKERS = (
    "Just a moment",
    "Sačekajte trenutak",
    "challenge-platform",
    "cf-turnstile-response",
)

# One navigation's worth of polling (~15s), then reload. Worst case 5 navigations ~= 75s + loads,
# which is under the old doomed budget of 3 attempts at ~43s each. Once clearance lands it is
# reused by every later page in the context — measured: subsequent details return 200 in ~2s.
_MARKER_POLL_STEPS = 25
_MARKER_POLL_STEP_MS = 600
_CHALLENGE_MAX_RELOADS = 4


def _is_challenge(html: str) -> bool:
    """True if `html` is a Cloudflare interstitial rather than a real (if unparseable) page."""
    return any(marker in html for marker in _CHALLENGE_MARKERS)


class _MarkerMissing(RuntimeError):
    """Page loaded but carries no data marker: challenge outlasted the reloads, or a soft error.

    Distinct from a browser fault because the retry policy differs — see `fetch_detail`.
    """


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
        self._pw = None
        self._context = None
        self._skipped_pages = 0
        self._site_total: int | None = None  # serverListData.TotalCount — coverage denominator
        # Raw page-1 HTML from the most recent list_recent walk. Stashed so the A2 smoke probe can
        # fingerprint the list structure without a second (expensive Playwright) fetch — halooglasi
        # has no cheap httpx `_fetch_page` like the other scrapers.
        self._last_list_html: str | None = None
        # Cloudflare's "Just a moment…" challenge often denies the headless-shell binary.
        # Default to headed so it passes on macOS; on Linux VPS use xvfb-run or override via env.
        # Toggle with HALOOGLASI_HEADLESS=true in .env when you really need headless.
        import os
        self._headless = os.environ.get("HALOOGLASI_HEADLESS", "false").lower() == "true"

    async def __aenter__(self) -> HalooglasiScraper:
        await super().__aenter__()
        try:
            return await self._start_browser_context()
        except BaseException:
            await self.aclose()
            raise

    async def _start_browser_context(self) -> HalooglasiScraper:
        self._user_data_dir.mkdir(parents=True, exist_ok=True)
        from playwright.async_api import async_playwright
        from playwright_stealth import Stealth

        self._pw = await async_playwright().start()
        try:
            # Stealth patches: navigator.webdriver=undefined, real chrome.runtime, proper sec-ch-ua,
            # webgl vendor, plugin list, etc. — what Cloudflare actually checks. Hook the running
            # Playwright instance so every new page gets the patches applied at navigation time.
            # Do NOT override the user agent here.  The VPS Playwright Chromium is updated with
            # the dependency (currently Chromium 148), while the old hard-coded macOS Chrome 126
            # string left a version/platform mismatch across UA, Client Hints and JS APIs — a
            # strong Cloudflare automation fingerprint.  Native headed Chromium gives coherent
            # network and JavaScript identity; stealth only supplies the missing automation fixes.
            stealth = Stealth(
                navigator_languages_override=("sr-RS", "sr", "en-US", "en"),
                navigator_platform_override="Linux x86_64",
                init_scripts_only=False,
            )
            stealth.hook_playwright_context(self._pw)

            self._context = await self._pw.chromium.launch_persistent_context(
                user_data_dir=str(self._user_data_dir),
                headless=self._headless,
                viewport={"width": 1366, "height": 900},
                locale="sr-RS",
                timezone_id="Europe/Belgrade",
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-features=IsolateOrigins,site-per-process",
                ],
            )
        except BaseException:
            # A failed launch must not leak the playwright driver process — and the caller
            # (e.g. nightly _run_all) treats this platform as failed, not the whole run.
            await self._pw.stop()
            self._pw = None
            await super().aclose()
            raise
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        try:
            if self._context is not None:
                await self._context.close()
                self._context = None
            if self._pw is not None:
                await self._pw.stop()
                self._pw = None
        finally:
            await super().aclose()

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
        self.reset_crawl_evidence()
        self._skipped_pages = 0
        self._site_total = None

        for page_num in range(1, max_pages + 1):
            self.record_page_attempt(observed=len(previews))
            url = self.list_url if page_num == 1 else f"{self.list_url}?page={page_num}"

            # Single-page retry with browser restart on Chromium network errors. A returned page
            # WITHOUT the data marker (Cloudflare challenge that outlasted the poll, soft error
            # page) must also consume retries — previously it broke out after attempt 1 and the
            # page was skipped with two unused retries left.
            html = ""
            for attempt in range(3):
                # The public smsprint host returns exactly the server-rendered payload we parse
                # below and avoids burning a minute on Cloudflare when the primary host decides
                # to show an interactive challenge to the VPS IP.
                html = await self._fetch_via_smsprint(url, ready_marker=_LIST_VAR)
                if html:
                    break
                try:
                    html = await self._fetch_with_browser(url, ready_marker=_LIST_VAR)
                except Exception as e:
                    logger.warning(
                        "[halooglasi] page %d attempt %d failed: %s — recycling browser",
                        page_num, attempt + 1, type(e).__name__,
                    )
                    await self._recycle_browser()
                    await asyncio.sleep(3.0)
                    continue
                if html and _LIST_VAR in html:
                    break
                logger.warning(
                    "[halooglasi] page %d attempt %d: marker missing (challenge/soft error) — retrying",
                    page_num, attempt + 1,
                )
                await asyncio.sleep(5.0)

            if not html or _LIST_VAR not in html:
                consecutive_failures += 1
                self._skipped_pages += 1
                self.record_crawl_failure("page_fetch_failed")
                logger.warning("[halooglasi] page %d: no data after retries (consec_fail=%d, skipped=%d)",
                               page_num, consecutive_failures, self._skipped_pages)
                if consecutive_failures >= 5:
                    logger.error("[halooglasi] giving up after 5 consecutive failures")
                    break
                continue
            consecutive_failures = 0
            if page_num == 1:
                self._last_list_html = html  # for the A2 smoke fingerprint (see __init__)

            data = _extract_json_var(html, _LIST_VAR)
            if not isinstance(data, dict) or not isinstance(data.get("Ads"), list):
                self.record_crawl_failure("page_payload_missing")
                break
            self.record_source_total(data.get("TotalCount"))
            if data.get("PageNumber") != page_num:
                self.record_crawl_failure("pagination_mismatch")
                break

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
                if self._site_total != len(previews):
                    self.record_crawl_failure("unexpected_empty_or_repeated_page")
                break
            if known_ids and unknown_this_page == 0:
                consecutive_full_known += 1
                if consecutive_full_known >= early_stop_full_known_pages:
                    self._stopped_known = True
                    logger.info(
                        "[halooglasi] incremental early-stop at page %d", page_num,
                    )
                    break
            else:
                consecutive_full_known = 0
            if type(data.get("TotalPages")) is int and page_num >= data["TotalPages"]:
                break
            await asyncio.sleep(1.5)
        self.finish_crawl_evidence(len(previews))
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
        mirror_html = await self._fetch_via_smsprint(url, ready_marker=_DETAIL_VAR)
        if mirror_html:
            return self.parse_detail(url, mirror_html)
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                html = await self._fetch_with_browser(url, ready_marker=_DETAIL_VAR)
                if _DETAIL_VAR not in html:
                    # Challenge/soft-error page: parse_detail would "succeed" with an all-None
                    # shell that gets inserted and frozen forever (existing rows are never
                    # detail-refreshed) — fail instead so the retry/error path handles it.
                    raise _MarkerMissing(
                        "detail data marker missing (challenge or soft error page)"
                    )
                return self.parse_detail(url, html)
            except _MarkerMissing as e:
                # Deliberately NO _recycle_browser() here. A challenged attempt leaves the
                # profile holding the cf_clearance Cloudflare just granted, and the next navigation
                # is what redeems it. Recycling discarded that every time, so every attempt started
                # cold and detail failed 9/9 in prod on 16.07 — while the list walk, which never
                # recycles, recovered on its 3rd attempt in the very same runs.
                last_error = e
                logger.warning("[halooglasi] detail %s attempt %d: %s", url, attempt + 1, e)
                await asyncio.sleep(3.0)
            except Exception as e:
                # A real Chromium fault (ERR_NETWORK_IO_SUSPENDED &c) can hit one detail page and
                # then propagate to the rest if we don't restart the browser.
                last_error = e
                logger.warning(
                    "[halooglasi] detail %s attempt %d failed: %s — recycling browser",
                    url, attempt + 1, type(e).__name__,
                )
                await self._recycle_browser()
                await asyncio.sleep(2.0)
        if last_error is not None:
            raise last_error
        raise RuntimeError("unreachable")

    # ---- Browser helpers ----

    @staticmethod
    def _smsprint_url(url: str) -> str | None:
        """Map a canonical Halooglasi URL to its public server-rendered print host."""
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.netloc not in {"halooglasi.com", "www.halooglasi.com"}:
            return None
        return urlunsplit(("https", "smsprint.halooglasi.com", parts.path, parts.query, ""))

    async def _fetch_via_smsprint(self, url: str, *, ready_marker: str) -> str:
        """Return a verified public print-host payload, or an empty string to use Chromium.

        This is deliberately a fallback rather than a second parser: both hosts expose the same
        Quiddita variables, and the caller still requires its expected marker before accepting
        anything.  Failed/changed mirror responses remain visible and fall back to the canonical
        browser route rather than silently yielding partial data.
        """
        mirror_url = self._smsprint_url(url)
        if mirror_url is None:
            return ""
        try:
            response = await self._client.get(mirror_url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.info("[halooglasi] smsprint unavailable for %s: %s", url, type(exc).__name__)
            return ""
        if ready_marker in response.text:
            logger.debug("[halooglasi] loaded %s via smsprint", url)
            return response.text
        if (
            ready_marker == _DETAIL_VAR
            and response.history
            and response.url.host == "smsprint.halooglasi.com"
            and response.url.path.rstrip("/") == "/nekretnine/izdavanje-stanova"
            and _LIST_VAR in response.text
        ):
            # A removed/unavailable print page redirects to the catalogue. Retrying the
            # challenged canonical host cannot validate this response. Keep the stored
            # listing unchanged and let the normal UNKNOWN/backoff policy record the gap;
            # a mirror redirect alone is never evidence for deactivation.
            raise _MarkerMissing("print detail redirected to catalogue; availability unknown")
        logger.warning("[halooglasi] smsprint response missing marker '%s' for %s", ready_marker, url)
        return ""

    async def _fetch_with_browser(self, url: str, *, ready_marker: str) -> str:
        """Open `url`, soak-and-reload until `ready_marker` shows up in the HTML, return content.

        Why content-polling instead of `wait_for_function`: halooglasi defines
        `QuidditaEnvironment.serverListData = {...}` with a bare `var` at the top of an inline
        script. JS `typeof` of a `var` in a different evaluate context isn't reliable across
        engines; HTML substring check is.

        Why RELOAD rather than only poll: Cloudflare grants clearance while the browser sits
        on the interstitial, but the interstitial never navigates to the real page by itself — only
        a fresh navigation cashes that clearance in. Measured on the VPS 16.07, fresh profile:

            goto detail, poll 150s, no reload → 403, still 'Sacekajte trenutak' (never resolves)
            goto detail, soak 45s, reload     → 403, still the challenge
                          ...reload again     → 200, CurrentClassified present

        Polling alone therefore watches a static interstitial until it times out — which is exactly
        what the detail path did. (The list walk accidentally sidestepped this: its retry loop
        re-`goto`s in the same context, so its 3rd attempt *is* the reload. That is why the list
        needed 2 doomed attempts on every single run.)
        """
        assert self._context is not None
        page = await self._context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            html = ""
            for nav in range(_CHALLENGE_MAX_RELOADS + 1):
                html = await self._poll_for_marker(page, ready_marker)
                if ready_marker in html:
                    return html
                if not _is_challenge(html):
                    # A real page that simply lacks the marker (removed ad, soft error): reloading
                    # it only burns a minute per attempt. Hand it back; the caller decides.
                    logger.warning(
                        "[halooglasi] %s marker '%s' missing on a non-challenge page",
                        url, ready_marker,
                    )
                    return html
                if nav < _CHALLENGE_MAX_RELOADS:
                    logger.info(
                        "[halooglasi] %s challenge held after soak — reload %d/%d",
                        url, nav + 1, _CHALLENGE_MAX_RELOADS,
                    )
                    await page.reload(wait_until="domcontentloaded", timeout=60000)
            logger.warning(
                "[halooglasi] %s still challenged after %d reloads", url, _CHALLENGE_MAX_RELOADS
            )
            return html
        finally:
            await page.close()

    async def _poll_for_marker(self, page: Any, ready_marker: str) -> str:
        """Poll one navigation's content for `ready_marker`; return the last HTML seen.

        Doubles as the challenge soak: Cloudflare needs these seconds of JS to grant clearance,
        which the *next* navigation redeems.
        """
        html = ""
        for _ in range(_MARKER_POLL_STEPS):
            html = await page.content()
            if ready_marker in html:
                return html
            await page.wait_for_timeout(_MARKER_POLL_STEP_MS)
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
    def fingerprint(cls, list_html: str) -> dict[str, Any]:
        """Structure-only fingerprint of serverListData (first ad's keys, not values)."""
        data = _extract_json_var(list_html, _LIST_VAR)
        if not isinstance(data, dict):
            return {}
        ads = data.get("Ads")
        if not isinstance(ads, list) or not ads or not isinstance(ads[0], dict):
            return {}
        return {
            "source": "serverListData",
            "ad_keys": sorted(ads[0].keys()),
            "n": len(ads),
        }

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

        # `mikrolokacija_s` is a neighbourhood (for example, "Dobanovačka");
        # `lokacija_s` is Halooglasi' parent municipality ("Opština Zemun").
        # Keep the latter so the public catalogue can consistently limit its district
        # labels to the ten supported urban districts. The micro-location remains in
        # the source HTML/title, but must not become the primary district field.
        district = (
            other.get("lokacija_s")
            or other.get("mikrolokacija_s")
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
