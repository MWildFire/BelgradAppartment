"""Diagnostics retain drift/failure provenance without changing fail-closed decisions."""

import json
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest

from src.db.models import Platform
from src.scrapers.base import ListingPreview
from src.scrapers.crawl_evidence import CrawlEvidence, source_total_report
from src.scrapers.nekretnine import NekretnineScraper
from src.scrapers.orchestrator import ScrapeResult, _copy_crawl_evidence
from src.scrapers.shadow_audit import run_shadow_audit
from tests.test_crawl_completeness import nek_page
from tests.test_shadow_audit import FakeScraper, _detail


class Evidence(CrawlEvidence):
    def __init__(self):
        self._site_total = None
        self._skipped_pages = 0
        self.reset_crawl_evidence()


def test_totals_drift_keeps_first_denominator_and_proves_when_it_changed():
    source = Evidence()
    for page, total in enumerate([4484, 4483, 4483, 4484], start=1):
        source.record_page_attempt()
        source.record_source_total(total, scope="base", page=page)
    source.finish_crawl_evidence(4484)
    assert not source._crawl_complete and not source._site_total_trusted
    assert source._site_total == 4484 and source._crawl_stop_reason == "source_total_changed"
    totals = source_total_report(source)
    assert [totals[name] for name in ("first", "last", "min", "max", "change_count")] == [
        4484, 4484, 4483, 4484, 2,
    ]
    assert totals["observations"] == 4
    assert [change["page"] for change in totals["changes"]] == [2, 4]
    assert totals["first_observation"]["page"] == 1
    assert totals["last_observation"]["page"] == 4
    assert all(change["scope"] == "base" for change in totals["changes"])
    assert all(datetime.fromisoformat(change["observed_at"]).tzinfo for change in totals["changes"])
    result = ScrapeResult(platform=Platform.NEKRETNINE)
    _copy_crawl_evidence(result, source)
    assert result.to_report()["source_totals"] == totals
    source.reset_crawl_evidence()
    assert result.to_report()["source_totals"] == totals


def test_many_total_changes_are_bounded_but_not_discarded_from_counts():
    source = Evidence()
    for page in range(100):
        source.record_source_total(100 + page % 2, scope="private https://example.test/token", page=page + 1)
    totals = source_total_report(source)
    assert totals["change_count"] == 99
    assert len(totals["changes"]) == 64 and totals["changes_omitted"] == 35
    assert totals["last_observation"]["page"] == 100
    assert "private" not in json.dumps(totals) and "https" not in json.dumps(totals)


async def test_real_nek_page_flow_records_base_scope_and_changed_page():
    def handler(request):
        page = int(request.url.params.get("pag", 1))
        return httpx.Response(200, text=nek_page([page], 2 if page == 1 else 3,
                                               page=page, pages=2))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        scraper = NekretnineScraper(client=client)
        scraper.page_delay_sec = scraper.retry_backoff_sec = 0
        rows = await scraper.list_recent(max_pages=2)
    assert len(rows) == 2 and scraper._site_total == 2
    assert not scraper._crawl_complete and not scraper._site_total_trusted
    totals = source_total_report(scraper)
    assert totals["first"] == 2 and totals["last"] == 3
    assert totals["changes"][0]["page"] == 2
    assert totals["changes"][0]["scope"] == "base"


@pytest.mark.parametrize("failure,code,status,missing", [
    ("http", "HTTPStatusError", 404, []),
    ("timeout", "TimeoutError", None, []),
    ("incomplete", "ValueError", None, ["price_eur", "photo_urls"]),
])
async def test_failed_detail_audit_reports_codes_not_exception_or_payload(failure, code, status, missing):
    good, bad = _detail("1"), _detail("2")
    incomplete = _detail("2")
    incomplete.district = None

    class Failed(FakeScraper):
        async def fetch_detail(self, url):
            if url == good.url:
                return good
            if failure == "http":
                raise httpx.HTTPStatusError(
                    "private credential should never escape", request=httpx.Request("GET", url),
                    response=httpx.Response(404),
                )
            if failure == "timeout":
                raise TimeoutError("private proxy URL should never escape")
            return SimpleNamespace(title="private title", price_eur=None, photo_urls=[])

    scraper = Failed([
        ListingPreview(Platform.NEKRETNINE, "1", good.url, snapshot=good),
        ListingPreview(Platform.NEKRETNINE, "2", bad.url, snapshot=incomplete),
    ], {})
    source = Evidence()
    source.record_source_total(2, scope="base", page=1)
    scraper._source_total_telemetry = source_total_report(source)
    report = await run_shadow_audit(scraper, compare_details=2)
    assert not report.passed and report.detail_errors == 1
    assert report.failure_reasons == ["legacy_detail_errors"]
    assert report.source_totals["first"] == 2
    assert report.detail_failures == [{
        "external_id": "2", "sample_kind": "fallback",
        "phase": "validate_detail" if failure == "incomplete" else "fetch_detail",
        "error_code": code, "http_status": status, "missing_fields": missing,
    }]
    assert "private" not in json.dumps(report.to_dict()).lower()
    assert "https://" not in json.dumps(report.to_dict())


async def test_crawl_exception_retains_partial_total_telemetry():
    class Failed:
        async def list_recent(self, **kwargs):
            raise TimeoutError("private diagnostic")

    scraper = Failed()
    source = Evidence()
    source.record_source_total(5, scope="base", page=1)
    scraper._source_total_telemetry = source_total_report(source)
    report = await run_shadow_audit(scraper)
    assert not report.passed and report.failure_reasons == ["crawl_exception", "TimeoutError"]
    assert report.source_totals["last_observation"]["page"] == 1
