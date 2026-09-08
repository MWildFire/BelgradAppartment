"""One conservative automatic publication-removal policy for all maintenance paths."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select, update

from src.config import get_settings
from src.db.models import Listing, Match, SiteListing
from src.site.availability import apartment_is_available

ACTIVE = "ACTIVE"
DEAD = "DEAD"
INCONCLUSIVE = "INCONCLUSIVE"


@dataclass(frozen=True)
class PublicationDecision:
    should_hide: bool
    reason: str


def evaluate_publication(
    member_statuses: Iterable[str], *, pinned: bool, auto_hide: bool, remaining_hides: int
) -> PublicationDecision:
    statuses = tuple(member_statuses)
    if pinned:
        return PublicationDecision(False, "pinned")
    if apartment_is_available(status == ACTIVE for status in statuses):
        return PublicationDecision(False, "active_sibling")
    if not statuses or any(status != DEAD for status in statuses):
        return PublicationDecision(False, "unconfirmed")
    if not auto_hide:
        return PublicationDecision(False, "warn_only")
    if remaining_hides <= 0:
        return PublicationDecision(False, "cap_reached")
    return PublicationDecision(True, "all_members_dead")


async def apply_publication_policy(
    session,
    site_listing_id: int,
    *,
    member_status_by_id: Mapping[int, str],
    remaining_hides: int,
    dry_run: bool = False,
) -> tuple[PublicationDecision, int]:
    """Re-read pin/group/activity in the writer transaction and change exactly one card.

    An inactive database flag is never sufficient proof of death: callers must supply a
    definitive observation for every inactive group member. Active state always wins.
    """
    card = (
        await session.execute(
            select(
                SiteListing.id,
                SiteListing.keep_on_site,
                Listing.id.label("listing_id"),
                Listing.dup_group_id,
            )
            .join(Match, Match.id == SiteListing.match_id)
            .join(Listing, Listing.id == Match.listing_id)
            .where(SiteListing.id == site_listing_id, SiteListing.is_public.is_(True))
        )
    ).one_or_none()
    if card is None:
        return PublicationDecision(False, "already_hidden"), 0
    membership = (
        (Listing.dup_group_id == card.dup_group_id)
        if card.dup_group_id is not None
        else (Listing.id == card.listing_id)
    )
    now = datetime.now(UTC)
    cutoff = now - timedelta(hours=24)
    members = (await session.execute(select(
        Listing.id, Listing.is_active, Listing.link_status, Listing.link_checked_at,
    ).where(membership))).all()

    def current_status(member):
        if member.is_active:
            return ACTIVE
        checked = member.link_checked_at
        checked = checked.replace(tzinfo=UTC) if checked and checked.tzinfo is None else checked
        if (member.link_status != DEAD or checked is None
                or not cutoff <= checked <= now):
            return INCONCLUSIVE
        return member_status_by_id.get(member.id, INCONCLUSIVE)

    decision = evaluate_publication(
        (current_status(member) for member in members),
        pinned=card.keep_on_site,
        auto_hide=get_settings().site_liveness_auto_hide,
        remaining_hides=remaining_hides,
    )
    if not decision.should_hide or dry_run:
        return decision, 0
    # Recheck active membership and pin at the write itself, including concurrent changes.
    result = await session.execute(
        update(SiteListing)
        .where(
            SiteListing.id == site_listing_id,
            SiteListing.is_public.is_(True),
            SiteListing.keep_on_site.is_(False),
            select(Listing.id)
            .where(
                Listing.id == card.listing_id,
                Listing.dup_group_id.is_not_distinct_from(card.dup_group_id),
            )
            .exists(),
            ~select(Listing.id).where(membership, or_(
                Listing.is_active.is_(True),
                Listing.link_status.is_distinct_from(DEAD),
                Listing.link_checked_at.is_(None),
                Listing.link_checked_at < cutoff,
                Listing.link_checked_at > now,
            )).exists(),
        )
        .values(is_public=False, is_featured=False)
    )
    hidden = result.rowcount or 0
    if hidden:
        from src.site.cache import invalidate_catalog_cache

        invalidate_catalog_cache()
    return decision, hidden
