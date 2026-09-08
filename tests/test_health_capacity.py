"""Admin capacity remains measured, read-only and honest about unknown evidence."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from jinja2 import Environment, FileSystemLoader
from sqlalchemy import event, insert

from src.admin.operational_health import build_operations, proxy_service_receipt, refresh_capacity
from src.config import get_settings
from src.db.models import InboundPost, Listing, Match, Platform, SiteListing, SourceChannel
from src.db.session import get_session
from src.scrapers.freshness import RefreshCandidate

NOW = datetime(2026, 9, 8, 12, tzinfo=UTC)


def settings(quota=200):
    return SimpleNamespace(
        nightly_detail_refresh=quota, nightly_heal_limit=500, nightly_refetch_budget_sec=7200
    )


def row(number, **kwargs):
    return RefreshCandidate(
        number, Platform.NEKRETNINE, f"https://example.invalid/{number}", **kwargs
    )


def test_production_sized_inventory_proves_default_quota_is_insufficient():
    inventory = [row(number, public=number <= 184) for number in range(1, 13190)]
    capacity = refresh_capacity(inventory, settings=settings(), now=NOW)
    assert capacity["active"] == 13189 and capacity["public_originals"] == 184
    assert capacity["minimum_daily_successes"] == 1948
    assert capacity["configured_daily_attempts"] == 200
    assert capacity["minimum_daily_shortfall"] == 1748
    assert capacity["status"] == "insufficient_even_at_full_success"
    assert capacity["estimated_daily_attempts"] is None
    assert capacity["attainment_confirmed"] is False


def test_last_attempt_sample_excludes_legacy_old_and_future_records():
    attempt = NOW - timedelta(hours=1)
    inventory = [
        row(1, attempted_at=attempt, succeeded_at=attempt + timedelta(seconds=30)),
        row(
            2,
            attempted_at=attempt,
            succeeded_at=attempt - timedelta(days=1),
            failure_count=2,
            next_retry_at=NOW + timedelta(minutes=30),
        ),
        row(3, attempted_at=attempt),  # legacy timestamp alone is neither success nor failure
        row(4, attempted_at=NOW - timedelta(days=8), succeeded_at=NOW - timedelta(days=8)),
        row(5, attempted_at=NOW + timedelta(hours=1), succeeded_at=NOW + timedelta(hours=1)),
        row(6, attempted_at=attempt, failure_count=1),  # no instrumented retry evidence
        row(7, attempted_at=attempt, succeeded_at=NOW + timedelta(days=1)),
    ]
    capacity = refresh_capacity(inventory, settings=settings(), now=NOW)
    source = capacity["sources"][Platform.NEKRETNINE.value]
    assert source["latest_attempt_sample"] == {"succeeded": 1, "failed": 1, "unknown": 3}
    assert source["measured_success_rate"] == 0.5
    assert source["estimated_daily_attempts"] == 2
    assert source["fresh_active_7d"] == 2
    assert capacity["sample_kind"] == "latest_attempt_per_listing"
    assert capacity["attainment_confirmed"] is False


def test_zero_rate_and_unmeasured_source_never_produce_finite_capacity():
    failure = row(1, attempted_at=NOW, failure_count=1, next_retry_at=NOW + timedelta(minutes=30))
    capacity = refresh_capacity([failure], settings=settings(), now=NOW)
    assert capacity["status"] == "no_observed_success"
    assert capacity["estimated_daily_attempts"] is None
    success = row(1, attempted_at=NOW, succeeded_at=NOW)
    unknown = RefreshCandidate(2, Platform.CITYEXPERT, "https://example.invalid/2")
    capacity = refresh_capacity([success, unknown], settings=settings(), now=NOW)
    assert capacity["status"] == "unverified"
    assert capacity["estimated_daily_attempts"] is None


async def test_database_overlay_count_unique_public_originals_and_ui_are_read_only(
    db, monkeypatch, tmp_path
):
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    monkeypatch.setattr(get_settings(), "nightly_detail_refresh", 0)
    monkeypatch.setattr("src.admin.operational_health.schedules", lambda: {})
    async with get_session() as session:
        await session.execute(
            insert(SourceChannel), [{"id": 1, "tg_id": 1, "title": "fixture"}]
        )
        await session.execute(
            insert(InboundPost),
            [{"id": n, "source_channel_id": 1, "tg_message_id": n} for n in (1, 2, 3)],
        )
        await session.execute(
            insert(Listing),
            [
                {
                    "id": 1,
                    "platform": Platform.NEKRETNINE,
                    "external_id": "1",
                    "url": "https://x/1",
                    "details_refreshed_at": NOW,
                    "details_succeeded_at": NOW,
                    "is_active": True,
                },
                {
                    "id": 2,
                    "platform": Platform.NEKRETNINE,
                    "external_id": "2",
                    "url": "https://x/2",
                    "is_active": False,
                },
            ],
        )
        await session.execute(
            insert(Match),
            [{"id": n, "listing_id": 1 if n < 3 else 2, "inbound_post_id": n} for n in (1, 2, 3)],
        )
        await session.execute(
            insert(SiteListing), [{"id": n, "match_id": n, "is_public": True} for n in (1, 2, 3)]
        )

    def reject_write(_conn, _cursor, statement, _parameters, _context, _many):
        assert statement.lstrip().upper().startswith("SELECT")

    event.listen(db.sync_engine, "before_cursor_execute", reject_write)
    async with get_session() as session:
        operations = await build_operations(session, now=NOW)
    assert operations["public_overlays"] == 3
    assert operations["public"] == 2 and operations["public_fresh"] == 1
    assert operations["refresh_capacity"]["active"] == 1
    assert operations["refresh_capacity"]["configured_daily_attempts"] == 0
    assert any("24-hour" in reason for reason in operations["reasons"])
    assert any("quota" in reason for reason in operations["reasons"])
    assert not list(tmp_path.iterdir())

    environment = Environment(
        loader=FileSystemLoader(Path(__file__).parents[1] / "src/admin/templates"), autoescape=True
    )
    report = {
        "now": NOW,
        "operations": operations,
        "platforms": [],
        "monitor": {"status": "warn", "last_inbound_at": None},
        "fingerprint": {"present": False},
    }
    rendered = environment.get_template("health.html").render(
        report=report,
        request=SimpleNamespace(
            url=SimpleNamespace(path="/health"), state=SimpleNamespace(csrf_token="fixture")
        ),
    )
    assert "Лимита недостаточно даже при 100% успехе" in rendered
    assert "3 публичных публикаций" in rendered
    assert "2 уникальных исходников" in rendered
    assert "выборка только последней попытки" in rendered
    assert "не подтверждена" in rendered


def receipt(verified=NOW, expires=None):
    return {
        "verified_at": verified.isoformat(),
        "orders": [
            {
                "order_id": "80647128",
                "expires_at": (expires or datetime(2026, 9, 25, 8, 18, tzinfo=UTC)).isoformat(),
                "subscription_enabled": True,
            },
            {
                "order_id": "80643004",
                "expires_at": "2026-09-25T11:15:00+04:00",
                "subscription_enabled": True,
            },
        ],
    }


def test_receipt_reports_nearest_utc_expiry_and_only_safe_projection(tmp_path):
    path = tmp_path / "proxy_service_receipt.json"
    value = receipt()
    value["endpoint"] = "private endpoint must never reach response"
    value["orders"][0]["credential"] = "private value must never reach response"
    path.write_text(json.dumps(value))
    report = proxy_service_receipt(path, now=NOW)
    assert report["status"] == "verified"
    assert report["nearest_expiry"] == datetime(2026, 9, 25, 7, 15, tzinfo=UTC)
    assert "private" not in repr(report)
    assert {order["order_id"] for order in report["orders"]} == {"80647128", "80643004"}


@pytest.mark.parametrize(
    ("value", "status"),
    [
        (receipt(verified=NOW - timedelta(hours=37)), "dated"),
        (receipt(verified=NOW + timedelta(hours=1)), "unknown"),
        (receipt(expires=NOW - timedelta(seconds=1)), "expired"),
        (receipt(expires=NOW + timedelta(days=2)), "expiring"),
        ({"verified_at": NOW.isoformat(), "orders": []}, "unknown"),
        ([], "unknown"),
    ],
)
def test_proxy_receipt_never_turns_stale_invalid_or_expired_evidence_healthy(
    tmp_path, value, status
):
    path = tmp_path / "proxy_service_receipt.json"
    path.write_text(json.dumps(value))
    assert proxy_service_receipt(path, now=NOW)["status"] == status


def test_proxy_receipt_missing_malformed_and_disabled_subscription(tmp_path):
    path = tmp_path / "proxy_service_receipt.json"
    assert proxy_service_receipt(path, now=NOW)["status"] == "unknown"
    path.write_text("bad json")
    assert proxy_service_receipt(path, now=NOW)["status"] == "unknown"
    value = receipt()
    value["orders"][0]["subscription_enabled"] = False
    path.write_text(json.dumps(value))
    assert proxy_service_receipt(path, now=NOW)["status"] == "renewal_disabled"
    value["orders"][0]["subscription_enabled"] = "true"
    path.write_text(json.dumps(value))
    assert proxy_service_receipt(path, now=NOW)["status"] == "unknown"
