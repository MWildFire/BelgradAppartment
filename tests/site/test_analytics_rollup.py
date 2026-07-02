"""Stage T / T2-4 — analytics.rollup(day) + analytics.retention() (DATA_CONTRACT-T §A.2).

Что закрепляем контрактно:
- rollup(day) считает агрегаты §A.2 (views_path/views_apt/uniques/leads/leads_apt/subs/
  src_utm/src_ref) по UTC-дню и пишет их в site_stats_daily; день закрывается маркером
  _rolled='':1 — даже пустой (иначе retention ждал бы его вечно);
- значения фикстуры посчитаны РУКАМИ ниже — тест не переиспользует код агрегации;
- идемпотентность: двойной прогон = те же строки без дублей; догон новых событий того же
  дня меняет числа, но не плодит строки (пересчёт дня целиком);
- retention удаляет raw старше SITE_ANALYTICS_RAW_RETENTION_DAYS ТОЛЬКО для дней с маркером
  _rolled (гард от потери несагрегированного); агрегаты site_stats_daily живут вечно.

DB — file-backed site_db из conftest (общий writer/reader-стек с record_event).
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import select

from src.config import get_settings
from src.db.models import SiteEvent, SiteStatsDaily
from src.site import db as sitedb
from src.site.analytics import retention, rollup

# Целевой день фикстуры и его соседи — граница дня по UTC полуоткрыта: [00:00, 24:00).
DAY = date(2026, 7, 1)


def _ts(hour: int, minute: int = 0, *, day: date = DAY) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)


def _ev(
    ts: datetime,
    *,
    type: str = "pageview",
    path: str = "/",
    apt_id: int | None = None,
    visitor: str = "v1",
    referrer: str | None = None,
    utm_source: str | None = None,
    is_bot: bool = False,
) -> SiteEvent:
    return SiteEvent(
        ts=ts, type=type, path=path, apt_id=apt_id, visitor=visitor,
        referrer=referrer, utm_source=utm_source, is_bot=is_bot,
    )


async def _add(url: str, *events: SiteEvent) -> None:
    async with sitedb.writer_session(database_url=url) as s:
        s.add_all(events)


async def _stats(url: str, day: date) -> set[tuple[str, str, int]]:
    async with sitedb.read_session(database_url=url) as s:
        rows = (
            await s.execute(
                select(SiteStatsDaily.metric, SiteStatsDaily.key, SiteStatsDaily.value)
                .where(SiteStatsDaily.day == day)
            )
        ).all()
    return {tuple(r) for r in rows}


async def _event_days(url: str) -> list[date]:
    async with sitedb.read_session(database_url=url) as s:
        rows = (await s.execute(select(SiteEvent.ts))).scalars().all()
    return sorted({r.date() for r in rows})


async def _seed_fixture_day(url: str) -> None:
    """Ручная фикстура §A.2 — ожидаемые агрегаты выписаны в тесте ниже, по одной строке на факт."""
    await _add(
        url,
        # -- pageviews (люди): 3× '/', 1× '/catalog', 3× '/apt' (apt 1 ×2, apt 2 ×1)
        _ev(_ts(10, 0), visitor="v1", utm_source="google",
            referrer="https://www.google.com/search?q=flat"),
        _ev(_ts(10, 5), visitor="v1"),
        # Referrer с хостом в другом регистре — домен должен слиться с google-строкой выше.
        _ev(_ts(11, 0), visitor="v2", utm_source="google", referrer="https://www.Google.com/"),
        _ev(_ts(12, 0), path="/catalog", visitor="v2", utm_source="tg"),
        _ev(_ts(12, 30), path="/apt", apt_id=1, visitor="v1"),
        _ev(_ts(13, 0), path="/apt", apt_id=1, visitor="v3", referrer="https://t.co/abc"),
        _ev(_ts(13, 30), path="/apt", apt_id=2, visitor="v2"),
        # -- бот: не попадает ни в views, ни в uniques, ни в src_* (is_bot=0 в §A.2)
        _ev(_ts(14, 0), visitor="bot1", utm_source="google",
            referrer="https://spam.example/", is_bot=True),
        # -- конверсии
        _ev(_ts(15, 0), type="lead_submit", path="/apt", apt_id=1, visitor="v1"),
        _ev(_ts(15, 30), type="lead_submit", path="/", visitor="v2"),
        _ev(_ts(16, 0), type="subscribe_submit", path="/catalog", visitor="v3"),
        # -- соседние дни: полуоткрытая граница [00:00, 24:00) — не должны попасть в DAY
        _ev(datetime(2026, 6, 30, 23, 59, 59, tzinfo=UTC), visitor="v1"),
        _ev(datetime(2026, 7, 2, 0, 0, 0, tzinfo=UTC), visitor="v1"),
    )


# Посчитано руками по фикстуре выше (боты и соседние дни исключены).
EXPECTED = {
    ("views_path", "/", 3),
    ("views_path", "/catalog", 1),
    ("views_path", "/apt", 3),
    ("views_apt", "1", 2),
    ("views_apt", "2", 1),
    ("uniques", "", 3),          # v1, v2, v3 — по всем типам событий
    ("leads", "", 2),
    ("leads_apt", "1", 1),       # лид без apt_id в leads_apt не попадает
    ("subs", "", 1),
    ("src_utm", "google", 2),
    ("src_utm", "tg", 1),
    ("src_ref", "www.google.com", 2),  # домены слиты case-insensitive
    ("src_ref", "t.co", 1),
    ("_rolled", "", 1),
}


# ============================================================== rollup: корректность


async def test_rollup_matches_hand_computed_fixture(site_db):
    await _seed_fixture_day(site_db)

    await rollup(DAY, database_url=site_db)

    assert await _stats(site_db, DAY) == EXPECTED


async def test_rollup_neighbor_days_untouched(site_db):
    """rollup(day) пишет ТОЛЬКО свой день — соседние дни не получают строк."""
    await _seed_fixture_day(site_db)

    await rollup(DAY, database_url=site_db)

    assert await _stats(site_db, date(2026, 6, 30)) == set()
    assert await _stats(site_db, date(2026, 7, 2)) == set()


async def test_rollup_empty_day_writes_only_rolled_marker(site_db):
    """Пустой день закрывается маркером — иначе retention никогда его не отпустит."""
    await rollup(DAY, database_url=site_db)

    assert await _stats(site_db, DAY) == {("_rolled", "", 1)}


# ============================================================== rollup: идемпотентность


async def test_rollup_double_run_same_numbers(site_db):
    await _seed_fixture_day(site_db)

    await rollup(DAY, database_url=site_db)
    await rollup(DAY, database_url=site_db)

    assert await _stats(site_db, DAY) == EXPECTED  # set-сравнение ловит и дубли строк


async def test_rollup_recomputes_after_new_events(site_db):
    """Пересчёт того же дня подхватывает догнавшие события (нужен для «вчера+сегодня»)."""
    await _seed_fixture_day(site_db)
    await rollup(DAY, database_url=site_db)

    await _add(site_db, _ev(_ts(23, 0), visitor="v4"))
    await rollup(DAY, database_url=site_db)

    stats = await _stats(site_db, DAY)
    assert ("views_path", "/", 4) in stats
    assert ("uniques", "", 4) in stats
    assert ("views_path", "/", 3) not in stats  # старое значение перезаписано, не задублировано


# ============================================================== retention (гард _rolled)


NOW = datetime(2026, 7, 2, 3, 0, tzinfo=UTC)  # ночь nightly; дефолт 90 дн. → cutoff 2026-04-03
OLD_ROLLED = date(2026, 3, 1)
OLD_UNROLLED = date(2026, 3, 2)
CUTOFF_DAY = date(2026, 4, 3)  # ровно на границе — старше НЕ считается
RECENT = date(2026, 7, 1)


async def _seed_retention_fixture(url: str) -> None:
    await _add(
        url,
        _ev(_ts(10, day=OLD_ROLLED), visitor="a1"),
        _ev(_ts(11, day=OLD_ROLLED), visitor="a2"),
        _ev(_ts(10, day=OLD_UNROLLED), visitor="b1"),
        _ev(_ts(10, day=CUTOFF_DAY), visitor="c1"),
        _ev(_ts(10, day=RECENT), visitor="d1"),
    )
    for day in (OLD_ROLLED, CUTOFF_DAY, RECENT):  # OLD_UNROLLED намеренно НЕ сагрегирован
        await rollup(day, database_url=url)


async def test_retention_deletes_only_rolled_days_older_than_cutoff(site_db):
    await _seed_retention_fixture(site_db)

    deleted = await retention(now=NOW, database_url=site_db)

    assert deleted == 2  # оба события OLD_ROLLED
    # Несролапленный старый день цел (гард), граница cutoff и свежий день целы (окно).
    assert await _event_days(site_db) == [OLD_UNROLLED, CUTOFF_DAY, RECENT]


async def test_retention_keeps_aggregates_forever(site_db):
    await _seed_retention_fixture(site_db)

    await retention(now=NOW, database_url=site_db)

    stats = await _stats(site_db, OLD_ROLLED)
    assert ("views_path", "/", 2) in stats  # raw ушёл, агрегат остался
    assert ("_rolled", "", 1) in stats


async def test_retention_idempotent_second_run_deletes_nothing(site_db):
    await _seed_retention_fixture(site_db)

    await retention(now=NOW, database_url=site_db)
    assert await retention(now=NOW, database_url=site_db) == 0


async def test_retention_respects_configured_window(site_db, monkeypatch):
    """SITE_ANALYTICS_RAW_RETENTION_DAYS сжимает окно: 10 дн. → cutoff 2026-06-22."""
    monkeypatch.setattr(get_settings(), "site_analytics_raw_retention_days", 10)
    await _add(
        site_db,
        _ev(_ts(10, day=date(2026, 6, 21)), visitor="e1"),
        _ev(_ts(10, day=date(2026, 6, 23)), visitor="e2"),
    )
    for day in (date(2026, 6, 21), date(2026, 6, 23)):
        await rollup(day, database_url=site_db)

    deleted = await retention(now=NOW, database_url=site_db)

    assert deleted == 1
    assert await _event_days(site_db) == [date(2026, 6, 23)]
