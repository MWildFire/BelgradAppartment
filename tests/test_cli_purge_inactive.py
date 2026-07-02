"""T1-3 — `otz purge-inactive` + честный `stats`.

purge-inactive: dry-run считает тот же отчёт, что и боевой прогон, но с НУЛЁМ мутаций
(снапшот-сравнение БД до/после); боевой прогон пишет архив data/archive/listings-YYYYMMDD.jsonl.gz
(полные строки, roundtrip-читаемый) и только потом удаляет. Строки с FK-ссылками из
matches/site_listings удалять ОТКАЗ — они перечисляются в отчёте.

stats: разбивка active/inactive per platform вместо раздутого total.

DB — общий `db`-фикстур (in-memory SQLite, подменяет глобальную session factory); архив
пишется в tmp_path через подмену settings.data_dir — никакого data/ в дереве репо.
"""

from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta, timezone

import pytest
from click.testing import CliRunner
from sqlalchemy import select

from src.cli import PurgeInactiveResult, _purge_inactive, _stats, cli
from src.db.models import (
    InboundPost,
    Listing,
    Match,
    Platform,
    SiteListing,
    SourceChannel,
)
from src.db.session import get_session


@pytest.fixture
def archive_dir(tmp_path, monkeypatch):
    """Redirect settings.data_dir to tmp_path; yields the would-be archive directory."""
    from src.config import get_settings

    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    return tmp_path / "archive"


async def _seed_listing(
    *,
    external_id: str,
    is_active: bool = False,
    last_seen_days_ago: int = 100,
    platform: Platform = Platform.NEKRETNINE,
    **extra,
) -> int:
    extra.setdefault("photo_urls", [])
    extra.setdefault("photo_phashes", [])
    async with get_session() as s:
        listing = Listing(
            platform=platform,
            external_id=external_id,
            url=f"https://example.com/{external_id}/",
            is_active=is_active,
            last_seen_at=datetime.now(timezone.utc) - timedelta(days=last_seen_days_ago),
            **extra,
        )
        s.add(listing)
        await s.flush()
        return listing.id


async def _protect_with_match(listing_id: int, *, on_site: bool = False) -> None:
    """Reference the listing from matches (optionally exposing it via site_listings).

    Полная цепочка source_channel → inbound → match — как в проде, а не висячий FK:
    тестовый движок не включает PRAGMA foreign_keys, полагаться на это нельзя.
    """
    async with get_session() as s:
        chan = SourceChannel(tg_id=100_000 + listing_id, username=f"src{listing_id}")
        s.add(chan)
        await s.flush()
        inbound = InboundPost(source_channel_id=chan.id, tg_message_id=listing_id)
        s.add(inbound)
        await s.flush()
        match = Match(inbound_post_id=inbound.id, listing_id=listing_id)
        s.add(match)
        await s.flush()
        if on_site:
            s.add(SiteListing(match_id=match.id))


async def _db_snapshot() -> tuple:
    """Full-ish DB state for the «dry-run mutates nothing» comparison."""
    async with get_session() as s:
        listings = (
            await s.execute(
                select(
                    Listing.id, Listing.is_active, Listing.url, Listing.last_seen_at
                ).order_by(Listing.id)
            )
        ).all()
        matches = (
            await s.execute(select(Match.id, Match.listing_id).order_by(Match.id))
        ).all()
        sites = (
            await s.execute(select(SiteListing.id, SiteListing.match_id).order_by(SiteListing.id))
        ).all()
    return listings, matches, sites


async def _seed_standard_mix() -> dict[str, int]:
    """5 listings covering every purge decision branch (older_than=30):

    purgeable — inactive, 100 дн, без FK          → архив + delete
    protected — inactive, 100 дн, есть Match       → ОТКАЗ
    on_site   — inactive, 100 дн, Match+SiteListing → ОТКАЗ (и виден на сайте)
    fresh     — inactive, но всего 5 дн            → не кандидат
    active    — старый, но is_active               → не кандидат
    """
    ids = {"purgeable": await _seed_listing(external_id="purgeable")}
    ids["protected"] = await _seed_listing(external_id="protected")
    await _protect_with_match(ids["protected"])
    ids["on_site"] = await _seed_listing(external_id="on-site")
    await _protect_with_match(ids["on_site"], on_site=True)
    ids["fresh"] = await _seed_listing(external_id="fresh", last_seen_days_ago=5)
    ids["active"] = await _seed_listing(external_id="active", is_active=True)
    return ids


