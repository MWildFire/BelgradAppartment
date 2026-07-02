"""T2-4 — analytics rollup+retention как fault-isolated шаг nightly (DATA_CONTRACT-T §N).

_analytics_nightly_line: порядок §N — rollup(вчера) → rollup(сегодня) → retention(); каждый
под-шаг изолирован ОТДЕЛЬНО: сбой rollup не отменяет retention (тот безопасен по построению —
гард _rolled), любой сбой становится 🚨-строкой summary, исключение наружу не уходит
(контракт price-drop/site-retention/dupes-report).

Сами агрегаты/удаление проверяет tests/site/test_analytics_rollup.py — здесь только проводка:
rollup/retention подменяются рекордерами через модуль src.site.analytics (cli импортирует
его лениво внутри шага).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta


def _install_recorders(monkeypatch, calls: list, *, rollup_boom=False, retention_boom=False):
    import src.site.analytics as analytics_mod

    async def fake_rollup(day: date, *, database_url: str | None = None) -> int:
        if rollup_boom:
            raise RuntimeError("rollup exploded")
        calls.append(("rollup", day))
        return 1

    async def fake_retention(*, now=None, database_url: str | None = None) -> int:
        if retention_boom:
            raise RuntimeError("retention exploded")
        calls.append(("retention",))
        return 5

    monkeypatch.setattr(analytics_mod, "rollup", fake_rollup)
    monkeypatch.setattr(analytics_mod, "retention", fake_retention)


async def test_nightly_line_rolls_yesterday_today_then_retention(monkeypatch):
    from src.cli import _analytics_nightly_line

    calls: list = []
    _install_recorders(monkeypatch, calls)

    line = await _analytics_nightly_line()

    today = datetime.now(UTC).date()
    yesterday = today - timedelta(days=1)
    assert calls == [("rollup", yesterday), ("rollup", today), ("retention",)]  # порядок §N
    assert "🚨" not in line
    assert yesterday.isoformat() in line and today.isoformat() in line
    assert "5" in line  # счётчик удалённого raw — в summary


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
