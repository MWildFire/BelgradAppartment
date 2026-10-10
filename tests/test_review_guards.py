"""Operator decisions stay terminal: no requeue, no rejection after delivery, no rematch wipe."""

from __future__ import annotations

import pytest
from sqlalchemy import event, select, update

from src.db import InboundPost, InboundStatus, Match, MatchStatus, get_session
from src.db.models import MatchDelivery, PublishedPost, SiteListing, ValidationJob
from src.matching import review_queue
from src.matching.validation_queue import automatic_inbound_ids
from src.telegram.delivery_ledger import claim_recipient, prepare_recipients
from tests.test_review_close import seed_case

NO_MATCH_REASON = "объект другой: комнаты и площадь не совпадают"


async def test_requeue_refuses_a_manual_decision(db):
    iid, _ = await seed_case()
    await review_queue.close_review(iid, "NO_MATCH", NO_MATCH_REASON)

    assert await review_queue.requeue_validation(iid) is False

    async with get_session() as s:
        inbound = await s.get(InboundPost, iid)
        job = await s.get(ValidationJob, iid)
    assert inbound.status == InboundStatus.NO_MATCH
    assert job.state == "complete"
    assert job.reason_code == "manual_no_match"


async def test_requeue_still_retries_an_automatic_case(db):
    iid, _ = await seed_case(job_state="needs_review")

    assert await review_queue.requeue_validation(iid) is True

    async with get_session() as s:
        job = await s.get(ValidationJob, iid)
    assert job.state == "pending"
    assert job.reason_code == "operator_retry"


@pytest.mark.parametrize("delivery_state", ["sending", "sent", "unknown"])
async def test_reject_is_refused_once_a_recipient_is_in_flight(db, delivery_state):
    _, match_id = await seed_case(match_status=MatchStatus.PENDING)
    await prepare_recipients(match_id, [111])
    async with get_session() as s:
        await s.execute(
            update(MatchDelivery)
            .where(MatchDelivery.match_id == match_id)
            .values(status=delivery_state)
        )

    with pytest.raises(review_queue.ReviewError):
        await review_queue.reject_match(match_id, "объявление снято")

    async with get_session() as s:
        match = await s.get(Match, match_id)
    assert match.status == MatchStatus.PENDING


async def test_reject_succeeds_while_deliveries_are_only_queued(db):
    _, match_id = await seed_case(match_status=MatchStatus.PENDING)
    await prepare_recipients(match_id, [111])

    assert await review_queue.reject_match(match_id, "объявление снято") == "rejected"

    assert await claim_recipient(match_id, 111) is None


async def test_claim_refuses_a_match_rejected_before_fan_out(db):
    _, match_id = await seed_case(match_status=MatchStatus.PENDING)
    assert await review_queue.reject_match(match_id, "объявление снято") == "rejected"

    await prepare_recipients(match_id, [111])

    assert await claim_recipient(match_id, 111) is None


async def test_rematch_reset_keeps_manual_decisions_and_rejected_matches(db):
    manual_iid, _ = await seed_case()
    await review_queue.close_review(manual_iid, "NO_MATCH", NO_MATCH_REASON)
    rejected_iid, rejected_match = await seed_case(
        inbound_status=InboundStatus.MATCHED, job_state="complete", match_status=MatchStatus.PENDING
    )
    assert await review_queue.reject_match(rejected_match, "объявление снято") == "rejected"

    await review_queue.reset_for_rematch()

    async with get_session() as s:
        manual_inbound = await s.get(InboundPost, manual_iid)
        manual_job = await s.get(ValidationJob, manual_iid)
        rejected = await s.get(Match, rejected_match)
        rejected_inbound = await s.get(InboundPost, rejected_iid)
        rejected_job = await s.get(ValidationJob, rejected_iid)
    assert manual_inbound.status == InboundStatus.NO_MATCH
    assert manual_job.reason_code == "manual_no_match"
    assert rejected.status == MatchStatus.REJECTED
    assert rejected_inbound.status == InboundStatus.MATCHED
    assert rejected_job is not None
    assert rejected_iid not in await automatic_inbound_ids()


