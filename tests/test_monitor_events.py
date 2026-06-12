"""Event-level tests for the Telegram source-channel monitor (W4-T2).

Direct handler invocation with fake Telethon objects — no real connection:
- album grouping by grouped_id (N sibling messages -> ONE inbound with N photos);
- flush-by-timer semantics (partial album + quiet -> flush; interleaved albums -> two inbounds);
- watermark semantics (pinned to ACTUAL code behavior — see SPEC NOTE comments);
- event-level duplicate skip (pre-check path; the IntegrityError race path is
  covered by tests/test_monitor_dedup.py and NOT duplicated here);
- backfill ordering oldest -> newest, watermark ends at max processed id;
- single photo (no grouped_id) -> inbound with 1 photo.

The album flush delay (ALBUM_FLUSH_DELAY = 2.5s) is a closure-local constant inside
register_live_handler and cannot be patched by name. Instead the tests swap the
monitor module's `asyncio` reference for a proxy whose `sleep` blocks on an Event the
test releases ("the quiet period elapsed") — no real sleeps, fully deterministic.

DB: file-backed SQLite with prod pragmas (WAL + busy_timeout), same pattern as
test_monitor_dedup.py — the interleaved-album test runs two flush tasks concurrently,
and an in-memory aiosqlite DB shares a single StaticPool connection across sessions,
which does not survive concurrent sessions the way production does.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import src.db.session as db_session
import src.telegram.monitor as monitor
from src.db.models import Base, InboundPost, InboundStatus, SourceChannel

CHAT_ID = -1001234500000


# --------------------------------------------------------------------------- fixtures


@pytest_asyncio.fixture
async def db(tmp_path: Path, monkeypatch):
    """File-backed SQLite (WAL + busy_timeout) swapped in for the global factory.

    Also patches settings dirs into tmp_path and gives the monitor a fresh
    _pipeline_semaphore: the module-level one loop-binds on first contended await,
    and pytest-asyncio creates a new event loop per test.
    """
    from src.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "inbound_photos_dir", tmp_path / "inbound")
    monkeypatch.setattr(monitor, "_pipeline_semaphore", asyncio.Semaphore(1))

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


@pytest.fixture(autouse=True)
def heavy_deps_stub(monkeypatch):
    """Skip real phash/CLIP — irrelevant to event-level behavior."""
    monkeypatch.setattr(monitor, "compute_phash_from_path", lambda p: "f" * 16)
    monkeypatch.setattr(monitor, "_compute_clip_blob", AsyncMock(return_value=None))


# --------------------------------------------------------------------------- fakes


class FakeMessage:
    """Minimal stand-in for a Telethon Message.

    Attributes are exactly the ones monitor.py touches: id, grouped_id, date,
    message, photo, media, entities, download_media. entities=None means the
    message can never be detected as a digest post.
    """

    def __init__(self, mid: int, *, gid: int | None = None, text: str = "", photo: bool = True):
        self.id = mid
        self.grouped_id = gid
        self.date = datetime(2026, 6, 1, 12, 0, mid % 60, tzinfo=timezone.utc)
        self.message = text
        self.photo = object() if photo else None  # non-None -> counts as photo
        self.media = None
        self.entities = None
        self.download_calls: list[str] = []

    async def download_media(self, file: str) -> str:
        Path(file).write_bytes(b"fake-jpeg")
        self.download_calls.append(file)
        return file


def _event_for(msg: FakeMessage, chat_id: int = CHAT_ID) -> SimpleNamespace:
    """Fake NewMessage event: the handler reads only .message and .chat_id."""
    return SimpleNamespace(message=msg, chat_id=chat_id)


class FakeLiveClient:
    """Captures the handler that register_live_handler wires via @client.on(...)."""

    def __init__(self) -> None:
        self.handlers: list = []

    def on(self, _event_builder):
        def deco(fn):
            self.handlers.append(fn)
            return fn

        return deco


class ControlledAsyncio:
    """Stand-in for the monitor module's `asyncio` global.

    `sleep` (the album flush timer) blocks on `quiet` until the test releases it;
    `create_task` records the flush tasks so the test can await their completion.
    Everything else proxies to the real asyncio module.
    """

    def __init__(self) -> None:
        self.quiet = asyncio.Event()
        self.sleep_delays: list[float] = []
        self.tasks: list[asyncio.Task] = []

    def __getattr__(self, name):
        return getattr(asyncio, name)

    async def sleep(self, delay: float) -> None:
        self.sleep_delays.append(delay)
        await self.quiet.wait()

    def create_task(self, coro) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        self.tasks.append(task)
        return task

    async def elapse_quiet_period(self, timeout: float = 5.0) -> None:
        """Simulate the album quiet period elapsing; wait for all flush tasks."""
        self.quiet.set()
        if self.tasks:
            await asyncio.wait_for(asyncio.gather(*self.tasks), timeout)


class FakeBackfillClient:
    """Stand-in for TelegramClient in backfill: canned history + recorded kwargs.

    Messages are supplied oldest -> newest, matching what iter_messages yields
    under reverse=True. min_id filtering is simulated like Telethon's.
    """

    def __init__(self, messages: list[FakeMessage]) -> None:
        self._messages = list(messages)
        self.iter_kwargs: dict | None = None
        self.entity_requested = None

    async def get_entity(self, identifier):
        self.entity_requested = identifier
        return object()

    def iter_messages(self, _entity, **kwargs):
        self.iter_kwargs = kwargs

        async def _gen():
            min_id = kwargs.get("min_id") or 0
            for m in self._messages:
                if m.id > min_id:
                    yield m

        return _gen()


# --------------------------------------------------------------------------- helpers


async def _make_channel(factory, **kw) -> SourceChannel:
    async with factory() as s:
        ch = SourceChannel(tg_id=CHAT_ID, username="src", is_active=True, **kw)
        s.add(ch)
        await s.commit()
        return ch


def _recording_callback():
    """on_inbound stub recording every inbound id it is called with."""
    calls: list[int] = []

    async def cb(inbound_id: int) -> None:
        calls.append(inbound_id)

    return cb, calls


def _wire_live(channel: SourceChannel, on_inbound, monkeypatch):
    """Register the live handler with controlled time; return (handler, ctl)."""
    ctl = ControlledAsyncio()
    monkeypatch.setattr(monitor, "asyncio", ctl)
    client = FakeLiveClient()
    monitor.register_live_handler(client, [channel], on_inbound)
    assert len(client.handlers) == 1
    return client.handlers[0], ctl


async def _rows(factory) -> list[InboundPost]:
    async with factory() as s:
        return (await s.execute(select(InboundPost).order_by(InboundPost.id))).scalars().all()


async def _watermark(factory, chan_id: int) -> int | None:
    async with factory() as s:
        chan = await s.get(SourceChannel, chan_id)
        return chan.last_processed_message_id


# --------------------------------------------------------------------------- live: singles


async def test_live_single_photo_no_grouped_id_one_inbound_one_photo(db, monkeypatch):
    """Standalone photo message (grouped_id=None) -> one inbound with exactly 1 photo,
    persisted synchronously in the handler (no album timer), pipeline called once,
    watermark at the message id."""
    factory = db
    chan = await _make_channel(factory)
    cb, calls = _recording_callback()
    handler, ctl = _wire_live(chan, cb, monkeypatch)

    msg = FakeMessage(10, text="однокомнатная, Дорчол, 450е")
    await handler(_event_for(msg))

    rows = await _rows(factory)
    assert len(rows) == 1
    post = rows[0]
    assert post.tg_message_id == 10
    assert post.source_channel_id == chan.id
    assert len(post.photo_paths) == 1
    assert len(post.photo_phashes) == 1
    assert post.raw_text == "однокомнатная, Дорчол, 450е"
    assert post.status == InboundStatus.NEW
    assert calls == [post.id]
    assert await _watermark(factory, chan.id) == 10
    # standalone path never arms the album timer
    assert ctl.tasks == [] and ctl.sleep_delays == []


async def test_live_event_filtering_text_only_and_foreign_chat_ignored(db, monkeypatch):
    """Text-only standalone messages and events from unknown chats are dropped:
    nothing persisted, no pipeline call, watermark untouched."""
    factory = db
    chan = await _make_channel(factory)
    cb, calls = _recording_callback()
    handler, _ctl = _wire_live(chan, cb, monkeypatch)

    await handler(_event_for(FakeMessage(11, text="сдается, фото в комментариях", photo=False)))
    await handler(_event_for(FakeMessage(12), chat_id=-999))  # not a registered source

    assert await _rows(factory) == []
    assert calls == []
    assert await _watermark(factory, chan.id) is None


def test_register_live_handler_without_sources_registers_nothing():
    """Empty source list -> early return, no handler wired."""
    client = FakeLiveClient()
    monitor.register_live_handler(client, [], None)
    assert client.handlers == []


# --------------------------------------------------------------------------- live: albums


async def test_live_album_grouped_into_one_inbound_with_n_photos(db, monkeypatch):
    """3 sibling messages with the same grouped_id -> ONE inbound with 3 photos.
    One flush timer armed (by the first sibling only); nothing persists until the
    quiet period elapses; leader id is the smallest message id; watermark at max."""
    factory = db
    chan = await _make_channel(factory)
    cb, calls = _recording_callback()
    handler, ctl = _wire_live(chan, cb, monkeypatch)

    gid = 777
    msgs = [
        FakeMessage(21, gid=gid, text="3-комн, Врачар, 800е"),  # caption on leader only
        FakeMessage(22, gid=gid),
        FakeMessage(23, gid=gid),
    ]
    for m in msgs:
        await handler(_event_for(m))

    # exactly one timer per grouped_id, armed by the first sibling
    assert len(ctl.tasks) == 1
    # let the flush task start and block on its (controlled) sleep
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert len(ctl.sleep_delays) == 1 and ctl.sleep_delays[0] > 0
    # buffered, not yet persisted: the album waits for the quiet period
    assert await _rows(factory) == []
    assert calls == []

    await ctl.elapse_quiet_period()

    rows = await _rows(factory)
    assert len(rows) == 1
    post = rows[0]
    assert post.tg_message_id == 21  # leader = smallest sibling id
    assert len(post.photo_paths) == 3
    assert len(post.photo_phashes) == 3
    assert post.raw_text == "3-комн, Врачар, 800е"
    assert calls == [post.id]
    assert await _watermark(factory, chan.id) == 23


async def test_live_partial_album_flushes_after_quiet_period(db, monkeypatch):
    """The monitor has no notion of an album's expected size — the quiet timer IS the
    only completion signal. Two siblings arrive, then quiet -> flushed as one inbound
    with the 2 photos that made it."""
    factory = db
    chan = await _make_channel(factory)
    cb, calls = _recording_callback()
    handler, ctl = _wire_live(chan, cb, monkeypatch)

    await handler(_event_for(FakeMessage(31, gid=888, text="2-комн")))
    await handler(_event_for(FakeMessage(32, gid=888)))
    await ctl.elapse_quiet_period()

    rows = await _rows(factory)
    assert len(rows) == 1
    assert rows[0].tg_message_id == 31
    assert len(rows[0].photo_paths) == 2
    assert calls == [rows[0].id]
    assert await _watermark(factory, chan.id) == 32


async def test_live_interleaved_albums_produce_two_inbounds(db, monkeypatch):
    """Two albums whose siblings interleave on the wire (A1 B1 A2 B2) are kept apart
    by grouped_id: two timers, two inbounds, each with its own photos; watermark ends
    at the max id across both."""
    factory = db
    chan = await _make_channel(factory)
    cb, calls = _recording_callback()
    handler, ctl = _wire_live(chan, cb, monkeypatch)

    a, b = 1000, 2000
    await handler(_event_for(FakeMessage(41, gid=a, text="album A")))
    await handler(_event_for(FakeMessage(42, gid=b, text="album B")))
    await handler(_event_for(FakeMessage(43, gid=a)))
    await handler(_event_for(FakeMessage(44, gid=b)))

    assert len(ctl.tasks) == 2  # one flush timer per grouped_id
    await ctl.elapse_quiet_period()

    rows = await _rows(factory)
    assert len(rows) == 2
    by_leader = {r.tg_message_id: r for r in rows}
    assert set(by_leader) == {41, 42}
    assert len(by_leader[41].photo_paths) == 2
    assert by_leader[41].raw_text == "album A"
    assert len(by_leader[42].photo_paths) == 2
    assert by_leader[42].raw_text == "album B"
    assert sorted(calls) == sorted(r.id for r in rows)  # pipeline once per inbound
    assert await _watermark(factory, chan.id) == 44


# --------------------------------------------------------------------------- live: dedup + watermark


async def test_live_duplicate_message_id_skipped_by_precheck(db, monkeypatch):
    """The same standalone message delivered twice (event-level pre-check path):
    one row, one pipeline call. The IntegrityError race path is covered by
    test_monitor_dedup.py and intentionally not duplicated here."""
    factory = db
    chan = await _make_channel(factory)
    cb, calls = _recording_callback()
    handler, _ctl = _wire_live(chan, cb, monkeypatch)

    msg = FakeMessage(50, text="дубль")
    await handler(_event_for(msg))
    await handler(_event_for(FakeMessage(50, text="дубль")))  # redelivery of same id

    rows = await _rows(factory)
    assert len(rows) == 1
    assert rows[0].tg_message_id == 50
    assert calls == [rows[0].id]  # pipeline ran exactly once
    assert await _watermark(factory, chan.id) == 50


async def test_live_pipeline_failure_does_not_roll_back_watermark(db, monkeypatch):
    """SPEC NOTE (W4-T2): the task list's ideal is "pipeline callback raises ->
    watermark NOT advanced past the failed message". ACTUAL behavior differs and is
    pinned here: the watermark advances inside _persist_inbound, in the same
    transaction as the InboundPost insert and BEFORE on_inbound ever runs; a callback
    failure is caught and only logged. The durable retry handle is the persisted
    InboundPost row itself (status=NEW), not the watermark — a failed pipeline run is
    never re-driven by message replay."""
    factory = db
    chan = await _make_channel(factory)

    failing_calls: list[int] = []

    async def failing_cb(inbound_id: int) -> None:
        failing_calls.append(inbound_id)
        raise RuntimeError("pipeline boom")

    handler, _ctl = _wire_live(chan, failing_cb, monkeypatch)

    # Does not raise: the handler swallows (logs) on_inbound failures.
    await handler(_event_for(FakeMessage(60, text="упавший пайплайн")))

    rows = await _rows(factory)
    assert len(rows) == 1
    assert rows[0].status == InboundStatus.NEW  # row remains as the retry handle
    assert failing_calls == [rows[0].id]
    # Watermark advanced despite the pipeline failure — pinned actual behavior.
    assert await _watermark(factory, chan.id) == 60


# --------------------------------------------------------------------------- backfill


async def test_backfill_processes_oldest_to_newest_watermark_at_max(db):
    """History replays in chronological order: singles and albums interleaved.
    Inserts happen oldest -> newest, on_inbound fires in that order, photo-less
    messages are skipped, and the watermark ends at the max processed id."""
    factory = db
    chan = await _make_channel(
        factory, start_date=datetime(2026, 1, 1, tzinfo=timezone.utc)
    )
    cb, calls = _recording_callback()

    history = [
        FakeMessage(1, text="single old"),
        FakeMessage(2, gid=900, text="album lead"),
        FakeMessage(3, gid=900),
        FakeMessage(4, text="text only — skipped", photo=False),
        FakeMessage(5, text="single new"),
    ]
    client = FakeBackfillClient(history)

    stats = await monitor.backfill(client, chan, on_inbound=cb)

    # First run, no watermark -> starts from the channel's start_date, oldest first.
    assert client.iter_kwargs == {"reverse": True, "offset_date": chan.start_date}
    assert stats.processed == 3
    assert stats.skipped_no_photo == 1
    assert stats.posts_inserted == 3

    rows = await _rows(factory)  # ordered by autoincrement id == insertion order
    assert [r.tg_message_id for r in rows] == [1, 2, 5]  # oldest -> newest
    assert calls == [r.id for r in rows]  # callback order matches
    album = rows[1]
    assert len(album.photo_paths) == 2  # 2-photo album collapsed into one inbound
    assert album.raw_text == "album lead"
    assert await _watermark(factory, chan.id) == 5


async def test_backfill_resumes_from_watermark_min_id(db):
    """A channel with a watermark replays strictly after it (min_id), not from
    start_date; only newer messages are persisted and the watermark moves to max."""
    factory = db
    chan = await _make_channel(
        factory,
        start_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        last_processed_message_id=2,
    )
    client = FakeBackfillClient([FakeMessage(i, text=f"m{i}") for i in (1, 2, 3, 4)])

    stats = await monitor.backfill(client, chan)

    assert client.iter_kwargs == {"reverse": True, "min_id": 2}  # watermark wins over start_date
    assert stats.posts_inserted == 2
    assert [r.tg_message_id for r in await _rows(factory)] == [3, 4]
    assert await _watermark(factory, chan.id) == 4


async def test_backfill_skipped_no_photo_does_not_advance_watermark(db):
    """Pinned actual behavior: photo-less (non-digest) messages are skipped WITHOUT
    advancing the watermark. A trailing text-only message therefore gets re-scanned
    by every subsequent backfill (idempotent thanks to dedup, but the watermark
    permanently lags behind it)."""
    factory = db
    chan = await _make_channel(factory)
    client = FakeBackfillClient(
        [FakeMessage(1, text="photo post"), FakeMessage(2, text="text tail", photo=False)]
    )

    stats = await monitor.backfill(client, chan)

    assert stats.processed == 1
    assert stats.skipped_no_photo == 1
    assert await _watermark(factory, chan.id) == 1  # not 2


async def test_backfill_persist_failure_stops_watermark_at_last_success(db, monkeypatch):
    """If persistence itself fails mid-backfill, the exception propagates and the
    watermark stays at the last successfully persisted message — the failed message
    (and everything after) is replayed on the next run. This is the failure mode the
    watermark actually guards (persistence, not pipeline delivery)."""
    factory = db
    chan = await _make_channel(factory)
    cb, calls = _recording_callback()

    real_persist = monitor._persist_inbound

    async def failing_persist(source_id: int, msgs):
        if msgs[0].id == 2:
            raise RuntimeError("db exploded")
        return await real_persist(source_id, msgs)

    monkeypatch.setattr(monitor, "_persist_inbound", failing_persist)
    client = FakeBackfillClient([FakeMessage(i, text=f"m{i}") for i in (1, 2, 3)])

    with pytest.raises(RuntimeError, match="db exploded"):
        await monitor.backfill(client, chan, on_inbound=cb)

    rows = await _rows(factory)
    assert [r.tg_message_id for r in rows] == [1]
    assert calls == [rows[0].id]
    assert await _watermark(factory, chan.id) == 1  # not advanced past the failure


async def test_backfill_on_inbound_failure_continues_and_advances_watermark(db):
    """SPEC NOTE (W4-T2): same discrepancy as the live path — backfill wraps
    on_inbound in try/except, so a pipeline failure on an older message does NOT stop
    the run and does NOT hold the watermark back. Both rows persist, both callbacks
    are attempted, watermark ends at max. Pinned actual behavior; retry of the failed
    pipeline run relies on the InboundPost row (status=NEW), not on message replay."""
    factory = db
    chan = await _make_channel(factory)

    attempted: list[int] = []

    async def cb(inbound_id: int) -> None:
        attempted.append(inbound_id)
        if len(attempted) == 1:
            raise RuntimeError("pipeline boom on first post")

    client = FakeBackfillClient([FakeMessage(1, text="first"), FakeMessage(2, text="second")])

    stats = await monitor.backfill(client, chan, on_inbound=cb)  # does not raise

    rows = await _rows(factory)
    assert [r.tg_message_id for r in rows] == [1, 2]
    assert attempted == [r.id for r in rows]  # second callback still attempted
    assert stats.posts_inserted == 2
    assert await _watermark(factory, chan.id) == 2
