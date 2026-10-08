"""Free synthetic regressions for the 12-row production incident and scheduler races."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from src.config import get_settings
from src.db import InboundPost, InboundStatus, SourceChannel, get_session
from src.db.models import ValidationJob

REVIEW_CASES = [
    (835, "candidate_limit"),
    (836, "multiple_identities"),
    (840, "candidate_limit"),
    (841, "multiple_identities"),
    (842, "candidate_limit"),
    (844, "candidate_limit"),
    (850, "multiple_identities"),
    (855, "candidate_limit"),
    (863, "candidate_limit"),
    (864, "candidate_limit"),
    (867, "multiple_identities"),
    (884, "multiple_identities"),
]


async def seed_review_cases() -> None:
    async with get_session() as session:
        source = SourceChannel(tg_id=-100, username="synthetic", is_active=True)
        session.add(source)
        await session.flush()
        for inbound_id, _reason in REVIEW_CASES:
            session.add(
                InboundPost(
                    id=inbound_id,
                    source_channel_id=source.id,
                    tg_message_id=inbound_id,
                    status=InboundStatus.NEW,
                )
            )
        await session.flush()
        session.add_all(
            [
                ValidationJob(
                    inbound_id=inbound_id,
                    state="needs_review",
                    reason_code=reason,
                    attempts=1,
                    outcomes=[{"listing_id": 1, "verdict": "different"}],
                )
                for inbound_id, reason in REVIEW_CASES
            ]
        )


async def test_monitor_12_review_posts_never_enters_pipeline(db, monkeypatch):
    """This fails on f477601: every manual row is selected and advertised as stranded."""
    from src.cli import _monitor
    from src.jobs import pipeline, validation_retry
    from src.telegram import delivery, monitor

    await seed_review_cases()
    client = MagicMock()
    client.run_until_disconnected = AsyncMock()
    client.disconnect = AsyncMock()
    monkeypatch.setattr(monitor, "make_client", lambda: client)
    monkeypatch.setattr(monitor, "connect_authorized", AsyncMock())
    monkeypatch.setattr(monitor, "register_live_handler", MagicMock())
    monkeypatch.setattr(
        monitor,
        "backfill",
        AsyncMock(
            return_value=MagicMock(
                posts_inserted=0,
                skipped_no_photo=0,
            )
        ),
    )
    process = AsyncMock()
    start = AsyncMock()
    summary = AsyncMock()
    monkeypatch.setattr(pipeline, "process_new_inbound", process)
    monkeypatch.setattr(validation_retry, "retry_loop", AsyncMock())
    monkeypatch.setattr(delivery, "send_text_to_recipients", start)
    monkeypatch.setattr(delivery, "send_run_summary", summary)
    await _monitor(do_backfill_first=True)
    process.assert_not_awaited()
    start.assert_not_awaited()
    assert summary.await_args.kwargs["selected"] == 0
    assert summary.await_args.kwargs["needs_review"] == 12
    assert summary.await_args.kwargs["matched"] == 0
    assert summary.await_args.kwargs["no_match"] == 0
    client.disconnect.assert_awaited_once()


async def test_12_case_recovery_retry_and_refresh_remain_blocked(db, monkeypatch):
    from src.jobs import pipeline, validation_retry
    from src.jobs.recovery import recover_new_posts
    from src.matching import matcher
    from src.matching.validation_queue import automatic_inbound_ids

    await seed_review_cases()
    refresh = AsyncMock(side_effect=AssertionError("manual work must not refresh sources"))
    provider = AsyncMock(side_effect=AssertionError("manual work must not call a provider"))
    delivery = AsyncMock(side_effect=AssertionError("manual work must not deliver"))
    monkeypatch.setattr(pipeline, "scrape_all_with_throttle", refresh)
    monkeypatch.setattr(matcher, "_run_llm_validation", provider)
    monkeypatch.setattr(pipeline, "send_match_to_saved", delivery)
    monkeypatch.setattr(pipeline, "notify_no_match", delivery)
    monkeypatch.setattr(validation_retry, "retry_pending", AsyncMock())
    callback, start, summary = AsyncMock(), AsyncMock(), AsyncMock()
    for _restart in range(2):
        assert await automatic_inbound_ids() == []
        stats = await recover_new_posts(callback, notify_start=start, notify_summary=summary)
        assert stats == dict(
            selected=0, matched=0, no_match=0, needs_review=12, pending=0, unresolved=0, errors=0
        )
        await validation_retry.retry_tick(callback)
    monkeypatch.setattr(
        matcher,
        "load_candidate_cache",
        AsyncMock(
            side_effect=AssertionError("manual work must not load a candidate snapshot"),
        ),
    )
    assert await matcher.match_all_pending() == []
    for inbound_id, _reason in REVIEW_CASES:
        assert (await pipeline.process_new_inbound(inbound_id)).status == "validation_pending"
    for mocked in [callback, start, refresh, provider, delivery]:
        mocked.assert_not_called()
    async with get_session() as session:
        jobs = list(
            (
                await session.execute(
                    select(ValidationJob).order_by(
                        ValidationJob.inbound_id,
                    )
                )
            ).scalars()
        )
        assert [(job.inbound_id, job.reason_code) for job in jobs] == REVIEW_CASES
        assert all(
            job.state == "needs_review"
            and job.attempts == 1
            and job.lease_until is None
            and job.next_retry_at is None
            and job.outcomes == [{"listing_id": 1, "verdict": "different"}]
            for job in jobs
        )


async def test_shared_selection_and_final_counts_cover_deferred_races_and_errors(db, monkeypatch):
    from src.jobs import validation_retry
    from src.jobs.recovery import recover_new_posts
    from src.matching import validation_jobs
    from src.matching.validation_queue import automatic_inbound_ids, may_refresh

    now = datetime.now(UTC)
    async with get_session() as session:
        session.add_all(
            [
                SourceChannel(id=1, tg_id=-1, username="active", is_active=True),
                SourceChannel(id=2, tg_id=-2, username="inactive", is_active=False),
            ]
        )
        await session.flush()
        for i in range(1, 12):
            session.add(
                InboundPost(
                    id=i,
                    source_channel_id=2 if i == 8 else 1,
                    tg_message_id=i,
                    received_at=now,
                    status=(InboundStatus.NO_MATCH if i == 11 else InboundStatus.NEW),
                )
            )
        await session.flush()
        session.add_all(
            [
                ValidationJob(
                    inbound_id=2, state="pending", next_retry_at=now - timedelta(seconds=1)
                ),
                ValidationJob(
                    inbound_id=3, state="running", lease_until=now - timedelta(seconds=1)
                ),
                ValidationJob(inbound_id=4, state="pending", attempts=5),
                ValidationJob(inbound_id=5, state="needs_review", attempts=1),
                ValidationJob(
                    inbound_id=6, state="pending", next_retry_at=now + timedelta(hours=1)
                ),
                ValidationJob(inbound_id=7, state="running", lease_until=now + timedelta(hours=1)),
                ValidationJob(inbound_id=9, state="done"),
            ]
        )
    assert await automatic_inbound_ids() == [1, 2, 3, 4, 10]
    assert await automatic_inbound_ids(limit=2) == [1, 2]
    assert await may_refresh(1)
    assert not await may_refresh(4)
    monkeypatch.setattr(validation_retry, "retry_pending", AsyncMock())
    retried = AsyncMock()
    await validation_retry.retry_tick(retried)
    assert [call.args[0] for call in retried.await_args_list] == [1, 2, 3, 4, 10]

    async def callback(inbound_id):
        if inbound_id == 4:
            assert await validation_jobs.claim(inbound_id) is None
            return
        async with get_session() as session:
            post = await session.get(InboundPost, inbound_id)
            if inbound_id == 1:
                post.status = InboundStatus.MATCHED
            elif inbound_id == 2:
                post.status = InboundStatus.NO_MATCH
            elif inbound_id == 3:  # became manual between selection and claim
                (await session.get(ValidationJob, inbound_id)).state = "needs_review"
            elif inbound_id == 10:  # disappeared during the callback
                await session.delete(post)
        if inbound_id == 1:
            raise RuntimeError("synthetic exception after commit")

    start, summary = AsyncMock(), AsyncMock()
    stats = await recover_new_posts(callback, notify_start=start, notify_summary=summary)
    assert stats == dict(
        selected=5, matched=1, no_match=1, needs_review=3, pending=4, unresolved=1, errors=1
    )
    assert (
        sum(stats[key] for key in ["matched", "no_match", "needs_review", "pending", "unresolved"])
        == 10
    )
    assert summary.await_args.kwargs["errors"] == 1
    assert await automatic_inbound_ids() == []


async def test_recovery_continues_after_notification_failure_and_empty_is_quiet(db):
    from src.jobs.recovery import recover_new_posts

    callback = AsyncMock()
    start, summary = AsyncMock(), AsyncMock()
    await recover_new_posts(callback, notify_start=start, notify_summary=summary)
    start.assert_not_awaited()
    summary.assert_not_awaited()
    await seed_review_cases()
    summary.side_effect = RuntimeError("synthetic notify failure")
    stats = await recover_new_posts(callback, notify_start=start, notify_summary=summary)
    assert stats["needs_review"] == 12


@pytest.mark.parametrize("mode", ["bot", "userbot"])
async def test_both_delivery_modes_render_manual_queue_truthfully(monkeypatch, mode):
    from src.telegram import bot_delivery, delivery

    monkeypatch.setattr(get_settings(), "tg_delivery_mode", mode)
    client = MagicMock(send_message=AsyncMock())
    bot = MagicMock(send_message=AsyncMock(), session=MagicMock(close=AsyncMock()))
    monkeypatch.setattr(delivery, "_resolve_delivery_target", AsyncMock(return_value="synthetic"))
    monkeypatch.setattr(bot_delivery, "_make_bot", lambda: bot)
    monkeypatch.setattr(bot_delivery, "_recipients", lambda: [1, 2])
    await delivery.send_run_summary(client, matched=0, no_match=0, selected=0, needs_review=12)
    text = (
        bot.send_message.await_args.kwargs["text"]
        if mode == "bot"
        else client.send_message.await_args.args[1]
    )
    assert "требует внимания" in text and "завершён" not in text
    assert "Обработано: 0" in text and "решения человека: 12" in text
    assert "Выбрано автоматически: 0" in text
    if mode == "bot":
        assert bot.send_message.await_count == 2
        bot.session.close.assert_awaited_once()


@pytest.mark.parametrize("remaining", [dict(pending=1), dict(unresolved=1), dict(errors=1)])
def test_summary_never_counts_errors_as_success_or_unresolved_as_complete(remaining):
    from src.telegram.run_summary import format_run_summary

    text = format_run_summary(matched=1, no_match=2, **remaining)
    assert "Обработано: 3" in text and "требует внимания" in text
    assert "завершён" not in text


@pytest.mark.parametrize(
    "seconds, expected", [(1, "1 сек"), (61, "1 мин 1 сек"), (3601, "1 ч 0 мин")]
)
def test_terminal_summary_is_complete_with_existing_duration_formats(seconds, expected):
    from src.telegram.run_summary import format_run_summary

    text = format_run_summary(matched=1, no_match=2, duration_seconds=seconds)
    assert "завершён" in text and "Обработано: 3" in text and expected in text


async def test_daily_summary_includes_older_manual_backlog_and_does_not_double_count(
    db, monkeypatch
):
    import src.cli as cli
    from src.db.models import Listing, Match, Platform
    from src.telegram import delivery

    await seed_review_cases()
    async with get_session() as session:
        for inbound_id, _reason in REVIEW_CASES:
            (await session.get(InboundPost, inbound_id)).post_date = datetime.now(UTC) - timedelta(
                days=3
            )
        session.add(
            Listing(
                id=1,
                external_id="synthetic",
                platform=Platform.NEKRETNINE,
                url="https://example.test/1",
            )
        )
        session.add(
            InboundPost(
                id=1000,
                source_channel_id=1,
                tg_message_id=1000,
                post_date=datetime.now(UTC),
                status=InboundStatus.MATCHED,
            )
        )
        await session.flush()
        session.add(Match(inbound_post_id=1000, listing_id=1))  # not delivered yet
    send = AsyncMock()
    monkeypatch.setattr(cli, "_open_userbot_if_needed", AsyncMock(return_value=None))
    monkeypatch.setattr(cli, "_photo_coverage_by_platform", AsyncMock(return_value=[]))
    monkeypatch.setattr(delivery, "send_text_to_recipients", send)
    await cli._daily_summary(24)
    text = send.await_args.args[1]
    assert "Обработано: 1" in text and "Ошибки: 1" in text
    assert "решения человека: 12" in text and "требует внимания" in text
    assert "включая более старые посты" in text


async def test_match_all_reports_12_manual_even_with_zero_selected(db, monkeypatch):
    import src.cli as cli
    from src.telegram import delivery

    await seed_review_cases()
    summary = AsyncMock()
    monkeypatch.setattr(cli, "_open_userbot_if_needed", AsyncMock(return_value=None))
    monkeypatch.setattr(delivery, "send_run_summary", summary)
    await cli._match_all()
    assert summary.await_args.kwargs["matched"] == 0
    assert summary.await_args.kwargs["no_match"] == 0
    assert summary.await_args.kwargs["needs_review"] == 12


async def test_backfill_validation_pending_is_not_reported_as_no_match(db, monkeypatch):
    import src.cli as cli
    from src.jobs import pipeline
    from src.matching.matcher import MatchResult
    from src.telegram import delivery, monitor

    await seed_review_cases()
    client = MagicMock(start=AsyncMock(), disconnect=AsyncMock())
    monkeypatch.setattr(monitor, "make_client", lambda: client)
    monkeypatch.setattr(
        pipeline,
        "process_new_inbound",
        AsyncMock(
            return_value=MatchResult(
                835,
                None,
                0,
                0,
                "validation_pending",
            )
        ),
    )

    async def backfill(_client, _source, *, limit, on_inbound):
        await on_inbound(835)
        return MagicMock(processed=1, posts_inserted=1, skipped_no_photo=0)

    monkeypatch.setattr(monitor, "backfill", backfill)
    summary = AsyncMock()
    monkeypatch.setattr(delivery, "send_run_summary", summary)
    await cli._backfill(None, 1, True, None)
    assert summary.await_args.kwargs["no_match"] == 0
    assert summary.await_args.kwargs["needs_review"] == 1
    assert summary.await_args.kwargs["pending"] == 0


async def test_test_batch_pending_has_attention_status_and_no_no_match(db, monkeypatch, capsys):
    import src.cli as cli
    from src.jobs import pipeline
    from src.matching.matcher import MatchResult
    from src.telegram import delivery

    await seed_review_cases()

    async def process(inbound_id, **kwargs):
        async with get_session() as session:
            session.add(
                ValidationJob(
                    inbound_id=inbound_id,
                    state="needs_review",
                    reason_code="multiple_identities",
                    attempts=1,
                )
            )
        return MatchResult(inbound_id, None, 0, 0, "validation_pending")

    monkeypatch.setattr(pipeline, "process_new_inbound", process)
    monkeypatch.setattr(cli, "_open_userbot_if_needed", AsyncMock(return_value=None))
    summary = AsyncMock()
    monkeypatch.setattr(delivery, "send_run_summary", summary)
    await cli._test_batch(1, 884)
    assert summary.await_args.kwargs["no_match"] == 0
    assert summary.await_args.kwargs["needs_review"] == 1
    assert "REQUIRES_ATTENTION" in capsys.readouterr().out