async def test_reset_keeps_a_match_that_already_has_delivery_rows(db):
    iid, match_id = await seed_case(
        inbound_status=InboundStatus.MATCHED, job_state="complete", match_status=MatchStatus.PENDING
    )
    await prepare_recipients(match_id, [111])

    await review_queue.reset_for_rematch()

    async with get_session() as s:
        match = await s.get(Match, match_id)
        inbound = await s.get(InboundPost, iid)
    assert match is not None and match.status == MatchStatus.PENDING
    assert inbound.status == InboundStatus.MATCHED
    assert iid not in await automatic_inbound_ids()


async def test_reset_still_rematches_a_pending_case_without_deliveries(db):
    iid, match_id = await seed_case(match_status=MatchStatus.PENDING)

    await review_queue.reset_for_rematch()

    async with get_session() as s:
        assert await s.get(Match, match_id) is None
        inbound = await s.get(InboundPost, iid)
        assert await s.get(ValidationJob, iid) is None
    assert inbound.status == InboundStatus.NEW
    assert iid in await automatic_inbound_ids()


async def test_decided_inbounds_protect_published_and_delivered_posts(db):
    published_iid, _ = await seed_case(
        inbound_status=InboundStatus.MATCHED, job_state="complete", match_status=MatchStatus.PUBLISHED
    )
    sent_iid, sent_match = await seed_case(
        inbound_status=InboundStatus.MATCHED, job_state="complete", match_status=MatchStatus.PENDING
    )
    await prepare_recipients(sent_match, [111])
    async with get_session() as s:
        await s.execute(
            update(MatchDelivery).where(MatchDelivery.match_id == sent_match).values(status="sent")
        )
    plain_iid, _ = await seed_case(match_status=MatchStatus.PENDING)
    manual_iid, _ = await seed_case()
    await review_queue.close_review(manual_iid, "NO_MATCH", NO_MATCH_REASON)

    decided = await review_queue.decided_inbound_ids(
        [published_iid, sent_iid, plain_iid, manual_iid]
    )

    assert decided == {published_iid, sent_iid, manual_iid}


@pytest.mark.parametrize(
    "history",
    ["undecided", "rejected", "queued", "sending", "sent", "unknown", "published_post", "site_overlay"],
)
async def test_decided_inbound_ids_cover_every_kind_of_history(db, history):
    iid, match_id = await seed_case(match_status=MatchStatus.PENDING)
    if history == "rejected":
        assert await review_queue.reject_match(match_id, "объявление снято") == "rejected"
    elif history in {"queued", "sending", "sent", "unknown"}:
        await prepare_recipients(match_id, [111])
        if history != "queued":
            async with get_session() as s:
                await s.execute(
                    update(MatchDelivery).where(MatchDelivery.match_id == match_id).values(status=history)
                )
    elif history == "published_post":
        async with get_session() as s:
            s.add(PublishedPost(match_id=match_id, tg_message_id=7, post_text="опубликовано вручную"))
    elif history == "site_overlay":
        async with get_session() as s:
            s.add(SiteListing(match_id=match_id))

    decided = await review_queue.decided_inbound_ids([iid])

    assert decided == (set() if history == "undecided" else {iid})


async def test_rematch_reset_writes_before_it_reads_decisions(db):
    """Reads taken before the first write can miss a decision committed in between.

    The first statement must be the write, so SQLite holds the write lock for the rest of the
    transaction and the decided state read afterwards cannot change under it.
    """
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(db.sync_engine, "before_cursor_execute", record)
    try:
        await review_queue.reset_for_rematch()
    finally:
        event.remove(db.sync_engine, "before_cursor_execute", record)

    assert statements[0].lstrip().upper().startswith("DELETE")


