"""T1-4 — dupes-report как fault-isolated шаг nightly (report-only, решение D6).

_write_dupes_report: пересчитывает кросс-платформенные дубли БЕЗ мутаций БД
(find_cross_platform_dupes(write=False)) и пишет артефакт data/reports/dupes-YYYY-MM-DD.txt
для ручной курации Дарьи. Повторный прогон за тот же день перезаписывает тот же файл
(идемпотентность), пустой кейс пишет «(no duplicate groups found)» и не падает.

_dupes_report_nightly_line: fault-isolation по контракту price-drop/site-retention —
сбой шага превращается в 🚨-строку summary, исключение наружу не уходит.

DB — общий `db`-фикстур (in-memory SQLite, подменяет глобальную session factory); отчёты
пишутся в tmp_path через подмену settings.data_dir — никакого data/ в дереве репо.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.db.models import Listing, Platform
from src.db.session import get_session


@pytest.fixture
def reports_dir(tmp_path, monkeypatch):
    """Redirect settings.data_dir to tmp_path; yields the would-be reports directory."""
    from src.config import get_settings

    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    return tmp_path / "reports"


# --- pHash-хелперы (как в tests/test_dedup.py): pHash = 16 hex-символов = 64-битный int.
# Флип N младших битов даёт хэш ровно в N Хэмминга от базы — «то же фото» для группировки.


def _hx(value: int) -> str:
    return format(value & ((1 << 64) - 1), "016x")


BASE_A = 0x0000_0000_0000_0000
BASE_B = 0xFFFF_FFFF_0000_0000
BASE_C = 0x0F0F_0F0F_0F0F_0F0F


def _near(base: int, flip_bits: int) -> str:
    return _hx(base ^ ((1 << flip_bits) - 1))


async def _seed(*, external_id: str, platform: Platform, phashes: list[str]) -> int:
    async with get_session() as s:
        listing = Listing(
            platform=platform,
            external_id=external_id,
            url=f"https://example.com/{platform.value}/{external_id}",
            photo_phashes=phashes,
            photo_urls=[f"u{i}" for i in range(len(phashes))],
            is_active=True,
        )
        s.add(listing)
        await s.flush()
        return listing.id


async def _seed_dupe_pair() -> tuple[int, int]:
    """Два активных объявления с ≥2 общими фото (Hamming ≤ 6) → ровно одна группа."""
    a = await _seed(
        external_id="a", platform=Platform.NEKRETNINE,
        phashes=[_near(BASE_A, 1), _near(BASE_B, 1), _hx(BASE_C)],
    )
    b = await _seed(
        external_id="b", platform=Platform.HALOOGLASI,
        phashes=[_near(BASE_A, 2), _near(BASE_B, 2)],
    )
    return a, b


def _today_name() -> str:
    return f"dupes-{datetime.now(timezone.utc).date().isoformat()}.txt"


# ------------------------------------------------------------------ артефакт


async def test_writes_dated_artifact_with_groups(db, reports_dir):
    from src.cli import _write_dupes_report

    a, b = await _seed_dupe_pair()

    path, stats = await _write_dupes_report()

    assert path == reports_dir / _today_name()
    assert path.is_file()
    assert stats.groups_found == 1
    text = path.read_text(encoding="utf-8")
    assert f"group {min(a, b)}" in text
    assert "★ primary" in text
    # URL каждого участника — Дарье нужно кликать по ним при курации.
    assert f"https://example.com/{Platform.NEKRETNINE.value}/a" in text
    assert f"https://example.com/{Platform.HALOOGLASI.value}/b" in text


async def test_nightly_dupes_write_true_persists_tags(db, reports_dir):
    """D2: по умолчанию (NIGHTLY_DUPES_WRITE=1) nightly-шаг ПИШЕТ dup_group_id/is_primary — liveness
    нужны живые dup-группы, чтобы знать, что квартира ещё жива на другой площадке."""
    from src.cli import _write_dupes_report

    a, b = await _seed_dupe_pair()

    _, stats = await _write_dupes_report()

    assert stats.groups_found == 1
    async with get_session() as s:
        rows = [await s.get(Listing, i) for i in (a, b)]
    gid = min(a, b)
    assert all(r.dup_group_id == gid for r in rows), "both members tagged with the shared group id"
    assert sum(1 for r in rows if r.is_primary) == 1, "exactly one primary per group"


async def test_nightly_dupes_write_false_is_report_only(db, reports_dir, monkeypatch):
    """NIGHTLY_DUPES_WRITE=0 → прежнее поведение: только отчёт, БД не трогаем."""
    from src.cli import _write_dupes_report
    from src.config import get_settings

    monkeypatch.setattr(get_settings(), "nightly_dupes_write", False)
    a, b = await _seed_dupe_pair()

    await _write_dupes_report()

    async with get_session() as s:
        rows = [await s.get(Listing, i) for i in (a, b)]
    assert all(r.dup_group_id is None and r.is_primary is True for r in rows)


async def test_empty_case_writes_report_without_failing(db, reports_dir):
    from src.cli import _write_dupes_report

    path, stats = await _write_dupes_report()

    assert stats.groups_found == 0
    assert path.is_file()
    assert "(no duplicate groups found)" in path.read_text(encoding="utf-8")


async def test_double_run_is_idempotent(db, reports_dir):
    from src.cli import _write_dupes_report

    await _seed_dupe_pair()

    p1, _ = await _write_dupes_report()
    first = p1.read_text(encoding="utf-8")
    p2, _ = await _write_dupes_report()

    assert p1 == p2
    assert list(reports_dir.iterdir()) == [p1]  # перезапись того же файла, не дубли
    assert p2.read_text(encoding="utf-8") == first


# ------------------------------------------------------------------ nightly-строка


async def test_nightly_line_carries_counters_and_report_name(db, reports_dir):
    from src.cli import _dupes_report_nightly_line

    await _seed_dupe_pair()

    line = await _dupes_report_nightly_line()

    assert "групп 1" in line
    assert _today_name() in line
    assert not line.startswith("🚨")


async def test_nightly_line_failure_becomes_alarm_not_exception(monkeypatch):
    """Fault isolation: сбой шага → 🚨-строка в summary, nightly живёт дальше."""
    import src.cli as cli_mod

    async def boom():
        raise RuntimeError("dedup exploded")

    monkeypatch.setattr(cli_mod, "_write_dupes_report", boom)

    line = await cli_mod._dupes_report_nightly_line()

    assert line.startswith("🚨")
    assert "dupes" in line