# ------------------------------------------------------------------ dry-run


async def test_dry_run_makes_zero_mutations_and_reports(db, archive_dir):
    ids = await _seed_standard_mix()
    before = await _db_snapshot()

    res = await _purge_inactive(older_than_days=30, dry_run=True)

    assert await _db_snapshot() == before, "dry-run must not mutate the DB at all"
    assert not archive_dir.exists(), "dry-run must not write an archive"
    assert res.deleted == 0
    assert res.archive_path is None

    assert res.candidates == 3  # purgeable + protected + on_site
    assert sorted(lid for lid, _, _ in res.protected) == sorted(
        [ids["protected"], ids["on_site"]]
    )
    assert res.protected_on_site == 1
    assert res.to_purge == 1
    assert res.by_platform == {"NEKRETNINE": 1}
    seen_date = (datetime.now(timezone.utc) - timedelta(days=100)).strftime("%Y-%m-%d")
    assert res.by_date == {seen_date: 1}
    assert res.archive_bytes > 0, "dry-run must still report the would-be volume"


# ------------------------------------------------------------------ real run


async def test_real_run_archives_and_deletes_but_refuses_fk(db, archive_dir):
    ids = await _seed_standard_mix()

    res = await _purge_inactive(older_than_days=30, dry_run=False)

    assert res.deleted == 1
    assert sorted(lid for lid, _, _ in res.protected) == sorted(
        [ids["protected"], ids["on_site"]]
    )

    async with get_session() as s:
        left = set((await s.execute(select(Listing.id))).scalars().all())
        n_matches = len((await s.execute(select(Match.id))).scalars().all())
        n_sites = len((await s.execute(select(SiteListing.id))).scalars().all())
    assert left == {ids["protected"], ids["on_site"], ids["fresh"], ids["active"]}
    assert n_matches == 2 and n_sites == 1, "FK-protected graph must stay intact"

    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    assert res.archive_path == archive_dir / f"listings-{today}.jsonl.gz"
    assert res.archive_path.exists()


async def test_real_run_with_nothing_to_purge_writes_no_archive(db, archive_dir):
    await _seed_listing(external_id="alive", is_active=True)

    res = await _purge_inactive(older_than_days=30, dry_run=False)

    assert res.candidates == 0 and res.to_purge == 0 and res.deleted == 0
    assert res.archive_path is None
    assert not archive_dir.exists(), "empty purge set must not leave an empty archive"


# ------------------------------------------------------------------ archive roundtrip


async def test_archive_roundtrip(db, archive_dir):
    """Каждая строка архива читается обратно и восстанавливается в insertable Listing.

    photo_embeddings — единственное намеренное исключение (derived-данные, см. отчёт задачи):
    в записи ключ присутствует, но = None.
    """
    payload = {"source": "list", "n": 7}
    await _seed_listing(
        external_id="rich",
        price_eur=650,
        area_m2=45.5,
        rooms=2.0,
        district="Vračar",
        title="Стан у Врачару — ёлка/ёжик",
        description="двушка, юникод, «кавычки»",
        photo_urls=["https://cdn.example.com/1.jpg", "https://cdn.example.com/2.webp"],
        photo_phashes=["ff00ff00ff00ff00"],
        photo_embeddings=b"\x01\x02\x03",
        raw_payload=payload,
        lat=44.7969,
        lon=20.4573,
    )

    res = await _purge_inactive(older_than_days=30, dry_run=False)

    with gzip.open(res.archive_path, "rt", encoding="utf-8") as fh:
        records = [json.loads(line) for line in fh]
    assert len(records) == 1
    rec = records[0]

    assert rec["platform"] == "NEKRETNINE"
    assert rec["external_id"] == "rich"
    assert rec["price_eur"] == 650
    assert rec["area_m2"] == 45.5
    assert rec["district"] == "Vračar"
    assert rec["title"] == "Стан у Врачару — ёлка/ёжик"
    assert rec["photo_urls"] == ["https://cdn.example.com/1.jpg", "https://cdn.example.com/2.webp"]
    assert rec["photo_phashes"] == ["ff00ff00ff00ff00"]
    assert rec["raw_payload"] == payload
    assert rec["is_active"] is False
    assert rec["photo_embeddings"] is None, "BLOB is excluded by design (derived data)"
    # Все остальные колонки модели присутствуют в записи (полные строки).
    assert set(rec) == {c.name for c in Listing.__table__.columns}

    # Roundtrip: запись → ORM-объект → insert → селект возвращает те же данные.
    rec["platform"] = Platform(rec["platform"])
    for key in ("scraped_at", "last_seen_at", "details_refreshed_at"):
        if rec[key] is not None:
            rec[key] = datetime.fromisoformat(rec[key])
    async with get_session() as s:
        s.add(Listing(**rec))
    async with get_session() as s:
        restored = (
            await s.execute(select(Listing).where(Listing.external_id == "rich"))
        ).scalar_one()
    assert restored.price_eur == 650
    assert restored.raw_payload == payload
    assert restored.photo_urls[1] == "https://cdn.example.com/2.webp"