async def test_reset_wipes_only_undecided_matches(db):
    undecided_iid, undecided_match = await seed_case(match_status=MatchStatus.PENDING)
    _, published_match = await seed_case(
        inbound_status=InboundStatus.MATCHED, job_state="complete", match_status=MatchStatus.PUBLISHED
    )
    _, history_match = await seed_case(
        inbound_status=InboundStatus.MATCHED, job_state="complete", match_status=MatchStatus.PENDING
    )
    _, overlay_match = await seed_case(
        inbound_status=InboundStatus.MATCHED, job_state="complete", match_status=MatchStatus.PENDING
    )
    _, rejected_match = await seed_case(
        inbound_status=InboundStatus.MATCHED, job_state="complete", match_status=MatchStatus.PENDING
    )
    assert await review_queue.reject_match(rejected_match, "объявление снято") == "rejected"
    manual_iid, _ = await seed_case()
    await review_queue.close_review(manual_iid, "NO_MATCH", NO_MATCH_REASON)
    async with get_session() as s:
        s.add(PublishedPost(match_id=history_match, tg_message_id=7, post_text="опубликовано вручную"))
        s.add(SiteListing(match_id=overlay_match))

    await review_queue.reset_for_rematch()

    async with get_session() as s:
        assert await s.get(Match, undecided_match) is None
        assert (await s.get(InboundPost, undecided_iid)).status == InboundStatus.NEW
        for match_id in (published_match, history_match, overlay_match, rejected_match):
            assert await s.get(Match, match_id) is not None
        assert (await s.get(InboundPost, manual_iid)).status == InboundStatus.NO_MATCH
        post_text = await s.execute(
            select(PublishedPost.post_text).where(PublishedPost.match_id == history_match)
        )
        assert post_text.scalar_one() == "опубликовано вручную"
    assert undecided_iid in await automatic_inbound_ids()


async def test_reset_undecided_for_test_never_drops_a_decided_match(db):
    published_iid, published_match = await seed_case(
        inbound_status=InboundStatus.MATCHED, job_state="complete", match_status=MatchStatus.PUBLISHED
    )
    undecided_iid, undecided_match = await seed_case(match_status=MatchStatus.PENDING)

    dropped = await review_queue.reset_undecided_for_test([published_iid, undecided_iid])

    assert dropped == 1
    async with get_session() as s:
        assert await s.get(Match, published_match) is not None
        assert (await s.get(InboundPost, published_iid)).status == InboundStatus.MATCHED
        assert await s.get(Match, undecided_match) is None
        assert (await s.get(InboundPost, undecided_iid)).status == InboundStatus.NEW


async def test_test_batch_skips_a_post_with_publication_history(db, capsys):
    import src.cli as cli

    _, match_id = await seed_case(
        inbound_status=InboundStatus.MATCHED, job_state="complete", match_status=MatchStatus.PENDING
    )
    async with get_session() as s:
        s.add(PublishedPost(match_id=match_id, tg_message_id=7, post_text="опубликовано вручную"))

    await cli._test_batch(1, None)

    assert "skipped posts with operator decisions" in capsys.readouterr().out
    async with get_session() as s:
        assert await s.get(Match, match_id) is not None
        history = await s.execute(select(PublishedPost.id).where(PublishedPost.match_id == match_id))
        assert history.scalar_one_or_none() is not None


async def test_rematch_all_refuses_to_wipe_published_history(db):
    from click.testing import CliRunner

    from src.cli import cli

    _, match_id = await seed_case(
        inbound_status=InboundStatus.MATCHED, job_state="complete", match_status=MatchStatus.PUBLISHED
    )

    result = CliRunner().invoke(cli, ["rematch-all", "--reset-published"])

    assert result.exit_code == 2
    assert "is refused" in result.output
    async with get_session() as s:
        assert await s.get(Match, match_id) is not None
