"""Operator decisions for needs_review cases: local DB only, no provider or delivery."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from click.testing import CliRunner
from sqlalchemy import select

from src.cli import cli
from src.db import (
    InboundPost,
    InboundStatus,
    Listing,
    Match,
    MatchStatus,
    Platform,
    SourceChannel,
    get_session,
)
from src.db.models import PublishedPost, ValidationJob
from src.matching import review_queue
from src.matching.clip_embed import pack_embeddings

_sequence = itertools.count(1)


async def seed_case(
    *,
    inbound_status: InboundStatus = InboundStatus.NEW,
    job_state: str = "needs_review",
    lease_until: datetime | None = None,
    match_status: MatchStatus | None = None,
) -> tuple[int, int | None]:
    n = next(_sequence)
    vecs = list(np.eye(512, dtype=np.float32)[:2])
    async with get_session() as s:
        source = SourceChannel(tg_id=-100000 - n, is_active=True)
        s.add(source)
        await s.flush()
        inbound = InboundPost(
            source_channel_id=source.id,
            tg_message_id=1,
            raw_text="",
            photo_paths=[],
            photo_phashes=["0" * 16, "1" * 16],
            photo_embeddings=pack_embeddings(vecs),
            status=inbound_status,
        )
        listing = Listing(
            platform=Platform.NEKRETNINE,
            external_id=str(n),
            url=f"https://example.test/{n}",
            photo_phashes=["f" * 16, "e" * 16],
            photo_embeddings=pack_embeddings(vecs),
        )
        s.add_all([inbound, listing])
        await s.flush()
        s.add(
            ValidationJob(
                inbound_id=inbound.id,
                state=job_state,
                reason_code="candidate_limit",
                lease_until=lease_until,
                attempts=1,
                outcomes=[],
                updated_at=datetime.now(UTC),
            )
        )
        match_id = None
        if match_status is not None:
            match = Match(
                inbound_post_id=inbound.id,
                listing_id=listing.id,
                confidence=0.9,
                matched_photo_count=2,
                method="phash",
                status=match_status,
            )
            s.add(match)
            await s.flush()
            match_id = match.id
        return inbound.id, match_id


async def test_close_as_no_match_stops_automatic_work_and_keeps_reason(db):
    from src.matching.validation_queue import automatic_inbound_ids

    iid, _ = await seed_case()
    await review_queue.close_review(
        iid, "NO_MATCH", "объект другой: комнаты и площадь не совпадают"
    )

    async with get_session() as s:
        inbound = await s.get(InboundPost, iid)
        job = await s.get(ValidationJob, iid)
    assert inbound.status == InboundStatus.NO_MATCH
    assert job.state == "complete"
    assert job.reason_code == "manual_no_match"
    assert job.outcomes[-1]["decision"] == "NO_MATCH"
    assert job.outcomes[-1]["reason"] == "объект другой: комнаты и площадь не совпадают"
    assert await automatic_inbound_ids() == []
    assert (await review_queue.pending_report())["validation"] == []


async def test_close_as_duplicate_marks_inbound_without_creating_match(db):
    iid, _ = await seed_case()
    await review_queue.close_review(
        iid, "DUPLICATE", "дубль: то же фото, что в объявлении из match 466"
    )

    async with get_session() as s:
        inbound = await s.get(InboundPost, iid)
        matches = list((await s.execute(select(Match))).scalars())
    assert inbound.status == InboundStatus.DUPLICATE
    assert matches == []


@pytest.mark.parametrize(
    ("job_state", "live_lease", "inbound_status", "match_status"),
    [
        ("pending", False, InboundStatus.NEW, None),
        ("needs_review", True, InboundStatus.NEW, None),
        ("needs_review", False, InboundStatus.MATCHED, None),
        ("needs_review", False, InboundStatus.NEW, MatchStatus.REJECTED),
    ],
)
async def test_close_refuses_unless_case_is_open_unleased_and_unmatched(
    db, job_state, live_lease, inbound_status, match_status
):
    # Computed inside the test: a lease fixed at collection time expires in a long suite run.
    lease_until = datetime.now(UTC) + timedelta(hours=1) if live_lease else None
    iid, _ = await seed_case(
        inbound_status=inbound_status,
        job_state=job_state,
        lease_until=lease_until,
        match_status=match_status,
    )
    with pytest.raises(review_queue.ReviewError):
        await review_queue.close_review(iid, "NO_MATCH", "проверка")

    async with get_session() as s:
        inbound = await s.get(InboundPost, iid)
        job = await s.get(ValidationJob, iid)
    assert inbound.status == inbound_status
    assert job.state == job_state
    assert job.outcomes == []


async def test_close_rejects_unknown_decision_and_unusable_reason(db):
    iid, _ = await seed_case()
    for decision, reason in [
        ("PUBLISHED", "проверка"),
        ("NO_MATCH", "   "),
        ("NO_MATCH", "x" * 257),
    ]:
        with pytest.raises(review_queue.ReviewError):
            await review_queue.close_review(iid, decision, reason)

    async with get_session() as s:
        assert (await s.get(ValidationJob, iid)).state == "needs_review"


async def test_reject_pending_match_stores_reason(db):
    _, match_id = await seed_case(match_status=MatchStatus.PENDING)
    assert (
        await review_queue.reject_match(match_id, "объявление снято 23.08, не отправлялось")
        == "rejected"
    )

    async with get_session() as s:
        match = await s.get(Match, match_id)
    assert match.status == MatchStatus.REJECTED
    assert match.rejection_reason == "объявление снято 23.08, не отправлялось"


async def test_reject_refuses_published_and_keeps_existing_rejection(db):
    _, published_id = await seed_case(match_status=MatchStatus.PUBLISHED)
    _, rejected_id = await seed_case(match_status=MatchStatus.REJECTED)
    with pytest.raises(review_queue.ReviewError):
        await review_queue.reject_match(published_id, "проверка")
    assert await review_queue.reject_match(rejected_id, "другая причина") == "already_rejected"

    async with get_session() as s:
        assert (await s.get(Match, published_id)).status == MatchStatus.PUBLISHED
        assert (await s.get(Match, rejected_id)).rejection_reason is None
    with pytest.raises(review_queue.ReviewError):
        await review_queue.reject_match(10**9, "нет такого матча")


async def test_reject_refuses_pending_match_that_has_a_published_post(db):
    _, match_id = await seed_case(match_status=MatchStatus.PENDING)
    async with get_session() as s:
        s.add(PublishedPost(match_id=match_id, tg_message_id=1, post_text="пост"))
    with pytest.raises(review_queue.ReviewError):
        await review_queue.reject_match(match_id, "проверка")
    async with get_session() as s:
        assert (await s.get(Match, match_id)).status == MatchStatus.PENDING


async def test_reject_allows_pending_match_while_another_match_is_published(db):
    _, match_id = await seed_case(match_status=MatchStatus.PENDING)
    _, other_id = await seed_case(match_status=MatchStatus.PENDING)
    async with get_session() as s:
        s.add(PublishedPost(match_id=other_id, tg_message_id=2, post_text="другой пост"))
    assert await review_queue.reject_match(match_id, "объявление снято") == "rejected"


def test_cli_review_close_passes_decision_and_reason(monkeypatch):
    calls = []

    async def record(inbound_id, decision, reason):
        calls.append((inbound_id, decision, reason))

    monkeypatch.setattr(review_queue, "close_review", record)
    result = CliRunner().invoke(
        cli, ["review", "close", "842", "--as", "DUPLICATE", "--reason", "дубль"]
    )
    assert result.exit_code == 0, result.output
    assert calls == [(842, "DUPLICATE", "дубль")]
    assert "inbound=842 closed as DUPLICATE" in result.output


def test_cli_review_close_shows_refusal_and_rejects_unknown_decision(monkeypatch):
    async def refuse(*args):
        raise review_queue.ReviewError("inbound 840: нет открытого кейса needs_review")

    monkeypatch.setattr(review_queue, "close_review", refuse)
    refused = CliRunner().invoke(
        cli, ["review", "close", "840", "--as", "NO_MATCH", "--reason", "проверка"]
    )
    assert refused.exit_code != 0
    assert "нет открытого кейса" in refused.output

    unknown = CliRunner().invoke(
        cli, ["review", "close", "840", "--as", "PUBLISHED", "--reason", "x"]
    )
    assert unknown.exit_code == 2


def test_cli_review_reject_match_passes_reason(monkeypatch):
    calls = []

    async def record(match_id, reason):
        calls.append((match_id, reason))
        return "rejected"

    monkeypatch.setattr(review_queue, "reject_match", record)
    result = CliRunner().invoke(
        cli, ["review", "reject-match", "312", "--reason", "объявление снято 23.08"]
    )
    assert result.exit_code == 0, result.output
    assert calls == [(312, "объявление снято 23.08")]
    assert "match=312 rejected" in result.output
