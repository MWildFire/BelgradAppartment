from datetime import UTC, datetime

import pytest
from sqlalchemy import select, update
from sqlalchemy.sql.dml import Update

from src.config import get_settings
from src.db.models import InboundPost, Listing, Match, Platform, SiteListing, SourceChannel
from src.db.session import get_session
from src.site.publication_policy import apply_publication_policy


@pytest.mark.parametrize("race", ["group", "link_unknown"])
async def test_changed_evidence_before_hide_preserves_public_card(db, monkeypatch, race):
    monkeypatch.setattr(get_settings(), "site_liveness_auto_hide", True)
    async with get_session() as session:
        session.add(SourceChannel(id=1, tg_id=100))
        session.add(
            Listing(
                id=1,
                platform=Platform.NEKRETNINE,
                external_id="old",
                url="https://x/1",
                is_active=False,
                link_status="DEAD",
                link_checked_at=datetime.now(UTC),
                dup_group_id=1,
            )
        )
        session.add(
            Listing(
                id=2,
                platform=Platform.HALOOGLASI,
                external_id="live",
                url="https://x/2",
                is_active=True,
                dup_group_id=99,
            )
        )
        await session.flush()
        session.add(InboundPost(id=1, source_channel_id=1, tg_message_id=1))
        await session.flush()
        session.add(
            Match(
                id=1,
                inbound_post_id=1,
                listing_id=1,
                matched_photo_count=2,
                method="phash",
                confidence=1,
            )
        )
        await session.flush()
        session.add(SiteListing(id=1, match_id=1, is_public=True))
    async with get_session() as session:
        execute = session.execute

        async def reassign_before_update(statement, *args, **kwargs):
            if isinstance(statement, Update) and statement.table.name == "site_listings":
                values = {"dup_group_id": 99} if race == "group" else {"link_status": "INCONCLUSIVE"}
                await execute(update(Listing).where(Listing.id == 1).values(**values))
            return await execute(statement, *args, **kwargs)

        monkeypatch.setattr(session, "execute", reassign_before_update)
        _decision, hidden = await apply_publication_policy(
            session,
            1,
            member_status_by_id={1: "DEAD"},
            remaining_hides=10,
        )
        assert hidden == 0
        assert (await execute(select(SiteListing.is_public))).scalar_one() is True
