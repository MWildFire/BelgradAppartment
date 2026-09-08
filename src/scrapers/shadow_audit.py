"""Read-only Nekretnine snapshot-vs-detail audit.

The audit deliberately depends only on the scraper interface.  It does not open a database
session, run the orchestrator, perform a stale sweep, or know anything about Telegram delivery.
That makes the command safe to run before enabling ``active`` snapshot ingestion.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse
from uuid import uuid4

from src.scrapers.base import ListingDetail, ListingPreview
from src.scrapers.crawl_evidence import source_total_report
from src.scrapers.snapshot_parity import mismatch_fields


class ShadowAuditScraper(Protocol):
    async def list_recent(
        self,
        max_pages: int = 5,
        *,
        known_ids: set[str] | None = None,
        early_stop_full_known_pages: int = 2,
    ) -> list[ListingPreview]: ...

    async def fetch_detail(self, url: str) -> ListingDetail: ...


@dataclass(frozen=True, slots=True)
class ShadowAuditThresholds:
    min_coverage: float = 0.98
    max_fallback_rate: float | None = None


@dataclass(slots=True)
class ShadowAuditReport:
    max_pages: int
    compare_limit: int
    run_id: str = field(default_factory=lambda: uuid4().hex)
    source: str = "nekretnine"
    mode: str = "shadow"
    no_db_writes: bool = True
    no_telegram_sends: bool = True
    no_stale_sweep: bool = True
    previewed: int = 0
    site_total: int | None = None
    source_totals: dict[str, Any] = field(default_factory=dict)
    skipped_pages: int = 0
    coverage: float = 0.0
    sample_compared: int = 0
    complete_snapshots: int = 0
    fallback_count: int = 0
    fallback_succeeded: int = 0
    sample_segments: list[str] = field(default_factory=list)
    population_segments: int = 0
    crawl_complete: bool = False
    stop_reason: str = "unverified"
    mismatched_count: int = 0
    detail_errors: int = 0
    browser_proxy_errors: int = 0
    proxy_metrics: dict[str, int] = field(default_factory=dict)
    mismatches: list[dict[str, Any]] = field(default_factory=list)
    fallback_listing_ids: list[str] = field(default_factory=list)
    detail_error_listing_ids: list[str] = field(default_factory=list)
    detail_failures: list[dict[str, Any]] = field(default_factory=list)
    thresholds: dict[str, Any] = field(default_factory=dict)
    passed: bool = False
    failure_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return only sanitized evidence; no URLs, addresses, credentials, or raw payloads."""
        return asdict(self)


def _error_code(error: Exception) -> str:
    code = type(error).__name__
    return code if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", code) else "Exception"


def _snapshot_is_complete(scraper: ShadowAuditScraper, snapshot: ListingDetail | None) -> bool:
    checker = getattr(scraper, "is_snapshot_complete", None)
    if callable(checker):
        return bool(checker(snapshot))
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
    return bool(snapshot.photo_urls and all(_valid_photo_url(url) for url in snapshot.photo_urls))


def _valid_photo_url(url: object) -> bool:
    if not isinstance(url, str):
        return False
    parsed = urlparse(url)
    path_parts = [part for part in parsed.path.split("/") if part]
    return (
        parsed.scheme == "https"
        and parsed.netloc == "pic.nekretnine.rs"
        and len(path_parts) == 3
        and path_parts[0] == "image"
        and path_parts[1].isdigit()
        and path_parts[2] == "xxl.jpg"
    )


def _mismatch_fields(snapshot: ListingDetail, detail: ListingDetail) -> list[str]:
    return mismatch_fields(snapshot, detail)


def _proxy_error_count(scraper: ShadowAuditScraper) -> int:
    metrics = getattr(scraper, "proxy_metrics", None)
    if not isinstance(metrics, dict):
        return 0
    return max(
        int(metrics.get(name, 0) or 0)
        for name in ("browser_proxy_errors", "proxy_errors", "failover_errors")
    )


