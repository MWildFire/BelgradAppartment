"""T2-4 — analytics rollup+retention как fault-isolated шаг nightly (DATA_CONTRACT-T §N).

_analytics_nightly_line: план дней — unrolled_days() (raw без маркера _rolled) ПЛЮС «сегодня»:
пропущенные ночи самоизлечиваются следующим прогоном, «сегодня» получает партиал-агрегаты
без маркера (rollup решает по инжектированному today). Каждый под-шаг изолирован ОТДЕЛЬНО:
сбой rollup не отменяет retention (тот безопасен по построению — гард _rolled), любой сбой
становится 🚨-строкой summary, исключение наружу не уходит (контракт price-drop/
site-retention/dupes-report). Kill-switch SITE_ANALYTICS_ENABLED=false глушит шаг целиком.

Сами агрегаты/удаление проверяет tests/site/test_analytics_rollup.py — здесь только проводка:
rollup/retention/unrolled_days подменяются рекордерами через модуль src.site.analytics
(cli импортирует его лениво внутри шага). Дата фиксируется через src.cli._utc_today —
одно чтение wall-clock, прогон через UTC-полночь не даёт ложных красных.
"""

from __future__ import annotations

from datetime import date, timedelta

from src.config import get_settings

# Замороженное «сегодня» шага — тесты не читают wall-clock вовсе.
TODAY = date(2026, 7, 2)


def _install_recorders(
    monkeypatch,
    calls: list,
    *,
    pending: tuple[date, ...] = (TODAY - timedelta(days=1),),
    rollup_boom=False,
    retention_boom=False,
):
    import src.cli as cli_mod
    import src.site.analytics as analytics_mod

    monkeypatch.setattr(cli_mod, "_utc_today", lambda: TODAY)

    async def fake_unrolled_days(*, database_url: str | None = None) -> list[date]:
        calls.append(("unrolled_days",))
        return list(pending)

    async def fake_rollup(day: date, *, database_url: str | None = None, today=None) -> int:
        if rollup_boom:
            raise RuntimeError("rollup exploded")
        calls.append(("rollup", day, today))
        return 1

    async def fake_retention(*, now=None, database_url: str | None = None) -> int:
        if retention_boom:
            raise RuntimeError("retention exploded")
        calls.append(("retention",))
        return 5

    monkeypatch.setattr(analytics_mod, "unrolled_days", fake_unrolled_days)
    monkeypatch.setattr(analytics_mod, "rollup", fake_rollup)
    monkeypatch.setattr(analytics_mod, "retention", fake_retention)


async def test_nightly_line_rolls_pending_days_plus_today_then_retention(monkeypatch):
    """Догон §N: гэп в 3 дня → следующий прогон роллапит ВСЕ пропущенные дни + сегодня."""
    from src.cli import _analytics_nightly_line

    gap = tuple(TODAY - timedelta(days=n) for n in (3, 2, 1))
    calls: list = []
    _install_recorders(monkeypatch, calls, pending=gap)

    line = await _analytics_nightly_line()

    # порядок: план догона → rollup по дням (везде одно и то же «сегодня») → retention
    assert calls == [
        ("unrolled_days",),
        *[("rollup", d, TODAY) for d in gap],
        ("rollup", TODAY, TODAY),
        ("retention",),
    ]
    assert "🚨" not in line
    for d in (*gap, TODAY):
        assert d.isoformat() in line
    assert "5" in line  # счётчик удалённого raw — в summary


async def test_nightly_line_today_not_duplicated_when_already_pending(monkeypatch):
    """«Сегодня» уже в плане догона (события есть, маркера нет) — второй rollup не нужен."""
    from src.cli import _analytics_nightly_line

    yesterday = TODAY - timedelta(days=1)
    calls: list = []
    _install_recorders(monkeypatch, calls, pending=(yesterday, TODAY))

    await _analytics_nightly_line()

    rolled = [c[1] for c in calls if c[0] == "rollup"]
    assert rolled == [yesterday, TODAY]  # каждый день ровно один раз


async def test_rollup_failure_is_alarm_and_retention_still_runs(monkeypatch):
    """Изоляция под-шагов: retention безопасен без свежего rollup (гард _rolled) — гоняем всегда."""
    from src.cli import _analytics_nightly_line

    calls: list = []
    _install_recorders(monkeypatch, calls, rollup_boom=True)

    line = await _analytics_nightly_line()

    assert ("retention",) in calls
    assert "🚨" in line and "rollup" in line


async def test_retention_failure_is_alarm_not_exception(monkeypatch):
    from src.cli import _analytics_nightly_line

    calls: list = []
    _install_recorders(monkeypatch, calls, retention_boom=True)

    line = await _analytics_nightly_line()  # исключение наружу не уходит

    assert len([c for c in calls if c[0] == "rollup"]) == 2  # rollup-часть отработала и отчиталась
    assert "🚨" in line and "retention" in line


async def test_kill_switch_silences_rollup_and_retention(monkeypatch):
    """SITE_ANALYTICS_ENABLED=false — ПОЛНЫЙ kill-switch (§ rollback): nightly не пишет
    маркеры, не ротирует raw; в summary — честная строка «off», а не тихий пропуск."""
    from src.cli import _analytics_nightly_line

    calls: list = []
    _install_recorders(monkeypatch, calls)
    monkeypatch.setattr(get_settings(), "site_analytics_enabled", False)

    line = await _analytics_nightly_line()

    assert calls == []  # ни rollup, ни retention, ни плана догона
    assert "off" in line and "🚨" not in line
