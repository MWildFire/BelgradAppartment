"""Read-only Nekretnine snapshot-vs-detail audit.

The audit deliberately depends only on the scraper interface.  It does not open a database
session, run the orchestrator, perform a stale sweep, or know anything about Telegram delivery.
That makes the command safe to run before enabling ``active`` snapshot ingestion.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse
from uuid import uuid4

from src.scrapers.base import ListingDetail, ListingPreview
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
    skipped_pages: int = 0
    coverage: float = 0.0
    sample_compared: int = 0
    complete_snapshots: int = 0
    fallback_count: int = 0
    mismatched_count: int = 0
    detail_errors: int = 0
    browser_proxy_errors: int = 0
    proxy_metrics: dict[str, int] = field(default_factory=dict)
    mismatches: list[dict[str, Any]] = field(default_factory=list)
    fallback_listing_ids: list[str] = field(default_factory=list)
    detail_error_listing_ids: list[str] = field(default_factory=list)
    thresholds: dict[str, Any] = field(default_factory=dict)
    passed: bool = False
    failure_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return only sanitized evidence; no URLs, addresses, credentials, or raw payloads."""
        return asdict(self)


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
    return sum(
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


async def run_shadow_audit(
    scraper: ShadowAuditScraper,
    *,
    max_pages: int = 500,
    compare_details: int = 100,
    healthy_baseline_coverage: float | None = None,
    thresholds: ShadowAuditThresholds | None = None,
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
    previews = await scraper.list_recent(
        max_pages=max_pages,
        known_ids=set(),
        early_stop_full_known_pages=2,
    )
    report.previewed = len(previews)
    report.site_total = getattr(scraper, "_site_total", None)
    report.skipped_pages = int(getattr(scraper, "_skipped_pages", 0) or 0)
    report.proxy_metrics = _proxy_metrics(scraper)
    report.browser_proxy_errors = _proxy_error_count(scraper)
    if report.site_total is not None and report.site_total > 0:
        report.coverage = report.previewed / report.site_total
    elif report.site_total == 0 and report.previewed == 0:
        report.coverage = 1.0

    for preview in previews[:compare_details]:
        report.sample_compared += 1
        snapshot = preview.snapshot
        if not _snapshot_is_complete(scraper, snapshot):
            report.fallback_count += 1
            report.fallback_listing_ids.append(preview.external_id)
            continue
        report.complete_snapshots += 1
        try:
            legacy = await scraper.fetch_detail(preview.url)
        except Exception:  # report the listing, then fail closed.
            report.detail_errors += 1
            report.detail_error_listing_ids.append(preview.external_id)
            continue
        assert snapshot is not None
        mismatches = _mismatch_fields(snapshot, legacy)
        if mismatches:
            report.mismatched_count += 1
            report.mismatches.append(
                {"external_id": preview.external_id, "fields": mismatches}
            )

    fallback_rate = (
        report.fallback_count / report.sample_compared if report.sample_compared else 0.0
    )
    report.thresholds["fallback_rate"] = fallback_rate
    reasons: list[str] = []
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
