"""Tests for inbound dedup in the monitor.

Wave 1 added the partial unique index `uq_inbound_chan_msg_nodigest`
(source_channel_id, tg_message_id WHERE digest_share_id IS NULL). These tests verify
the monitor's persist logic handles the resulting IntegrityError race gracefully:
one row, one pipeline run — and that digest (non-NULL share) behavior is unchanged.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import src.db.session as db_session
import src.telegram.monitor as monitor
from src.db.models import Base, InboundPost, SourceChannel


@pytest_asyncio.fixture
async def db(tmp_path: Path, monkeypatch):
    """File-backed SQLite with prod pragmas (WAL + busy_timeout) so two concurrent
    sessions race like they do in production — the loser must get IntegrityError,
    not 'database is locked'."""
    from src.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "inbound_photos_dir", tmp_path / "inbound")

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/test.db", echo=False, future=True
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _pragmas(dbapi_conn, _connection_record):  # type: ignore[no-untyped-def]
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=10000")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    orig_engine = db_session._engine
    orig_factory = db_session._session_factory
    db_session._engine = engine
    db_session._session_factory = factory
    try:
        yield factory
    finally:
        await engine.dispose()
        db_session._engine = orig_engine
        db_session._session_factory = orig_factory


class FakeMessage:
    """Minimal stand-in for a Telethon Message with one photo."""

    def __init__(self, mid: int, text: str = "стан у центру 500е"):
        self.id = mid
        self.grouped_id = None
        self.date = datetime.now(timezone.utc)
        self.message = text
        self.photo = object()  # non-None → counts as photo
        self.media = None
        self.entities = None  # no entities → never detected as digest

    async def download_media(self, file: str) -> str:
        Path(file).write_bytes(b"fake-jpeg")
        return file


async def _make_channel(factory, tg_id: int = -100) -> int:
    async with factory() as s:
        ch = SourceChannel(tg_id=tg_id, username="src", is_active=True)
        s.add(ch)
        await s.commit()
        return ch.id


def _patch_heavy_deps():
    """Skip real phash/CLIP — irrelevant to dedup."""
    return (
        patch("src.telegram.monitor.compute_phash_from_path", return_value="f" * 16),
        patch("src.telegram.monitor._compute_clip_blob", new=AsyncMock(return_value=None)),
    )


@pytest.mark.asyncio
async def test_concurrent_same_message_one_row_one_pipeline(db):
    """Two coroutines persist the same (channel, tg_message_id, NULL digest) concurrently:
    exactly one row lands, the pipeline runs exactly once, the loser hits the
    IntegrityError path (not just the pre-check)."""
    factory = db
    chan_id = await _make_channel(factory)
    pipeline = AsyncMock()

    # Spy on the existing-row lookup: 2 pre-checks (both None) + the loser's
    # re-select inside the IntegrityError handler (finds the winner's row).
    real_select = monitor._select_existing_regular
    select_results: list[InboundPost | None] = []

    async def counting_select(s, source_id, tg_message_id):
        res = await real_select(s, source_id, tg_message_id)
        select_results.append(res)
        return res

    # Barrier: each task's FIRST _advance_watermark call (i.e. after its pre-check passed)
    # waits for the other — guarantees both pass the pre-check before either flushes,
    # so the partial unique index is what resolves the race.
    real_advance = monitor._advance_watermark
    barrier = asyncio.Barrier(2)
    arrived: set[asyncio.Task] = set()

    async def synced_advance(s, source_id, last_msg_id):
        task = asyncio.current_task()
        if task not in arrived:
            arrived.add(task)
            await barrier.wait()
        await real_advance(s, source_id, last_msg_id)

    async def persist_and_run_pipeline():
        # Mirrors the live handler contract: pipeline runs per returned post.
        posts = await monitor._persist_inbound(chan_id, [FakeMessage(42)])
        for post in posts:
            await pipeline(post.id)
        return posts

    p1, p2 = _patch_heavy_deps()
    with p1, p2, patch("src.telegram.monitor._advance_watermark", new=synced_advance), \
            patch("src.telegram.monitor._select_existing_regular", new=counting_select):
        results = await asyncio.gather(persist_and_run_pipeline(), persist_and_run_pipeline())

    assert len(arrived) == 2  # both passed the pre-check → race was real
    assert sorted(len(r) for r in results) == [0, 1]
    assert pipeline.await_count == 1
    # Loser took the except path: 2 empty pre-checks, then re-select found the winner's row.
    assert [r is not None for r in select_results] == [False, False, True]

    async with factory() as s:
        rows = (
            await s.execute(
                select(InboundPost).where(
                    InboundPost.source_channel_id == chan_id,
                    InboundPost.tg_message_id == 42,
                )
            )
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].digest_share_id is None
        chan = await s.get(SourceChannel, chan_id)
        assert chan.last_processed_message_id == 42  # loser still advanced the watermark


@pytest.mark.asyncio
async def test_sequential_duplicate_skipped_by_precheck(db):
    """Re-persisting an already-stored message (backfill replay) returns no posts —
    no second pipeline run, watermark still advanced."""
    factory = db
    chan_id = await _make_channel(factory)

    p1, p2 = _patch_heavy_deps()
    with p1, p2:
        first = await monitor._persist_inbound(chan_id, [FakeMessage(7)])
        second = await monitor._persist_inbound(chan_id, [FakeMessage(7)])

    assert len(first) == 1
    assert second == []
    async with factory() as s:
        rows = (await s.execute(select(InboundPost))).scalars().all()
        assert len(rows) == 1
        chan = await s.get(SourceChannel, chan_id)
        assert chan.last_processed_message_id == 7


@pytest.mark.asyncio
async def test_digest_nonnull_share_ids_behave_as_before(db):
    """Non-NULL digest_share_id semantics are untouched by the partial index:
    different shares under one message coexist, exact triple duplicates still fail."""
    factory = db
    chan_id = await _make_channel(factory, tg_id=-200)

    async with factory() as s:
        s.add_all([
            InboundPost(source_channel_id=chan_id, tg_message_id=500, digest_share_id="share-a"),
            InboundPost(source_channel_id=chan_id, tg_message_id=500, digest_share_id="share-b"),
        ])
        await s.commit()

    # A NULL-share row may coexist with digest rows for the same message.
    async with factory() as s:
        s.add(InboundPost(source_channel_id=chan_id, tg_message_id=500, digest_share_id=None))
        await s.commit()

    # Exact triple duplicate → uq_inbound_channel_msg, same as before Wave 1.
    with pytest.raises(IntegrityError):
        async with factory() as s:
            s.add(
                InboundPost(source_channel_id=chan_id, tg_message_id=500, digest_share_id="share-a")
            )
            await s.commit()

    # And a second NULL-share row is now caught by the partial index.
    with pytest.raises(IntegrityError):
        async with factory() as s:
            s.add(InboundPost(source_channel_id=chan_id, tg_message_id=500, digest_share_id=None))
            await s.commit()

    async with factory() as s:
        count = len((await s.execute(select(InboundPost))).scalars().all())
        assert count == 3
