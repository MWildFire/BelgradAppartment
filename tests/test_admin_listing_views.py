"""Stage T · T3-2 (stretch) — колонка «👁 7д» в /published (admin-список публичных листингов).

views_7d_by_apt(session, apt_ids): SUM(value) per site_listings.id из агрегатов
site_stats_daily(metric='views_apt', day >= today-7). Live-«сегодня» из raw НЕ добирается —
задача про порядок величины для Дарьи, не про точную сводку (точная — /analytics, T3-1).
Роут /published показывает бейдж только у карточек с SiteListing-оверлеем: ключ метрики —
site_listings.id, без оверлея квартире просто нечем быть посчитанной.
"""

from __future__ import annotations

import base64
import itertools
from datetime import UTC, date, datetime, timedelta

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from src.admin.analytics import views_7d_by_apt
from src.admin.app import create_app
from src.db.models import (
    InboundPost,
    InboundStatus,
    Listing,
    Match,
    MatchStatus,
    Platform,
    PublishedPost,
    SiteListing,
    SiteStatsDaily,
    SourceChannel,
)
from tests.conftest import fresh_db

CREDS = base64.b64encode(b"darya:test_secret_123").decode()
AUTH = {"Authorization": f"Basic {CREDS}"}

# детерминированные tg_id/external_id для сидов (hash() зависит от PYTHONHASHSEED)
_SEQ = itertools.count(910_000_001)


@pytest.fixture(autouse=True)
def _force_admin_password(monkeypatch):
    from src.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "admin_username", "darya")
    monkeypatch.setattr(s, "admin_password", "test_secret_123")
    yield


@pytest_asyncio.fixture
async def db():
    async with fresh_db() as (_engine, factory):
        yield factory


@pytest.fixture
def client():
    return TestClient(create_app())


def _today() -> date:
    return datetime.now(UTC).date()


async def _seed_stat(db, day: date, metric: str, key: str, value: int) -> None:
    async with db() as s:
        s.add(SiteStatsDaily(day=day, metric=metric, key=key, value=value))
        await s.commit()


async def _seed_published(db, *, with_site: bool = True) -> tuple[int, int | None]:
    """Доставленный кандидат для /published: Match(PUBLISHED)+PublishedPost(+SiteListing).

    Возвращает (match_id, site_listing_id | None) — site-id и есть apt_id метрики.
    """
    n = next(_SEQ)
    async with db() as s:
        ch = SourceChannel(tg_id=n, username=f"src{n}", is_active=True)
        s.add(ch)
        await s.flush()
        listing = Listing(
            platform=Platform.NEKRETNINE,
            external_id=f"ext{n}",
            url=f"https://nekretnine.rs/{n}",
            price_eur=850,
        )
        ib = InboundPost(
            source_channel_id=ch.id,
            tg_message_id=n,
            status=InboundStatus.MATCHED,
        )
        s.add_all([listing, ib])
        await s.flush()
        m = Match(
            inbound_post_id=ib.id,
            listing_id=listing.id,
            status=MatchStatus.PUBLISHED,
        )
        s.add(m)
        await s.flush()
        s.add(PublishedPost(match_id=m.id, tg_message_id=n, post_text="x"))
        site_id = None
        if with_site:
            site = SiteListing(match_id=m.id)
            s.add(site)
            await s.flush()
            site_id = site.id
        await s.commit()
        return m.id, site_id


# --------------------------------------------------------------------- views_7d_by_apt (unit)


async def test_sums_views_apt_per_apt_over_last_7_days(db):
    apt = 5
    await _seed_stat(db, _today() - timedelta(days=1), "views_apt", str(apt), 3)
    await _seed_stat(db, _today() - timedelta(days=7), "views_apt", str(apt), 2)  # граница входит
    await _seed_stat(db, _today() - timedelta(days=8), "views_apt", str(apt), 999)  # вне окна
    await _seed_stat(db, _today() - timedelta(days=1), "leads_apt", str(apt), 999)  # чужая метрика
    async with db() as s:
        assert await views_7d_by_apt(s, [apt]) == {apt: 5}


async def test_counts_only_requested_ids_and_omits_zero_rows(db):
    y1 = _today() - timedelta(days=1)
    await _seed_stat(db, y1, "views_apt", "1", 4)
    await _seed_stat(db, y1, "views_apt", "2", 6)  # не запрошен — не возвращается
    async with db() as s:
        result = await views_7d_by_apt(s, [1, 3])  # 3 без строк → отсутствует (caller делает .get)
    assert result == {1: 4}


async def test_empty_ids_short_circuit(db):
    async with db() as s:
        assert await views_7d_by_apt(s, []) == {}


# --------------------------------------------------------------------- GET /published (render)


async def test_published_card_shows_views_badge(client, db):
    _, apt = await _seed_published(db)
    await _seed_stat(db, _today() - timedelta(days=1), "views_apt", str(apt), 42)
    r = client.get("/published", headers=AUTH)
    assert r.status_code == 200
    assert "👁 42" in r.text and "7д" in r.text


async def test_published_card_without_stats_shows_zero(client, db):
    # оверлей есть, просмотров нет — честный «👁 0», а не пропавший бейдж
    await _seed_published(db)
    r = client.get("/published", headers=AUTH)
    assert r.status_code == 200
    assert "👁 0" in r.text


async def test_published_card_without_site_overlay_has_no_badge(client, db):
    # без SiteListing нет apt_id → бейдж не рендерится (не «👁 0»-враньё про несуществующую страницу)
    await _seed_published(db, with_site=False)
    r = client.get("/published", headers=AUTH)
    assert r.status_code == 200
    assert "👁" not in r.text
