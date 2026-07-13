"""Site-listing liveness sweep — the successor to the age-based retention notice (D5).

A curated public card stays up while the apartment still exists on ≥1 platform, and comes down only
when it is CONFIRMED gone from every platform it was matched on. "Same apartment" = the A4 dup-group
(listings sharing `dup_group_id`; a NULL group is a group of one — the matched listing itself).
Card age is NOT a reason to remove (Дмитрий, 14.07); the 21-day notice is retired.

Member status (C0):
  ACTIVE        is_active=1
  DEAD          is_active=0 AND a target HEAD of its URL returned 404/410 (definitively gone)
  INCONCLUSIVE  is_active=0 but HEAD gave 403/405/5xx/timeout/network — state unknown → fail-safe

Decision per public card (site_listings.is_public=1, resolved match_id → listing → dup-group):
  keep_on_site=1 (pin, D4)              → KEEP (pinned)
  any member ACTIVE                     → KEEP (age irrelevant)
  any INCONCLUSIVE (and none ACTIVE)    → KEEP + «⚠️ candidate (unconfirmed)»
  ALL members DEAD                      → HIDE (SITE_LIVENESS_AUTO_HIDE=1, up to the nightly cap D3),
                                          otherwise «🪦 candidate (confirmed)»

No "platform unhealthy tonight" guard in v1: the per-URL target HEAD is already fail-safe (a blocked
platform → 403 → INCONCLUSIVE → KEEP). Recorded as a deliberate simplification.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

import httpx
from sqlalchemy import select

from src.config import get_settings
from src.db import Listing, Match, get_session
from src.db.models import SiteListing
from src.scrapers.orchestrator import (
    _DEAD_LINK_STATUSES,
    _make_side_client,
    hide_site_listings_for_listing,
)
from src.telegram.bot_delivery import send_text_via_bot

logger = logging.getLogger(__name__)

# How many rows to list per section before collapsing the rest into «…и ещё N» (D8).
_NOTICE_TOP = 10

# Member statuses (C0). Plain strings keep the report trivially serialisable for logs/tests.
_ACTIVE = "ACTIVE"
_DEAD = "DEAD"
_INCONCLUSIVE = "INCONCLUSIVE"


@dataclass(frozen=True, slots=True)
class LivenessCandidate:
    """One flagged public card — enough to render the notice line and the admin badge."""

    site_listing_id: int
    listing_id: int
    price_eur: int | None
    district: str | None  # already D8-fallbacked (district → address_raw → title)
    group_size: int
    dead: int
    inconclusive: int
    url: str


@dataclass
class LivenessReport:
    """Outcome of one sweep. kept_* are counts; the candidate/hidden lists carry display rows."""

    kept_active: int = 0
    kept_pinned: int = 0
    candidates_unconfirmed: list[LivenessCandidate] = field(default_factory=list)
    candidates_confirmed: list[LivenessCandidate] = field(default_factory=list)
    hidden: list[LivenessCandidate] = field(default_factory=list)
    errors: int = 0

    @property
    def total_flagged(self) -> int:
        return (
            len(self.candidates_unconfirmed)
            + len(self.candidates_confirmed)
            + len(self.hidden)
        )


async def _head_liveness(client: httpx.AsyncClient, url: str) -> tuple[str, bool]:
    """Target HEAD with A3/D7 semantics → (status, errored).

    404/410 = DEAD (definitively gone); everything else (403/405/5xx/timeout/network) = INCONCLUSIVE
    (unknown → fail-safe KEEP). `errored` flags transport failures for observability only — they are
    still treated as INCONCLUSIVE, never as DEAD.
    """
    try:
        resp = await client.head(url, timeout=10.0)
    except Exception as e:  # noqa: BLE001 — any transport failure is inconclusive, never fatal
        logger.info("[liveness] HEAD %s inconclusive: %s: %s", url, type(e).__name__, e)
        return _INCONCLUSIVE, True
    if resp.status_code in _DEAD_LINK_STATUSES:
        return _DEAD, False
    return _INCONCLUSIVE, False


async def site_liveness_sweep(
    *,
    now: datetime | None = None,  # reserved for signature parity; liveness has no age component
    client: httpx.AsyncClient | None = None,
    dry_run: bool = False,
) -> LivenessReport:
    """Sweep every public card and decide KEEP / candidate / HIDE per the C0 matrix.

    HEADs only INACTIVE dup-group members (active members are ACTIVE without a network call), and
    each distinct URL at most once per sweep. `client` is injectable (like verify_active_links); when
    omitted a proxy-aware side client is built (T1-5) so an IP block routes HEADs via the proxy.
    `dry_run` computes the exact same decisions but mutates nothing (the hidden list becomes
    would-hide). Auto-hide is gated by SITE_LIVENESS_AUTO_HIDE (D1) and capped by
    SITE_LIVENESS_MAX_HIDES (D3); overflow spills into candidates_confirmed.
    """
    settings = get_settings()
    report = LivenessReport()

    async with get_session() as s:
        card_rows = (
            await s.execute(
                select(
                    SiteListing.id.label("sl_id"),
                    SiteListing.keep_on_site,
                    Listing.id.label("listing_id"),
                    Listing.dup_group_id,
                    Listing.is_active,
                    Listing.url,
                    Listing.price_eur,
                    Listing.district,
                    Listing.address_raw,
                    Listing.title,
                )
                .join(Match, Match.id == SiteListing.match_id)
                .join(Listing, Listing.id == Match.listing_id)
                .where(SiteListing.is_public.is_(True))
            )
        ).all()

        if not card_rows:
            return report

        # Resolve every dup-group's full membership in one query. NB: an inactive listing keeps its
        # last dup_group_id (dedup only resets ACTIVE listings), so a card whose own listing went
        # inactive still resolves to the group that holds its still-active platform siblings.
        group_ids = {r.dup_group_id for r in card_rows if r.dup_group_id is not None}
        members_by_group: dict[int, list[tuple[int, bool, str]]] = {}
        if group_ids:
            for m in (
                await s.execute(
                    select(Listing.id, Listing.dup_group_id, Listing.is_active, Listing.url).where(
                        Listing.dup_group_id.in_(group_ids)
                    )
                )
            ).all():
                members_by_group.setdefault(m.dup_group_id, []).append((m.id, m.is_active, m.url))

    # Build per-card member lists (dup-group members, or the lone listing for a NULL group).
    card_members: dict[int, list[tuple[int, bool, str]]] = {}
    for r in card_rows:
        if r.dup_group_id is not None and members_by_group.get(r.dup_group_id):
            card_members[r.sl_id] = members_by_group[r.dup_group_id]
        else:
            card_members[r.sl_id] = [(r.listing_id, r.is_active, r.url)]

    # HEAD every distinct INACTIVE member URL once (active members need no network call).
    inactive_urls = {
        url
        for members in card_members.values()
        for (_lid, is_active, url) in members
        if not is_active
    }
    url_status: dict[str, str] = {}
    if inactive_urls:
        owns_client = client is None
        if owns_client:
            from src.scrapers.base import DEFAULT_USER_AGENT

            ua = settings.scraper_user_agent or DEFAULT_USER_AGENT
            client = _make_side_client(headers={"User-Agent": ua}, timeout=10.0)
        try:
            for url in inactive_urls:
                status, errored = await _head_liveness(client, url)
                url_status[url] = status
                if errored:
                    report.errors += 1
        finally:
            if owns_client:
                await client.aclose()

    for r in card_rows:
        members = card_members[r.sl_id]
        any_active = any(is_active for (_lid, is_active, _url) in members)
        dead = sum(
            1
            for (_lid, is_active, url) in members
            if not is_active and url_status.get(url) == _DEAD
        )
        inconclusive = sum(
            1
            for (_lid, is_active, url) in members
            if not is_active and url_status.get(url) == _INCONCLUSIVE
        )
        candidate = LivenessCandidate(
            site_listing_id=r.sl_id,
            listing_id=r.listing_id,
            price_eur=r.price_eur,
            # D8 fallback fixes the "—" from the retention screenshot: district → address → title.
            district=r.district or r.address_raw or r.title,
            group_size=len(members),
            dead=dead,
            inconclusive=inconclusive,
            url=r.url,
        )

        if r.keep_on_site:
            report.kept_pinned += 1
        elif any_active:
            report.kept_active += 1
        elif inconclusive:
            report.candidates_unconfirmed.append(candidate)
        elif settings.site_liveness_auto_hide and len(report.hidden) < settings.site_liveness_max_hides:
            # All members DEAD, auto-hide on and under the nightly cap → hide (unless dry-run).
            report.hidden.append(candidate)
            if not dry_run:
                async with get_session() as s2:
                    await hide_site_listings_for_listing(s2, r.listing_id)
        else:
            # All DEAD but auto-hide off (warn-first) or cap reached → confirmed candidate.
            report.candidates_confirmed.append(candidate)

    logger.info(
        "[liveness] cards=%d kept_active=%d kept_pinned=%d unconfirmed=%d confirmed=%d hidden=%d "
        "errors=%d dry_run=%s",
        len(card_rows), report.kept_active, report.kept_pinned,
        len(report.candidates_unconfirmed), len(report.candidates_confirmed),
        len(report.hidden), report.errors, dry_run,
    )
    return report


# --------------------------------------------------------------------------- notice (D8)


def _fmt_row(c: LivenessCandidate) -> str:
    price = f"{c.price_eur}€" if c.price_eur is not None else "—"
    where = c.district or "—"  # already district → address_raw → title fallbacked in the sweep
    if c.group_size > 1:
        return f"• {price}, {where} — мертво {c.dead}/{c.group_size} площадок"
    return f"• {price}, {where}"


def _section(title: str, items: list[LivenessCandidate]) -> list[str]:
    lines = [title]
    lines += [_fmt_row(c) for c in items[:_NOTICE_TOP]]
    if len(items) > _NOTICE_TOP:
        lines.append(f"…и ещё {len(items) - _NOTICE_TOP}")
    return lines


def format_liveness_notice(report: LivenessReport) -> str | None:
    """Compact Telegram body (D8). Sections: auto-hidden / confirmed-dead candidates / unconfirmed.
    Top-10 per section + «…и ещё N». Returns None when nothing is flagged (the caller stays silent).
    """
    if report.total_flagged == 0:
        return None
    blocks: list[str] = ["🏠 Живость карточек"]
    if report.hidden:
        blocks.append("")
        blocks += _section(f"🪦 сняты автоматически ({len(report.hidden)}):", report.hidden)
    if report.candidates_confirmed:
        blocks.append("")
        blocks += _section(
            f"🪦 подтверждённо мертвы — снять вручную ({len(report.candidates_confirmed)}):",
            report.candidates_confirmed,
        )
    if report.candidates_unconfirmed:
        blocks.append("")
        blocks += _section(
            f"⚠️ не подтверждено — проверить ({len(report.candidates_unconfirmed)}):",
            report.candidates_unconfirmed,
        )
    return "\n".join(blocks)


async def send_liveness_notice(report: LivenessReport) -> bool:
    """Send the liveness notice to Дарья + оператор via the bot. Silent (returns False) when nothing
    is flagged or TG_BOT_TOKEN is unset — same delivery as the retired retention notice.

    Sends via the bot DIRECTLY (send_text_via_bot fans out to every TG_BOT_RECIPIENTS chat), NOT
    send_text_to_recipients(None, …) which raises under the userbot delivery mode.
    """
    body = format_liveness_notice(report)
    if body is None:
        logger.info("[liveness] nothing to report — notice not sent")
        return False
    if not get_settings().tg_bot_token:
        logger.warning(
            "[liveness] %d flagged but TG_BOT_TOKEN unset — notice not sent", report.total_flagged
        )
        return False
    await send_text_via_bot(body)
    logger.info("[liveness] notice sent (%d flagged)", report.total_flagged)
    return True
