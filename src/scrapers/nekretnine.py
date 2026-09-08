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
import os
import re
import secrets
import shutil
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import unquote, urljoin, urlparse

import httpx

from src.config import get_settings
from src.db.models import Platform
from src.scrapers.base import DEFAULT_USER_AGENT, ListingDetail, ListingPreview, Scraper
from src.scrapers.proxy_pool import NoProxyAvailableError, ProxyCandidate, ProxyPool

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
_PHOTO_ID_RE = re.compile(r"^\d+$")
_NEKRETNINE_PHOTO_RE = re.compile(
    r"^https://pic\.nekretnine\.rs/image/\d+/[^/?#]+\.jpg(?:[?#].*)?$"
)

# Detail-fetch statuses that are deterministic for a given URL — retrying just wastes the backoff
# budget and burns the IP's reputation with the anti-bot layer. 403 = block (the 13.07 incident),
# 404/410 = gone, 401 = auth wall. Everything else (5xx, 429, transport errors) is transient.
_NON_RETRYABLE_DETAIL_STATUSES = frozenset({401, 403, 404, 410})


class NekretnineScraper(Scraper):
    platform: ClassVar[Platform] = Platform.NEKRETNINE
    use_scraper_proxy: ClassVar[bool] = True

    # Resilience knobs (overridable in tests to keep them fast).
    fetch_attempts: ClassVar[int] = 3  # per-page GET attempts before declaring the page failed
    retry_backoff_sec: ClassVar[float] = 2.0  # sleep between per-page retries
    page_delay_sec: ClassVar[float] = 0.4  # pacing between pages on a long walk (avoids 429s)
    max_consecutive_failures: ClassVar[int] = 3  # abort the walk after this many in a row
    pw_timeout_ms: ClassVar[int] = 60000  # per-page cap on the D6 browser fallback
    # DataDome can initially serve a 403 challenge to Chromium and set its clearance cookie only
    # after the page has run briefly.  Keep this bounded: a few paced reloads are enough to let a
    # legitimate browser session settle, but do not turn a persistent upstream block into a long
    # hanging scrape.
    pw_challenge_reloads: ClassVar[int] = 3
    pw_challenge_wait_ms: ClassVar[int] = 4000

    # The site hard-caps every search at 80 pages × 25 = 2000 results (page 81+ returns HTTP 404)
    # even when totalAds is ~7700. A single unfiltered walk therefore covers only ~25% of the
    # market. For full walks we segment the search into price bands (?prezzoMinimo/?prezzoMassimo —
    # IndomioTech's Italian param names), each safely under the cap, and merge with dedup.
    page_cap: ClassVar[int] = 80
    results_cap: ClassVar[int] = 2000
    # (lo, hi) EUR bands; hi=None = open-ended. Shared boundaries are safe: inclusive overlap is
    # deduped by external_id, exclusive boundaries lose nothing.
    price_bands: ClassVar[tuple[tuple[int, int | None], ...]] = (
        (0, 300), (300, 400), (400, 500), (500, 600), (600, 700), (700, 800), (800, 900),
        (900, 1100), (1100, 1400), (1400, 2000), (2000, 3000), (3000, None),
    )

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.list_url = get_settings().nekretnine_list_url
        # Pages we couldn't fetch and skipped past — surfaced via ScrapeResult so a partial
        # under-run doesn't masquerade as a healthy walk.
        self._skipped_pages = 0
        # totalAds the site reports for the unfiltered search — lets the nightly summary show
        # coverage (previewed/site_total) and alert when we collect far less than what exists.
        self._site_total: int | None = None
        self._last_status: int | None = None  # HTTP status of the most recent _fetch_page
        # D6 Playwright fallback — started lazily on the FIRST DataDome 403 and reused for the rest
        # of the run (a browser per detail would cost seconds each); aclose() stops it. Headed by
        # default (anti-bot denies headless-shell); prod units already run under `xvfb-run -a`.
        self._pw: Any = None
        self._pw_context: Any = None
        self._pw_user_data_dir: Path | None = None
        self._pw_headless = os.environ.get("NEKRETNINE_HEADLESS", "false").lower() == "true"
        self._proxy_pool = ProxyPool.from_settings(get_settings())
        self._http_proxy_slot = (
            self._proxy_pool.candidates[0].slot if self._proxy_pool.candidates else None
        )
        self._failed_proxy_slots: set[str] = set()
        self._active_proxy_slot: str | None = None
        self._proxy_metrics: dict[str, int] = {
            "proxy_attempts": 0,
            "proxy_failovers": 0,
            "proxy_challenges": 0,
            "proxy_errors": 0,
            "browser_proxy_errors": 0,
            "http_attempts": 0, "http_errors": 0, "http_recovered_errors": 0,
            "http_residual_errors": 0, "browser_attempts": 0, "browser_errors": 0,
            "browser_recovered_errors": 0,
        }

    @property
    def proxy_metrics(self) -> dict[str, int]:
        """Sanitized proxy evidence for shadow reports; never expose URLs or credentials."""
        return {
            **self._proxy_metrics,
            "failed_proxy_slots": len(self._failed_proxy_slots),
        }

    def _select_proxy(self) -> ProxyCandidate | None:
        if not self._proxy_pool.candidates:
            self._active_proxy_slot = None
            return None
        try:
            candidate = self._proxy_pool.select(failed_slots=self._failed_proxy_slots)
        except NoProxyAvailableError:
            raise
        if self._active_proxy_slot != candidate.slot:
            if self._active_proxy_slot is not None:
                self._proxy_metrics["proxy_failovers"] += 1
            self._active_proxy_slot = candidate.slot
        return candidate

    def _mark_proxy_failure(self, *, challenge: bool = True) -> None:
        slot = self._active_proxy_slot
        if slot is None or slot in self._failed_proxy_slots:
            return
        self._failed_proxy_slots.add(slot)
        if challenge:
            self._proxy_metrics["proxy_challenges"] += 1

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

        # Pass 1 — unfiltered walk (newest-first). For incremental scrapes this is all we need;
        # it is also the only pass that can see listings with NO price (excluded by price filters).
        await self._walk(
            self.list_url, max_pages, known_ids, early_stop_full_known_pages,
            seen, previews, is_base=True,
        )

        # Pass 2 — price-band segmentation, only for full walks that the cap truncated.
        wants_full = max_pages > self.page_cap
        if wants_full and self._site_total is not None and self._site_total > len(previews):
            logger.info(
                "[nekretnine] unfiltered walk capped at %d of %d site listings — walking %d price bands",
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
                        "[nekretnine] band %s-%s holds %d listings > cap %d — band too wide, "
                        "narrow price_bands to restore full coverage",
                        lo, hi if hi is not None else "∞", band_total, self.results_cap,
                    )
                logger.info(
                    "[nekretnine] band %s-%s: +%d new (band_total=%s, cumulative=%d)",
                    lo, hi if hi is not None else "∞", len(previews) - before, band_total, len(previews),
                )
            logger.info(
                "[nekretnine] segmented walk done: %d collected vs site_total=%s",
                len(previews), self._site_total,
            )
        self.finish_crawl_evidence(len(previews))
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

        Returns the totalAds the site reports for THIS search (None if never seen).
        """
        consecutive_full_known = 0  # pages where every entry was already in known_ids
        consecutive_failures = 0  # pages we couldn't fetch at all
        site_max_pages: int | None = None
        search_total: int | None = None
        # Never request past the site's hard pagination cap — pages beyond it 404 or re-serve.
        limit = min(max_pages, self.page_cap)

        search_page_ids = set()
        page = 1
        while page <= limit:
            self.record_page_attempt(observed=len(previews))
            html = await self._fetch_page(self._page_url(base_url, page))
            if html is None:
                if getattr(self, "_last_failure_reason", None) is not None:
                    self.record_crawl_failure(self._last_failure_reason)
                    break
                if self._last_status == 404:
                    # Past the site's pagination cap — semantic end of this search, not a failure.
                    logger.info("[nekretnine] page %d → 404 (pagination cap), ending walk", page)
                    break
                # Transient fetch failure: retry a few pages before giving up. A single 429/timeout
                # must NOT abort the whole walk (the old `except: break` silently truncated the
                # active set and is what let nekretnine drop to zero unnoticed). But we DO count the
                # skip so a partial under-run is surfaced rather than silently passing for healthy.
                consecutive_failures += 1
                self._skipped_pages += 1
                self.record_crawl_failure("page_fetch_failed")
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
                self.record_crawl_failure("page_payload_missing")
                # 200 OK but no listing payload — almost certainly a site-structure change.
                # Log loudly: this is exactly the silent-failure mode we want surfaced.
                logger.error(
                    "[nekretnine] page %d: __NEXT_DATA__ list payload missing — site structure changed?",
                    page,
                )
                break
            ta = data.get("totalAds")
            if type(ta) is int and ta >= 0:
                if search_total is None:
                    search_total = ta
                if is_base:
                    self.record_source_total(ta)
            if site_max_pages is None:
                mp = data.get("maxPages")
                if isinstance(mp, int) and mp > 0:
                    site_max_pages = mp
            cp = data.get("currentPage")
            if type(cp) is not int:
                self.record_crawl_failure("missing_page_number")
            elif cp != page:
                self.record_crawl_failure("pagination_mismatch")
                # The site ignored ?pag= and re-served another page (the original migration bug
                # class). Without this guard a band walk would happily re-read page 1 forever.
                logger.error(
                    "[nekretnine] requested page %d but got currentPage=%s — pagination broken, stopping walk",
                    page, cp,
                )
                break

            page_previews = self._previews_from_payload(data)
            page_ids = tuple(p.external_id for p in page_previews)
            if page_ids and page_ids in search_page_ids:
                self.record_crawl_failure("repeated_page")
                break
            search_page_ids.add(page_ids)
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
                if search_total != 0:
                    self.record_crawl_failure("unexpected_empty_page")
                break  # structurally empty page = end of pagination
            # NB: a page whose listings are ALL already in `seen` is NOT the end — price-band
            # walks legitimately overlap the unfiltered pass (newest listings appear in both), so
            # we keep walking; the site's maxPages/currentPage signals bound the walk instead.
            if known_ids and unknown_this_page == 0 and new_this_page > 0:
                consecutive_full_known += 1
                if consecutive_full_known >= early_stop_full_known_pages:
                    self._stopped_known = True
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
        return search_total

    async def _prepare_http_route(self):
        candidate = self._select_proxy()
        slot = candidate.slot if candidate is not None else None
        if slot != self._http_proxy_slot:
            await self._stop_browser()
            if self._owned_client:
                await self._client.aclose()
                self._client = self._make_client(proxy_override=candidate.materialize(
                    session_token=secrets.token_hex(4)) if candidate is not None else None)
            self._http_proxy_slot = slot

    def _record_operation(self, kind):
        self._proxy_metrics[f"{kind}_attempts"] += 1
        if self._active_proxy_slot is not None:
            self._proxy_metrics["proxy_attempts"] += 1
            key = f"{kind}_attempts_{self._active_proxy_slot}"
            self._proxy_metrics[key] = self._proxy_metrics.get(key, 0) + 1

    async def _request_html(self, url: str, *, detail: bool) -> str:
        """One bounded HTTP/browser policy; unusable proxies fail over without direct fallback."""
        last_error = None
        raw_errors = 0
        attempts = max(self.fetch_attempts, len(self._proxy_pool.candidates))
        headers = {"Referer": self.list_url, "Sec-Fetch-Site": "same-origin"} if detail else None
        for attempt in range(attempts):
            status = None
            try:
                await self._prepare_http_route()
                self._record_operation("http")
                response = await self._client.get(url, headers=headers)
                status = response.status_code
                self._last_status = status
                challenge = status == 403 or (
                    status == 200 and not self._has_browser_payload(url, response.text)
                )
                if challenge:
                    raw_errors += 1
                    self._proxy_metrics["http_errors"] += 1
                    if status == 403 and get_settings().nekretnine_playwright_fallback:
                        html = await self._playwright_html(url)
                        if html and self._has_browser_payload(url, html):
                            self._proxy_metrics["http_recovered_errors"] += raw_errors
                            self._last_status = 200
                            return html
                    response.raise_for_status()
                    raise ValueError("listing payload missing after bounded challenge fallback")
                response.raise_for_status()
                self._proxy_metrics["http_recovered_errors"] += raw_errors
                return response.text
            except (httpx.HTTPError, ValueError, NoProxyAvailableError) as exc:
                last_error = exc
                status = getattr(getattr(exc, "response", None), "status_code", status)
                if status in {401, 404, 410}:
                    raise
                if not (status == 403 or isinstance(exc, ValueError)):
                    raw_errors += 1
                    self._proxy_metrics["http_errors"] += 1
                if isinstance(exc, httpx.RequestError) or status in {402, 407}:
                    self._mark_proxy_failure(challenge=False)
                if status == 403 or isinstance(exc, (ValueError, NoProxyAvailableError)):
                    break
                if self._proxy_pool.candidates and all(
                    c.slot in self._failed_proxy_slots for c in self._proxy_pool.candidates
                ):
                    break
                if attempt + 1 < attempts and self.retry_backoff_sec > 0:
                    await asyncio.sleep(self.retry_backoff_sec * (attempt + 1))
        self._proxy_metrics["http_residual_errors"] += 1
        self._proxy_metrics["proxy_errors"] += 1
        if last_error is not None:
            raise last_error
        raise RuntimeError("source request exhausted")

    async def fetch_detail(self, url: str) -> ListingDetail:
        html = await self._request_html(url, detail=True)
        return self.parse_detail(url, html)

    async def _start_browser(self) -> None:
        """Launch the stealth Chromium used by the DataDome fallback (mirrors halooglasi's setup).

        Headed by default: an anti-bot layer routinely denies the headless-shell binary. The prod
        units already wrap the CLI in `xvfb-run -a`, which is what makes headed work on the VPS.
        Each browser session gets a fresh profile and proxy session. This avoids carrying a
        DataDome block from one residential IP to the next while still reusing the browser for the
        current session's pages.
        """
        await self.acquire_resource()
        from playwright.async_api import async_playwright
        from playwright_stealth import Stealth

        settings = get_settings()
        ua = settings.scraper_user_agent or DEFAULT_USER_AGENT
        profile_root = settings.data_dir / "playwright" / "nekretnine"
        profile_root.mkdir(parents=True, exist_ok=True)
        user_data_dir = Path(tempfile.mkdtemp(prefix="session-", dir=profile_root))
        self._pw_user_data_dir = user_data_dir

        # Playwright does not accept an httpx-style proxy URL with embedded credentials as its
        # `server` value. Split the configured URL so the browser fallback uses the same egress as
        # the httpx client instead of silently falling back to the blocked VPS IP.
        browser_proxy: dict[str, str] | None = None
        selected_proxy = self._select_proxy()
        proxy_url = (
            selected_proxy.materialize(session_token=secrets.token_hex(4))
            if selected_proxy is not None
            else ""
        )
        if proxy_url:
            parsed_proxy = urlparse(proxy_url)
            if parsed_proxy.scheme and parsed_proxy.hostname:
                server = f"{parsed_proxy.scheme}://{parsed_proxy.hostname}"
                if parsed_proxy.port:
                    server += f":{parsed_proxy.port}"
                browser_proxy = {"server": server}
                if parsed_proxy.username is not None:
                    browser_proxy["username"] = unquote(parsed_proxy.username)
                if parsed_proxy.password is not None:
                    browser_proxy["password"] = unquote(parsed_proxy.password)

        self._pw = await async_playwright().start()
        try:
            stealth = Stealth(
                navigator_user_agent_override=ua,
                navigator_languages_override=("sr-RS", "sr", "en-US", "en"),
                init_scripts_only=False,
            )
            stealth.hook_playwright_context(self._pw)
            self._pw_context = await self._pw.chromium.launch_persistent_context(
                user_data_dir=str(user_data_dir),
                headless=self._pw_headless,
                user_agent=ua,
                proxy=browser_proxy,
                viewport={"width": 1366, "height": 900},
                locale="sr-RS",
                timezone_id="Europe/Belgrade",
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-features=IsolateOrigins,site-per-process",
                ],
            )
            # The parser needs the document and its scripts, not the visual page.  Loading every
            # image/font/stylesheet while using a metered residential proxy can consume more
            # traffic than the HTML itself and was the main reason the first full refresh burned
            # through the 2 GB allowance.  Keep scripts/XHR enabled so DataDome can settle, but
            # abort heavyweight visual subresources before they cross the proxy.
            await self._pw_context.route("**/*", self._route_browser_request)
        except BaseException:
            # A failed launch must not leak the playwright driver process.
            await self._pw.stop()
            self._pw = None
            raise

    @staticmethod
    async def _route_browser_request(route: Any) -> None:
        """Keep browser fallback HTML-only while preserving anti-bot scripts and JSON payloads."""
        if route.request.resource_type in {"image", "media", "font", "stylesheet"}:
            await route.abort()
            return
        await route.continue_()

    async def _stop_browser(self) -> None:
        """Stop the fallback browser and remove its short-lived profile."""
        if self._pw_context is not None:
            with suppress(Exception):
                await self._pw_context.close()
            self._pw_context = None
        if self._pw is not None:
            with suppress(Exception):
                await self._pw.stop()
            self._pw = None
        if self._pw_user_data_dir is not None:
            with suppress(Exception):
                shutil.rmtree(self._pw_user_data_dir)
            self._pw_user_data_dir = None

    async def _playwright_html(self, url: str) -> str | None:
        """Refetch one detail page through a real browser. Returns HTML, or None if that failed too.

        This is the D6 escalation for DataDome: httpx is 403'd on detail pages by TLS/JS
        fingerprint, but a stealth-patched headed Chromium gets 200 with the same `__NEXT_DATA__`
        blob, so `parse_detail` is reused verbatim. Best-effort by contract — any failure returns
        None and the caller re-raises the original 403 (never a false "listing is gone").
        """
        session_limit = (
            max(1, len(self._proxy_pool.candidates)) if self._proxy_pool.candidates else 3
        )
        raw_errors = 0
        for session_attempt in range(session_limit):
            try:
                if self._pw_context is None:
                    await self._start_browser()
                page = await self._pw_context.new_page()
                try:
                    self._record_operation("browser")
                    resp = await page.goto(
                        url, wait_until="domcontentloaded", timeout=self.pw_timeout_ms
                    )
                    for attempt in range(self.pw_challenge_reloads + 1):
                        status = resp.status if resp is not None else None
                        html = await page.content()
                        if status in {404, 410}:
                            raise httpx.HTTPStatusError("source listing removed",
                                request=httpx.Request("GET", url), response=httpx.Response(status))
                        if (status is None or status < 400) and self._has_browser_payload(url, html):
                            self._proxy_metrics["browser_recovered_errors"] += raw_errors
                            return html
                        raw_errors += 1
                        self._proxy_metrics["browser_errors"] += 1

                        if attempt == self.pw_challenge_reloads:
                            logger.warning(
                                "[nekretnine] playwright fallback %s → HTTP %s; no detail payload "
                                "after %d challenge reloads",
                                url, status, self.pw_challenge_reloads,
                            )
                            self._mark_proxy_failure()
                            break

                        logger.info(
                            "[nekretnine] playwright fallback %s → HTTP %s/no payload; "
                            "waiting %dms before challenge reload %d/%d",
                            url, status, self.pw_challenge_wait_ms, attempt + 1,
                            self.pw_challenge_reloads,
                        )
                        await page.wait_for_timeout(self.pw_challenge_wait_ms)
                        self._record_operation("browser")
                        resp = await page.reload(
                            wait_until="domcontentloaded", timeout=self.pw_timeout_ms
                        )
                finally:
                    await page.close()
            except httpx.HTTPStatusError:
                raise
            except Exception as e:  # fallback is best-effort, with sanitized failure evidence
                raw_errors += 1
                self._proxy_metrics["browser_errors"] += 1
                self._mark_proxy_failure(challenge=False)
                logger.warning(
                    "[nekretnine] playwright fallback failed: %s",
                    type(e).__name__,
                )

            if session_attempt < session_limit - 1:
                logger.info(
                    "[nekretnine] rotating browser proxy session after challenge (%d/2)",
                    session_attempt + 1,
                )
                await self._stop_browser()
            else:
                # Do not leave the last failed browser context alive.  A subsequent detail must
                # be able to start a fresh context and select the next bounded pool slot instead
                # of retrying the same exhausted ISP/rotating session forever.
                await self._stop_browser()
                self._proxy_metrics["browser_proxy_errors"] += 1
                return None
        return None

    async def aclose(self) -> None:
        """Close the httpx client (base) AND the fallback browser, if one was ever started."""
        try:
            await self._stop_browser()
        finally:
            await super().aclose()

    async def _fetch_page(self, url: str) -> str | None:
        self._last_status = None
        self._last_failure_reason = None
        try:
            return await self._request_html(url, detail=False)
        except Exception as exc:
            if isinstance(exc, NoProxyAvailableError):
                self._last_failure_reason = "proxy_unavailable"
            elif isinstance(exc, ValueError):
                self._last_failure_reason = "page_payload_missing"
            self._last_status = getattr(
                getattr(exc, "response", None), "status_code", self._last_status
            )
            logger.warning("[nekretnine] list request failed: %s", type(exc).__name__)
            return None

    def _band_url(self, lo: int, hi: int | None) -> str:
        sep = "&" if "?" in self.list_url else "?"
        params = f"prezzoMinimo={lo}"
        if hi is not None:
            params += f"&prezzoMassimo={hi}"
        return f"{self.list_url}{sep}{params}"

    @staticmethod
    def _page_url(base_url: str, page: int) -> str:
        if page <= 1:
            return base_url
        sep = "&" if "?" in base_url else "?"
        return f"{base_url}{sep}pag={page}"

    # ---- Pure parsers (testable on fixtures) ----

    @classmethod
    def parse_list(cls, html: str) -> list[ListingPreview]:
        """Extract preview rows from a list page's __NEXT_DATA__ blob."""
        data = cls._list_payload(html)
        if data is None:
            return []
        return cls._previews_from_payload(data)

    @classmethod
    def fingerprint(cls, list_html: str) -> dict[str, Any]:
        """Structure-only fingerprint of the __NEXT_DATA__ results payload (keys, not values)."""
        data = cls._list_payload(list_html)
        if data is None:
            return {}
        results = data.get("results")
        if not isinstance(results, list) or not results or not isinstance(results[0], dict):
            return {}
        return {
            "source": "__NEXT_DATA__",
            "result_keys": sorted(results[0].keys()),
            "n": len(results),
        }

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
    def _has_browser_payload(cls, url: str, html: str) -> bool:
        """Accept a genuine detail or list payload, never a successful-looking WAF page."""
        if not _OGLAS_ID_RE.search(url):
            return cls._list_payload(html) is not None
        nxt = cls._extract_next_data(html)
        if not isinstance(nxt, dict):
            return False
        try:
            real_estate = nxt["props"]["pageProps"]["detailData"]["realEstate"]
            expected_id = cls._external_id_from_url(url)
            return (isinstance(real_estate, dict) and bool(real_estate)
                    and str(real_estate.get("id", "")) == expected_id)
        except (KeyError, TypeError):
            return False

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
            # Canonical numeric URL — slug-independent. The platform periodically rewrites the
            # slug part of seo.url (/oglasi/<id>/<slug>/), so a stored slug-URL 404s while the
            # listing is still live. The numeric form /oglasi/<id>/ is immune. Keep the slug
            # anchor/title only as the human-readable title.
            url = f"{BASE_URL}/oglasi/{external_id}/"
            title = real_estate.get("title") or seo.get("anchor")
            seen.add(external_id)
            snapshot = NekretnineScraper._detail_from_real_estate(
                url, external_id, real_estate
            )
            if snapshot.title is None and isinstance(title, str):
                snapshot.title = title
            previews.append(
                ListingPreview(
                    platform=Platform.NEKRETNINE,
                    external_id=external_id,
                    url=urljoin(BASE_URL, url),
                    title=title if isinstance(title, str) else None,
                    snapshot=snapshot,
                )
            )
        return previews

    @staticmethod
    def is_snapshot_complete(snapshot: ListingDetail | None) -> bool:
        """Return whether a list snapshot contains every bot-visible field needed for active use."""
        if snapshot is None:
            return False
        required_text = (
            snapshot.external_id,
            snapshot.url,
            snapshot.district,
            snapshot.address_raw,
            snapshot.title,
        )
        if any(not isinstance(value, str) or not value.strip() for value in required_text):
            return False
        if not snapshot.url.startswith(f"{BASE_URL}/oglasi/"):
            return False
        if any(
            value is None
            for value in (
                snapshot.price_eur,
                snapshot.area_m2,
                snapshot.rooms,
                snapshot.lat,
                snapshot.lon,
            )
        ):
            return False
        return bool(
            snapshot.photo_urls
            and all(NekretnineScraper._is_valid_photo_url(url) for url in snapshot.photo_urls)
        )

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
    def _is_valid_photo_url(url: object) -> bool:
        return isinstance(url, str) and bool(_NEKRETNINE_PHOTO_RE.fullmatch(url))

    @staticmethod
    def _photo_urls(prop: dict[str, Any]) -> list[str]:
        """Collect ordered unique XXL CDN URLs from IDs, with a strict host allowlist."""
        out: list[str] = []
        seen: set[str] = set()
        multimedia = prop.get("multimedia") if isinstance(prop.get("multimedia"), dict) else {}
        photos = multimedia.get("photos") if isinstance(multimedia.get("photos"), list) else []
        for ph in photos:
            if not isinstance(ph, dict):
                continue
            photo_id = ph.get("id")
            if photo_id is not None and _PHOTO_ID_RE.fullmatch(str(photo_id).strip()):
                derived = f"https://pic.nekretnine.rs/image/{str(photo_id).strip()}/xxl.jpg"
                if derived not in seen:
                    seen.add(derived)
                    out.append(derived)
                continue
            urls = ph.get("urls") if isinstance(ph.get("urls"), dict) else {}
            best = urls.get("large") or urls.get("medium") or urls.get("small")
            if NekretnineScraper._is_valid_photo_url(best) and best not in seen:
                seen.add(best)
                out.append(best)
        if not out:
            primary = prop.get("photo") if isinstance(prop.get("photo"), dict) else {}
            urls = primary.get("urls") if isinstance(primary.get("urls"), dict) else {}
            best = urls.get("large") or urls.get("medium")
            if NekretnineScraper._is_valid_photo_url(best):
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