def _proxy_metrics(scraper: ShadowAuditScraper) -> dict[str, int]:
    metrics = getattr(scraper, "proxy_metrics", None)
    if not isinstance(metrics, dict):
        return {}
    return {
        str(name): int(value or 0)
        for name, value in metrics.items()
        if isinstance(value, int)
    }


def _sample_previews(scraper, previews, count):
    """Cover both snapshot paths and price bands before spreading the remaining sample."""
    if count <= 0 or not previews:
        return [], [], 0
    bands = getattr(scraper, "price_bands", ((0, 300), (300, 500), (500, 700),
                                            (700, 1000), (1000, 1500), (1500, None)))
    groups = {}
    for index, preview in enumerate(previews):
        price = preview.snapshot.price_eur if preview.snapshot is not None else None
        band = next((i for i, (lo, hi) in enumerate(bands)
                     if price is not None and price >= lo and (hi is None or price < hi)), -1)
        key = ("complete" if _snapshot_is_complete(scraper, preview.snapshot) else "fallback", band)
        groups.setdefault(key, []).append(index)
    selected = []
    for indexes in groups.values():
        if len(selected) >= count:
            break
        selected.append(indexes[len(indexes)//2])
    for index in sorted(set(round(i * (len(previews)-1) / max(1, min(count, len(previews))-1))
                            for i in range(min(count, len(previews))))):
        if len(selected) >= count:
            break
        if index not in selected:
            selected.append(index)
    selected.sort()
    covered = [f"{kind}:band-{band}" for (kind, band), indexes in groups.items()
               if any(index in selected for index in indexes)]
    return [previews[index] for index in selected], covered, len(groups)


async def run_shadow_audit(
    scraper: ShadowAuditScraper,
    *,
    max_pages: int = 500,
    compare_details: int = 100,
    healthy_baseline_coverage: float | None = None,
    thresholds: ShadowAuditThresholds | None = None,
    source_budget_sec: float = 1800,
    detail_timeout_sec: float = 90,
) -> ShadowAuditReport:
    """Crawl list previews and compare a bounded sample against legacy detail pages."""
    if max_pages < 1 or compare_details < 0:
        raise ValueError("max_pages must be >= 1 and compare_details must be >= 0")

    limit = thresholds or ShadowAuditThresholds()
    report = ShadowAuditReport(
        max_pages=max_pages,
        compare_limit=compare_details,
        thresholds={
            "min_coverage": limit.min_coverage,
            "healthy_baseline_coverage": healthy_baseline_coverage,
            "max_fallback_rate": limit.max_fallback_rate,
        },
    )
    deadline = time.monotonic() + max(0.001, source_budget_sec)
    timed_out = False
    try:
        async with asyncio.timeout(max(0.001, deadline - time.monotonic())):
            previews = await scraper.list_recent(
                max_pages=max_pages, known_ids=set(), early_stop_full_known_pages=2,
            )
    except Exception as exc:
        report.failure_reasons = ["crawl_exception", _error_code(exc)]
        report.source_totals = source_total_report(scraper)
        report.proxy_metrics = _proxy_metrics(scraper)
        report.browser_proxy_errors = _proxy_error_count(scraper)
        return report
    report.previewed = len(previews)
    report.site_total = getattr(scraper, "_site_total", None)
    report.source_totals = source_total_report(scraper)
    report.skipped_pages = int(getattr(scraper, "_skipped_pages", 0) or 0)
    report.proxy_metrics = _proxy_metrics(scraper)
    report.browser_proxy_errors = _proxy_error_count(scraper)
    if report.site_total is not None and report.site_total > 0:
        report.coverage = report.previewed / report.site_total
    elif report.site_total == 0 and report.previewed == 0:
        report.coverage = 1.0

    report.crawl_complete = bool(getattr(scraper, "_crawl_complete", False))
    report.stop_reason = str(getattr(scraper, "_crawl_stop_reason", "unverified"))
    sample, report.sample_segments, report.population_segments = _sample_previews(
        scraper, previews, compare_details
    )
    for preview in sample:
        if time.monotonic() >= deadline:
            timed_out = True
            break
        report.sample_compared += 1
        snapshot = preview.snapshot
        complete = _snapshot_is_complete(scraper, snapshot)
        if not complete:
            report.fallback_count += 1
            report.fallback_listing_ids.append(preview.external_id)
        else:
            report.complete_snapshots += 1
        phase = "fetch_detail"
        missing_fields = []
        try:
            async with asyncio.timeout(min(detail_timeout_sec, deadline - time.monotonic())):
                legacy = await scraper.fetch_detail(preview.url)
            phase = "validate_detail"
            missing_fields = [name for name, missing in (
                ("title", not legacy.title), ("price_eur", legacy.price_eur is None),
                ("photo_urls", not legacy.photo_urls),
            ) if missing]
            if missing_fields:
                raise ValueError("unusable fallback/detail payload")
        except Exception as exc:  # Record codes, never exception text/request/response bodies.
            report.detail_errors += 1
            report.detail_error_listing_ids.append(preview.external_id)
            status = getattr(getattr(exc, "response", None), "status_code", None)
            report.detail_failures.append({
                "external_id": preview.external_id,
                "sample_kind": "complete" if complete else "fallback",
                "phase": phase, "error_code": _error_code(exc),
                "http_status": status if type(status) is int and 100 <= status <= 599 else None,
                "missing_fields": missing_fields,
            })
            continue
        if not complete:
            report.fallback_succeeded += 1
            continue
        assert snapshot is not None
        mismatches = _mismatch_fields(snapshot, legacy)
        if mismatches:
            report.mismatched_count += 1
            report.mismatches.append(
                {"external_id": preview.external_id, "fields": mismatches}
            )

    # Detail/fallback operations may fail after a perfectly healthy list crawl.
    report.proxy_metrics = _proxy_metrics(scraper)
    report.browser_proxy_errors = _proxy_error_count(scraper)
    fallback_rate = (
        report.fallback_count / report.sample_compared if report.sample_compared else 0.0
    )
    report.thresholds["fallback_rate"] = fallback_rate
    reasons: list[str] = []
    if timed_out or time.monotonic() >= deadline:
        reasons.append("source_timeout")
    if len(report.sample_segments) < report.population_segments:
        reasons.append("missing_segment_sample")
    if not report.sample_compared:
        reasons.append("empty_sample")
    if not report.complete_snapshots:
        reasons.append("no_complete_snapshot_sample")
    if any(
        not _snapshot_is_complete(scraper, preview.snapshot) for preview in previews
    ) and not report.fallback_count:
        reasons.append("missing_fallback_sample")
    if not report.crawl_complete:
        reasons.append("crawl_incomplete")
    if not bool(getattr(scraper, "_site_total_trusted", False)):
        reasons.append("untrusted_denominator")
    if report.site_total is None:
        reasons.append("site_total_missing")
    if report.skipped_pages != 0:
        reasons.append("skipped_pages_nonzero")
    if report.coverage < limit.min_coverage:
        reasons.append("coverage_below_threshold")
    if healthy_baseline_coverage is not None and report.coverage < healthy_baseline_coverage:
        reasons.append("coverage_below_healthy_baseline")
    if report.mismatched_count:
        reasons.append("snapshot_detail_mismatch")
    if report.detail_errors:
        reasons.append("legacy_detail_errors")
    if report.browser_proxy_errors:
        reasons.append("browser_proxy_errors")
    if limit.max_fallback_rate is not None and fallback_rate > limit.max_fallback_rate:
        reasons.append("fallback_rate_above_threshold")
    report.failure_reasons = reasons
    report.passed = not reasons
    return report


def write_report(report: ShadowAuditReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
