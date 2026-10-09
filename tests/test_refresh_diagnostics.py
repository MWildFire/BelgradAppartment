"""Synthetic failures reproduce missing per-card causes without contacting sources."""

import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import pytest

from src.db.models import Platform
from src.scrapers import orchestrator as orch
from src.scrapers.base import SourceListingInactiveError
from src.scrapers.freshness import RefreshEvidence
from src.scrapers.proxy_pool import NoProxyAvailableError
from src.scrapers.refresh_diagnostics import reason_code
from tests.test_refresh_evidence_integration import (
    detail,
    isolated_media,  # noqa: F401
    read,
    save_photos,
    scraper_for,
    seed,
)


async def test_169_successes_one_failure_preserves_cause_without_private_data(monkeypatch, tmp_path):
    rows = [SimpleNamespace(id=i, platform=Platform.HALOOGLASI, url="https://private.test/phone")
            for i in range(170)]

    async def refresh(scraper, listing_id, url, **kwargs):
        evidence = (RefreshEvidence.failed("incomplete_fields") if listing_id == 169
                    else RefreshEvidence(True, True, expected_photos=1, ready_photos=1))
        return orch._RefetchOutcome(0, 500, 500, evidence)

    monkeypatch.setattr(orch, "_refetch_one_listing", refresh)
    stats = await orch.refetch_listing_details(rows, sleep_between=0,
                                             scrapers={Platform.HALOOGLASI: scraper_for(None)})
    raw = (tmp_path / "state/freshness/refetch.json").read_text()
    checkpoint = json.loads(raw)
    assert stats.verified == 169 and stats.errors == 1 and not stats.source_errors
    assert checkpoint["stop_reason"] == "degraded"
    assert checkpoint["reason_counts"] == {"HALOOGLASI": {"item": {"incomplete_fields": 1}}}
    assert "private.test" not in raw and "phone" not in raw


async def test_proxy_failure_counts_one_attempt_two_skips_and_continues_other_source(db):
    rows = [await seed(str(i)) for i in range(3)]
    other = await seed("other", Platform.HALOOGLASI)

    class Broken(scraper_for(None)):
        async def fetch_detail(self, url):
            raise NoProxyAvailableError("https://username:password@private.test")

    stats = await orch.refetch_listing_details(
        [*rows, other], sleep_between=0, download_photos=False,
        scrapers={rows[0].platform: Broken, other.platform: scraper_for(detail(other))},
    )
    assert stats.errors == 2 and stats.skipped == 2
    assert stats.reason_counts["NEKRETNINE"] == {
        "item": {"proxy_unavailable": 1}, "skipped": {"proxy_unavailable": 2},
        "source": {"proxy_unavailable": 1},
    }
    assert (await read(rows[1])).details_refreshed_at is None
    assert (await read(other)).details_refreshed_at is not None
    assert "password" not in json.dumps(stats.reason_counts)


async def test_item_timeout_is_not_reported_as_external_cancellation(db, monkeypatch):
    row = await seed()
    monkeypatch.setattr(orch.get_settings(), "refetch_per_fetch_timeout_sec", 0.05)

    async def hanging(*args, **kwargs):
        await asyncio.Future()

    monkeypatch.setattr(orch, "_refetch_one_listing", hanging)
    stats = await orch.refetch_listing_details(
        [row], sleep_between=0, scrapers={row.platform: scraper_for(None)},
    )
    assert stats.errors == 1
    assert stats.reason_counts == {row.platform.value: {"item": {"timeout": 1}}}


async def test_source_error_survives_later_external_cancellation(db, tmp_path):
    first = await seed()
    second = await seed("other", Platform.HALOOGLASI)
    entered = asyncio.Event()

    class Broken(scraper_for(None)):
        async def __aenter__(self):
            raise type("SecretClassName", (Exception,), {})("secret payload")

    class Hanging(scraper_for(None)):
        async def fetch_detail(self, url):
            entered.set()
            await asyncio.Future()

    task = asyncio.create_task(orch.refetch_listing_details(
        [first, second], sleep_between=0, scrapers={first.platform: Broken, second.platform: Hanging},
    ))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    raw = (tmp_path / "state/freshness/refetch.json").read_text()
    checkpoint = json.loads(raw)
    assert checkpoint["source_errors"] == {first.platform.value: "RefreshError"}
    assert checkpoint["reason_counts"][first.platform.value]["source"] == {"refresh_failed": 1}
    assert checkpoint["reason_counts"][first.platform.value]["skipped"] == {"refresh_failed": 1}
    assert checkpoint["skipped"] == 1
    assert checkpoint["stop_reason"] == "cancelled" and "Secret" not in raw and "secret" not in raw


@pytest.mark.parametrize("error,expected", [
    (RuntimeError("incomplete_photos"), "incomplete_photos"),
    (RuntimeError("detail_identity_mismatch"), "detail_identity_mismatch"),
    (ValueError("private URL and credentials"), "invalid_data"),
    (SourceListingInactiveError("private URL"), "listing_inactive"),
    (type("SecretClassName", (Exception,), {})("secret"), "refresh_failed"),
    (httpx.HTTPStatusError("secret", request=httpx.Request("GET", "https://private.test"),
                           response=httpx.Response(403)), "http_forbidden"),
])
def test_exception_classifier_never_persists_arbitrary_text(error, expected):
    assert reason_code(error) == expected


async def test_photo_failure_is_distinct_from_generic_runtime_error(db, monkeypatch):
    row = await seed()

    async def partial(*args, **kwargs):
        return [], [], None

    monkeypatch.setattr(orch, "_download_photos", partial)
    stats = await orch.refetch_listing_details(
        [row], sleep_between=0, scrapers={row.platform: scraper_for(detail(row))},
    )
    assert stats.reason_counts == {row.platform.value: {"item": {"incomplete_photos": 1}}}


async def test_verified_browser_recovery_does_not_count_initial_http_403(db, monkeypatch):
    # A scraper's recovered result is the callback contract; intermediate HTTP errors
    # must not be inferred from unrelated fetch counters.
    row = await seed()
    monkeypatch.setattr(orch, "_download_photos", save_photos)
    stats = await orch.refetch_listing_details(
        [row], sleep_between=0, scrapers={row.platform: scraper_for(detail(row))},
    )
    assert stats.verified == 1 and stats.errors == 0 and stats.reason_counts == {}


async def test_exhausted_budget_records_skips_without_opening_source(db):
    row = await seed()

    class Forbidden(scraper_for(None)):
        async def __aenter__(self):
            raise AssertionError("an exhausted budget must not open a browser")

    stats = await orch.refetch_listing_details(
        [row], deadline=time.monotonic() - 1, scrapers={row.platform: Forbidden},
    )
    assert stats.errors == 0 and stats.skipped == 1
    assert stats.reason_counts == {row.platform.value: {"skipped": {"source_budget_exhausted": 1}}}
    assert (await read(row)).details_refreshed_at is None


async def test_persistence_failure_retains_original_item_cause(db, monkeypatch):
    row = await seed()

    class Broken(scraper_for(None)):
        async def fetch_detail(self, url):
            raise RuntimeError("empty_detail")

    async def fail_to_record(*args, **kwargs):
        raise PermissionError("sensitive local path")

    monkeypatch.setattr(orch, "record_attempt", fail_to_record)
    stats = await orch.refetch_listing_details(
        [row], sleep_between=0, scrapers={row.platform: Broken},
    )
    assert stats.errors == 1
    assert stats.reason_counts == {row.platform.value: {
        "item": {"empty_detail": 1}, "persistence": {"permission_denied": 1},
    }}
