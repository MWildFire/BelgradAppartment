"""Admin /health dashboard (B3) — per-platform scraper status + monitor staleness.

Read-only aggregation over existing tables/state (no migration, no new persistence):
  * build_health(session) gathers per-platform last-seen / active-count / 7-day trend /
    deactivated-24h, monitor last-inbound staleness, and the fingerprint-baseline mtime.
  * GET /health renders that into a single «всё ли живо» screen for Дарья.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from src.admin.app import create_app
from src.admin.routes import (
    INBOUND_STALE_WARN_HOURS,
    SCRAPE_STALE_WARN_DAYS,
    build_health,
)
from src.db.models import DailyMetrics, InboundPost, Listing, Platform, SourceChannel
from tests.conftest import fresh_db

CREDS = base64.b64encode(b"darya:test_secret_123").decode()
AUTH = {"Authorization": f"Basic {CREDS}"}


@pytest.fixture(autouse=True)
def _force_admin_password(monkeypatch):
    from src.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "admin_username", "darya")
    monkeypatch.setattr(s, "admin_password", "test_secret_123")
    yield


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    """Point data_dir at a temp dir so the fingerprint file never leaks between tests / real data."""
    from src.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "data_dir", tmp_path)
    yield


@pytest_asyncio.fixture
async def db():
    async with fresh_db() as (_engine, factory):
        yield factory


@pytest.fixture
def client():
    return TestClient(create_app())


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _seed_listing(db, **kw) -> None:
    async with db() as s:
        s.add(
            Listing(
                platform=kw.pop("platform", Platform.NEKRETNINE),
                external_id=kw.pop("external_id", "1"),
                url=kw.pop("url", "https://example.com/1"),
                **kw,
            )
        )
        await s.commit()


async def _seed_metric(db, platform: str, date: str, active: int) -> None:
    async with db() as s:
        s.add(
            DailyMetrics(
                snapshot_date=date, platform=platform, active_count=active, no_photo_count=0
            )
        )
        await s.commit()


async def _seed_inbound(db, received_at: datetime) -> None:
    async with db() as s:
        ch = SourceChannel(tg_id=int(received_at.timestamp()))
        s.add(ch)
        await s.flush()
        s.add(
            InboundPost(
                source_channel_id=ch.id,
                tg_message_id=int(received_at.timestamp()),
                received_at=received_at,
            )
        )
        await s.commit()


def _platform(report, value: str):
    return next(p for p in report.platforms if p["platform"] == value)


# --------------------------------------------------------------------------- build_health (unit)


@pytest.mark.asyncio
async def test_build_health_per_platform_active_count_and_last_seen(db):
    seen = _now() - timedelta(hours=3)
    await _seed_listing(db, platform=Platform.NEKRETNINE, external_id="a", is_active=True,
                        last_seen_at=seen)
    await _seed_listing(db, platform=Platform.NEKRETNINE, external_id="b", is_active=True,
                        last_seen_at=seen - timedelta(days=5))
    # inactive listing must NOT count toward active_count
    await _seed_listing(db, platform=Platform.NEKRETNINE, external_id="c", is_active=False,
                        last_seen_at=seen)

    async with db() as s:
        report = await build_health(s)

    nk = _platform(report, "NEKRETNINE")
    assert nk["active_count"] == 2
    # last_seen proxy = MAX(last_seen_at) over the platform (active OR not)
    assert nk["last_seen_at"] is not None
    assert abs((nk["last_seen_at"] - seen).total_seconds()) < 2


@pytest.mark.asyncio
async def test_build_health_lists_all_platforms_even_when_empty(db):
    async with db() as s:
        report = await build_health(s)
    values = {p["platform"] for p in report.platforms}
    assert values == {"NEKRETNINE", "CETIRI_ZIDA", "HALOOGLASI", "CITYEXPERT"}
    # empty platform: zero active, no last-seen, no crash
    for p in report.platforms:
        assert p["active_count"] == 0
        assert p["last_seen_at"] is None


@pytest.mark.asyncio
async def test_build_health_7day_trend_from_daily_metrics(db):
    today = _now().date()
    # 3 daily snapshots for one platform → trend is the active_count series oldest→newest
    for i, active in enumerate([100, 120, 90]):
        d = (today - timedelta(days=2 - i)).isoformat()
        await _seed_metric(db, "CETIRI_ZIDA", d, active)

    async with db() as s:
        report = await build_health(s)
    cz = _platform(report, "CETIRI_ZIDA")
    assert cz["trend"] == [100, 120, 90]


@pytest.mark.asyncio
async def test_build_health_trend_window_is_seven_days(db):
    today = _now().date()
    # one point inside the 7-day window, one well outside → only the recent one shows
    await _seed_metric(db, "HALOOGLASI", (today - timedelta(days=2)).isoformat(), 50)
    await _seed_metric(db, "HALOOGLASI", (today - timedelta(days=30)).isoformat(), 999)

    async with db() as s:
        report = await build_health(s)
    ha = _platform(report, "HALOOGLASI")
    assert ha["trend"] == [50]
    assert 999 not in ha["trend"]


@pytest.mark.asyncio
async def test_build_health_deactivated_last_24h(db):
    now = _now()
    # inactive + recently confirmed inactive → counts as deactivated-24h
    await _seed_listing(db, platform=Platform.NEKRETNINE, external_id="d1", is_active=False,
                        deactivated_at=now - timedelta(hours=2))
    # inactive but confirmed inactive long ago → NOT in the 24h window
    await _seed_listing(db, platform=Platform.NEKRETNINE, external_id="d2", is_active=False,
                        deactivated_at=now - timedelta(days=3))
    # active → never counted
    await _seed_listing(db, platform=Platform.NEKRETNINE, external_id="d3", is_active=True,
                        deactivated_at=now - timedelta(hours=1))

    async with db() as s:
        report = await build_health(s)
    nk = _platform(report, "NEKRETNINE")
    assert nk["deactivated_24h"] == 1


@pytest.mark.asyncio
async def test_build_health_scrape_status_warns_when_stale(db):
    fresh = _now() - timedelta(hours=1)
    stale = _now() - timedelta(days=SCRAPE_STALE_WARN_DAYS + 1)
    await _seed_listing(db, platform=Platform.NEKRETNINE, external_id="fresh", last_seen_at=fresh)
    await _seed_listing(db, platform=Platform.CETIRI_ZIDA, external_id="stale", last_seen_at=stale)

    async with db() as s:
        report = await build_health(s)
    assert _platform(report, "NEKRETNINE")["status"] == "ok"
    assert _platform(report, "CETIRI_ZIDA")["status"] == "warn"
    # a platform with zero listings at all is a hard problem, not just "warn"
    assert _platform(report, "HALOOGLASI")["status"] == "crit"


# --------------------------------------------------------------------------- monitor staleness


@pytest.mark.asyncio
async def test_build_health_monitor_fresh(db):
    recent = _now() - timedelta(minutes=30)
    await _seed_inbound(db, recent)
    async with db() as s:
        report = await build_health(s)
    assert report.monitor["last_inbound_at"] is not None
    assert report.monitor["status"] == "ok"
    assert report.monitor["age_hours"] is not None
    assert report.monitor["age_hours"] < 1


@pytest.mark.asyncio
async def test_build_health_monitor_stale_warns(db):
    old = _now() - timedelta(hours=INBOUND_STALE_WARN_HOURS + 5)
    await _seed_inbound(db, old)
    async with db() as s:
        report = await build_health(s)
    assert report.monitor["status"] == "warn"


@pytest.mark.asyncio
async def test_build_health_monitor_never_received(db):
    async with db() as s:
        report = await build_health(s)
    assert report.monitor["last_inbound_at"] is None
    assert report.monitor["age_hours"] is None
    # no inbound ever = the monitor has never produced → a hard flag, not silent ok
    assert report.monitor["status"] == "crit"


# --------------------------------------------------------------------------- fingerprint baseline


@pytest.mark.asyncio
async def test_build_health_fingerprint_absent(db):
    # _isolate_state points data_dir at an empty tmp → no parser_fingerprints.json
    async with db() as s:
        report = await build_health(s)
    assert report.fingerprint["present"] is False
    assert report.fingerprint["mtime"] is None
    assert report.fingerprint["platforms"] == []


@pytest.mark.asyncio
async def test_build_health_fingerprint_present(db, tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    fp = state_dir / "parser_fingerprints.json"
    fp.write_text(
        json.dumps({"NEKRETNINE": {"source": "__NEXT_DATA__"}, "CETIRI_ZIDA": {"source": "ld"}}),
        encoding="utf-8",
    )
    async with db() as s:
        report = await build_health(s)
    assert report.fingerprint["present"] is True
    assert report.fingerprint["mtime"] is not None
    assert set(report.fingerprint["platforms"]) == {"NEKRETNINE", "CETIRI_ZIDA"}


@pytest.mark.asyncio
async def test_build_health_fingerprint_corrupt_is_handled(db, tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "parser_fingerprints.json").write_text("{ this is not json", encoding="utf-8")
    async with db() as s:
        report = await build_health(s)
    # file exists but unreadable → present True, platforms empty, no crash
    assert report.fingerprint["present"] is True
    assert report.fingerprint["platforms"] == []


# --------------------------------------------------------------------------- GET /health route


def test_health_requires_auth(client, db):
    assert client.get("/health").status_code == 401


def test_health_empty_db_renders(client, db):
    r = client.get("/health", headers=AUTH)
    assert r.status_code == 200
    # all three platforms still listed even with no data — no division/None crash
    assert "NEKRETNINE" in r.text
    assert "CETIRI_ZIDA" in r.text
    assert "HALOOGLASI" in r.text


@pytest.mark.asyncio
async def test_health_renders_seeded_data(client, db):
    await _seed_listing(db, platform=Platform.NEKRETNINE, external_id="x", is_active=True,
                        last_seen_at=_now() - timedelta(hours=2))
    await _seed_inbound(db, _now() - timedelta(minutes=10))
    r = client.get("/health", headers=AUTH)
    assert r.status_code == 200
    assert "NEKRETNINE" in r.text
    # monitor row present
    assert "Монитор" in r.text or "монитор" in r.text.lower()


def test_health_in_nav(client, db):
    r = client.get("/health", headers=AUTH)
    assert r.status_code == 200
    assert 'href="/health"' in r.text
