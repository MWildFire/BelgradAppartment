"""Site-listing retention (Stage W2 follow-up).

A curated apartment lives on the public site for a fixed window, then should come down. We do NOT
auto-delete (warn-first, per the operator's choice): the nightly job sends a Telegram notice listing
flats that have been live >= RETENTION_DAYS so Дарья can take them off with one click in the admin
(«Опубликованные» → «На сайт»). Age counts from SiteListing.created_at (when it was put on the site).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select

from src.config import get_settings
from src.db import Listing, Match, get_session
from src.db.models import SiteListing, utcnow
from src.telegram.bot_delivery import send_text_via_bot

logger = logging.getLogger(__name__)

# Mirror src.admin.routes.SITE_RETENTION_DAYS — keep the warn threshold in one mental place.
RETENTION_DAYS = 21


@dataclass(frozen=True)
class OverdueListing:
    site_listing_id: int
    age_days: int
    price_eur: int | None
    district: str | None


async def overdue_listings(*, now: datetime | None = None) -> list[OverdueListing]:
    """Public site listings live on the site >= RETENTION_DAYS, oldest first."""
    # SQLite stores/returns naive datetimes; normalize to naive-UTC so the age math and the
    # created_at <= cutoff comparison never mix offset-aware and offset-naive values.
    now = (now or utcnow()).replace(tzinfo=None)
    cutoff = now - timedelta(days=RETENTION_DAYS)
    stmt = (
        select(SiteListing, Listing)
        .join(Match, Match.id == SiteListing.match_id)
        .join(Listing, Listing.id == Match.listing_id, isouter=True)
        .where(SiteListing.is_public.is_(True), SiteListing.created_at <= cutoff)
        .order_by(SiteListing.created_at.asc())
    )
    async with get_session() as s:
        rows = (await s.execute(stmt)).all()
    return [
        OverdueListing(
            site_listing_id=sl.id,
            age_days=(now - sl.created_at).days,
            price_eur=listing.price_eur if listing else None,
            district=listing.district if listing else None,
        )
        for sl, listing in rows
    ]


def format_notice(overdue: list[OverdueListing]) -> str:
    """Compact Telegram body listing the flats due for removal."""
    lines = [f"🗓 На сайте дольше {RETENTION_DAYS} дней — пора убрать ({len(overdue)}):", ""]
    for o in overdue:
        price = f"{o.price_eur}€" if o.price_eur is not None else "—"
        lines.append(f"• {price}, {o.district or '—'} — {o.age_days} дн.")
    lines += ["", "Снять: админка → «Опубликованные» → «На сайт»."]
    return "\n".join(lines)


async def send_overdue_notice(*, now: datetime | None = None) -> int:
    """Notify Дарью + оператора of flats overdue for removal. No-op when none. Returns the count.

    Reuses the bot channel (TG_BOT_TOKEN / TG_BOT_RECIPIENTS) that already carries lead notices —
    same recipients (Дарья + оператор). Best-effort: the caller (nightly job) logs failures and
    continues; a missed notice is never fatal.
    """
    overdue = await overdue_listings(now=now)
    if not overdue:
        logger.info("retention: no listings overdue for removal")
        return 0
    if not get_settings().tg_bot_token:
        logger.warning("retention: %d overdue but TG_BOT_TOKEN unset — notice not sent", len(overdue))
        return 0
    # Send via the bot DIRECTLY — NOT send_text_to_recipients(None, …), which raises under the
    # userbot delivery mode; mirrors _alert_auth_lost. Fans out to every TG_BOT_RECIPIENTS chat.
    await send_text_via_bot(format_notice(overdue))
    logger.info("retention: notified recipients of %d overdue listing(s)", len(overdue))
    return len(overdue)
