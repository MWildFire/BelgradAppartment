from __future__ import annotations

import json

import pytest

from src.db.models import Platform
from src.scrapers.base import ListingDetail, ListingPreview
from src.scrapers.shadow_audit import ShadowAuditThresholds, run_shadow_audit, write_report


def _detail(external_id: str, *, district: str = "Vracar") -> ListingDetail:
    return ListingDetail(
        platform=Platform.NEKRETNINE,
        external_id=external_id,
        url=f"https://www.nekretnine.rs/oglasi/{external_id}/",
        price_eur=700,
        area_m2=50.0,
        rooms=2.0,
        district=district,
        address_raw="PRIVATE ADDRESS MUST NOT LEAK",
        lat=44.80,
        lon=20.46,
        title="Flat",
        photo_urls=[
            f"https://pic.nekretnine.rs/image/{external_id}01/xxl.jpg",
            f"https://pic.nekretnine.rs/image/{external_id}02/xxl.jpg",
        ],
    )


class FakeScraper:
    def __init__(self, previews: list[ListingPreview], details: dict[str, ListingDetail]) -> None:
        self.previews = previews
        self.details = details
        self._site_total = len(previews)
        self._skipped_pages = 0
        self.proxy_metrics = {"browser_proxy_errors": 0}
        self.fetches: list[str] = []

    async def list_recent(self, **_kwargs: object) -> list[ListingPreview]:
        return self.previews

    async def fetch_detail(self, url: str) -> ListingDetail:
        external_id = url.rstrip("/").split("/")[-1]
        self.fetches.append(external_id)
        return self.details[external_id]

    @staticmethod
    def is_snapshot_complete(snapshot: ListingDetail | None) -> bool:
        return snapshot is not None and bool(snapshot.photo_urls and snapshot.district)


@pytest.mark.asyncio
async def test_shadow_audit_passes_and_fetches_only_complete_sample() -> None:
    first = _detail("1")
    second = _detail("2")
    previews = [
        ListingPreview(Platform.NEKRETNINE, "1", first.url, snapshot=first),
        ListingPreview(Platform.NEKRETNINE, "2", second.url, snapshot=ListingDetail(
            platform=Platform.NEKRETNINE, external_id="2", url=second.url,
        )),
    ]
    scraper = FakeScraper(previews, {"1": first, "2": second})

    report = await run_shadow_audit(scraper, max_pages=2, compare_details=2)

    assert report.passed is True
    assert scraper.fetches == ["1"]
    assert report.fallback_listing_ids == ["2"]
    assert report.coverage == 1.0
    assert report.proxy_metrics == {"browser_proxy_errors": 0}


@pytest.mark.asyncio
async def test_shadow_audit_fails_closed_on_mismatch_skip_and_proxy_error() -> None:
    snapshot = _detail("1")
    legacy = _detail("1", district="Zemun")
    scraper = FakeScraper(
        [ListingPreview(Platform.NEKRETNINE, "1", snapshot.url, snapshot=snapshot)],
        {"1": legacy},
    )
    scraper._site_total = 2
    scraper._skipped_pages = 1
    scraper.proxy_metrics = {"browser_proxy_errors": 1}

    report = await run_shadow_audit(
        scraper,
        max_pages=2,
        compare_details=1,
        healthy_baseline_coverage=0.99,
        thresholds=ShadowAuditThresholds(min_coverage=0.98),
    )

    assert report.passed is False
    assert {
        "skipped_pages_nonzero",
        "coverage_below_threshold",
        "snapshot_detail_mismatch",
        "browser_proxy_errors",
    } <= set(report.failure_reasons)
    assert report.mismatches == [{"external_id": "1", "fields": ["district"]}]


@pytest.mark.asyncio
async def test_shadow_audit_report_is_sanitized(tmp_path) -> None:
    detail = _detail("1")
    scraper = FakeScraper([
        ListingPreview(Platform.NEKRETNINE, "1", detail.url, snapshot=detail),
    ], {"1": detail})
    report = await run_shadow_audit(scraper, max_pages=1, compare_details=1)
    path = tmp_path / "audit.json"

    write_report(report, path)
    content = path.read_text(encoding="utf-8")
    payload = json.loads(content)
    assert "PRIVATE ADDRESS" not in content
    assert detail.url not in content
    assert payload["mismatches"] == []


@pytest.mark.asyncio
async def test_shadow_audit_rejects_missing_site_total() -> None:
    detail = _detail("1")
    scraper = FakeScraper([
        ListingPreview(Platform.NEKRETNINE, "1", detail.url, snapshot=detail),
    ], {"1": detail})
    scraper._site_total = None

    report = await run_shadow_audit(scraper, max_pages=1, compare_details=1)

    assert report.passed is False
    assert "site_total_missing" in report.failure_reasons
