"""Operator decisions stay terminal: no requeue, no rejection after delivery, no rematch wipe."""

from __future__ import annotations

import pytest
from sqlalchemy import update

from src.db import InboundPost, InboundStatus, Match, MatchStatus, get_session
from src.db.models import MatchDelivery, ValidationJob
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


async def test_reject_is_refused_once_a_recipient_is_in_flight(db):
    _, match_id = await seed_case(match_status=MatchStatus.PENDING)
    await prepare_recipients(match_id, [111])
    async with get_session() as s:
        await s.execute(update(MatchDelivery).where(MatchDelivery.match_id == match_id).values(status="sent"))

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

    await review_queue.reset_for_rematch(keep_published=True)

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

    await review_queue.reset_for_rematch(keep_published=True)

    async with get_session() as s:
        match = await s.get(Match, match_id)
        inbound = await s.get(InboundPost, iid)
    assert match is not None and match.status == MatchStatus.PENDING
    assert inbound.status == InboundStatus.MATCHED
    assert iid not in await automatic_inbound_ids()


async def test_reset_still_rematches_a_pending_case_without_deliveries(db):
    iid, match_id = await seed_case(match_status=MatchStatus.PENDING)

    await review_queue.reset_for_rematch(keep_published=True)

    async with get_session() as s:
        assert await s.get(Match, match_id) is None
        inbound = await s.get(InboundPost, iid)
        assert await s.get(ValidationJob, iid) is None
    assert inbound.status == InboundStatus.NEW
    assert iid in await automatic_inbound_ids()