# ------------------------------------------------------------------ stats breakdown


async def test_stats_reports_active_inactive_breakdown(db, capsys):
    """Per-platform строка показывает active/inactive; раздутый «total» из листингов ушёл."""
    await _seed_listing(external_id="a1", is_active=True)
    await _seed_listing(external_id="a2", is_active=True)
    await _seed_listing(external_id="i1")
    await _seed_listing(external_id="i2")
    await _seed_listing(external_id="i3")

    await _stats()

    out = capsys.readouterr().out
    nek_line = next(
        line for line in out.splitlines() if line.strip().startswith("NEKRETNINE")
    )
    assert "2 active" in nek_line
    assert "3 inactive" in nek_line
    assert "total" not in nek_line


# ------------------------------------------------------------------ CLI wiring


def test_cli_wiring_passes_options_through(monkeypatch):
    captured: dict = {}

    async def fake_purge(*, older_than_days, dry_run):
        captured.update(older_than_days=older_than_days, dry_run=dry_run)
        return PurgeInactiveResult(
            candidates=0, protected=[], protected_on_site=0, to_purge=0,
            by_platform={}, by_date={}, archive_bytes=0, archive_path=None, deleted=0,
        )

    monkeypatch.setattr("src.cli._purge_inactive", fake_purge)
    runner = CliRunner()

    result = runner.invoke(cli, ["purge-inactive", "--older-than", "45", "--dry-run"])
    assert result.exit_code == 0
    assert captured == {"older_than_days": 45, "dry_run": True}

    result = runner.invoke(cli, ["purge-inactive", "--older-than", "180"])
    assert result.exit_code == 0
    assert captured == {"older_than_days": 180, "dry_run": False}


def test_cli_older_than_is_required(monkeypatch):
    """Без явного --older-than команда не должна молча брать какой-то дефолт."""
    called = {"n": 0}

    async def fake_purge(**_kwargs):
        called["n"] += 1

    monkeypatch.setattr("src.cli._purge_inactive", fake_purge)
    runner = CliRunner()
    result = runner.invoke(cli, ["purge-inactive", "--dry-run"])
    assert result.exit_code != 0
    assert called["n"] == 0


def test_cli_rejects_non_positive_older_than(monkeypatch):
    """--older-than <= 0 сдвигает cutoff в будущее — боевой прогон снёс бы ВСЕ inactive-строки
    независимо от возраста. Для необратимой команды это отклоняется на границе CLI."""
    called = {"n": 0}

    async def fake_purge(**_kwargs):
        called["n"] += 1

    monkeypatch.setattr("src.cli._purge_inactive", fake_purge)
    runner = CliRunner()
    for bad in ("0", "-5"):
        result = runner.invoke(cli, ["purge-inactive", "--older-than", bad])
        assert result.exit_code == 2, f"--older-than {bad} должен отклоняться"
    assert called["n"] == 0
