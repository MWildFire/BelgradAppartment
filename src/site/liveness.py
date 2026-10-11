"""Site-listing liveness sweep — the successor to the age-based retention notice (D5).

A curated public card stays up while the apartment still exists on ≥1 platform, and comes down only
when it is CONFIRMED gone from every platform it was matched on. "Same apartment" = the A4 dup-group
(listings sharing `dup_group_id`; a NULL group is a group of one — the matched listing itself).
Card age is NOT a reason to remove (Дмитрий, 14.07); the 21-day notice is retired.

Member status (C0):
  ACTIVE        is_active=1
  DEAD          is_active=0 AND a source GET/browser probe returned 404/410
  INCONCLUSIVE  is_active=0 but its source probe was blocked, incomplete or failed
                → unknown, keep the card

Decision per public card (site_listings.is_public=1, resolved match_id → listing → dup-group):
  keep_on_site=1 (pin, D4)              → KEEP (pinned)
  any member ACTIVE                     → KEEP (age irrelevant)
  any INCONCLUSIVE (and none ACTIVE)    → KEEP + «⚠️ candidate (unconfirmed)»
  ALL members DEAD                      → HIDE (SITE_LIVENESS_AUTO_HIDE=1, up to the nightly cap D3),
                                          otherwise «🪦 candidate (confirmed)»

Source probes use the scraper route and validate a listing payload; a blocked or unavailable
source stays INCONCLUSIVE. Bounded source leases prevent concurrent browser/profile use.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select, update

from src.config import get_settings
from src.db import Listing, Match, get_session
from src.db.models import Platform, SiteListing
from src.jobs.lifecycle import remaining_budget
from src.scrapers.link_probe import LinkObservation, SourceProbeSession
from src.scrapers.orchestrator import _DEAD_LINK_STATUSES
from src.site.publication_policy import apply_publication_policy, evaluate_publication
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
    # Source-level failures (a source's probe session timed out or raised), already in `errors`.
    # They never probed a listing, so the caller must not count them as probe failures.
    source_errors: int = 0
    checked: int = 0
    skipped: int = 0

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
    max_hides: int | None = None,
) -> LivenessReport:
    """Sweep every public card and decide KEEP / candidate / HIDE per the C0 matrix.

    Probes only INACTIVE dup-group members and each URL at most once per sweep. Production
    uses the shared source GET/browser route with payload validation. An explicitly injected
    HTTP client supports offline HEAD tests; a HEAD 200 alone never establishes ACTIVE.
    `dry_run` computes the exact same decisions but mutates nothing (the hidden list becomes
    would-hide). Auto-hide is gated by SITE_LIVENESS_AUTO_HIDE (D1) and capped by
    SITE_LIVENESS_MAX_HIDES (D3); overflow spills into candidates_confirmed.
    """
    settings = get_settings()
    report = LivenessReport()
    hide_limit = settings.site_liveness_max_hides if max_hides is None else max(0, max_hides)

    async with get_session() as s:
        card_rows = (
            await s.execute(
                select(
                    SiteListing.id.label("sl_id"),
                    SiteListing.keep_on_site,
                    Listing.id.label("listing_id"),
                    Listing.platform,
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
        members_by_group: dict[int, list[tuple[int, bool, str, Platform]]] = {}
        if group_ids:
            for m in (
                await s.execute(
                    select(
                        Listing.id,
                        Listing.dup_group_id,
                        Listing.is_active,
                        Listing.url,
                        Listing.platform,
                    ).where(
                        Listing.dup_group_id.in_(group_ids)
                    )
                )
            ).all():
                members_by_group.setdefault(m.dup_group_id, []).append(
                    (m.id, m.is_active, m.url, m.platform)
                )

    # Build per-card member lists (dup-group members, or the lone listing for a NULL group).
    card_members: dict[int, list[tuple[int, bool, str, Platform]]] = {}
    for r in card_rows:
        if r.dup_group_id is not None and members_by_group.get(r.dup_group_id):
            card_members[r.sl_id] = members_by_group[r.dup_group_id]
        else:
            card_members[r.sl_id] = [(r.listing_id, r.is_active, r.url, r.platform)]

    # Persist oldest-first attempts so a bounded nightly queue eventually reaches every
    # inactive public member. Cached evidence is short-lived; stale/unvisited means UNKNOWN.
    inactive = {
        lid: (url, platform)
        for members in card_members.values()
        for lid, is_active, url, platform in members
        if not is_active
    }
    url_status: dict[str, str] = {}
    now = datetime.now(UTC)
    queue = []
    if inactive:
        async with get_session() as session:
            records = (await session.execute(select(
                Listing.id, Listing.link_checked_at, Listing.link_next_retry_at,
                Listing.link_status,
            ).where(Listing.id.in_(inactive)).order_by(
                Listing.link_checked_at.asc().nullsfirst(), Listing.id,
            ))).all()
        for record in records:
            url, platform = inactive[record.id]
            checked = record.link_checked_at
            retry = record.link_next_retry_at
            checked = checked.replace(tzinfo=UTC) if checked and checked.tzinfo is None else checked
            retry = retry.replace(tzinfo=UTC) if retry and retry.tzinfo is None else retry
            recent = checked and timedelta(0) <= now - checked < timedelta(hours=24)
            if recent and record.link_status in {_ACTIVE, _DEAD}:
                url_status[url] = record.link_status
            elif retry and retry > now:
                url_status[url] = _INCONCLUSIVE
            else:
                queue.append((record.id, url, platform))

    async def begin_probe(row):
        # Commit before network I/O. Outer cancellation must not leave a hung oldest
        # URL eligible at the front of the same source's queue forever.
        if not dry_run:
            async with get_session() as session:
                await session.execute(update(Listing).where(Listing.id == row[0]).values(
                    link_checked_at=now, link_status=_INCONCLUSIVE,
                    link_next_retry_at=now + timedelta(hours=6),
                ))
        report.checked += 1

    async def record_probe(row, observation):
        lid, url, _platform = row
        url_status[url] = observation.status
        report.errors += int(observation.errored)
        if not dry_run:
            values = {
                "link_checked_at": now, "link_status": observation.status,
                "link_next_retry_at": now + timedelta(hours=6)
                if observation.status == _INCONCLUSIVE else None,
            }
            if observation.status in {_ACTIVE, _DEAD}:
                values["link_succeeded_at"] = now
            if observation.status == _ACTIVE:
                values.update(is_active=True, deactivated_at=None, last_seen_at=now)
            async with get_session() as session:
                await session.execute(update(Listing).where(Listing.id == lid).values(**values))

    budget = remaining_budget(settings.site_liveness_budget_sec, reserve=180)
    deadline = time.monotonic() + budget
    if client is not None:
        for row in queue:
            if time.monotonic() >= deadline:
                break
            await begin_probe(row)
            status, errored = await _head_liveness(client, row[1])
            await record_probe(row, LinkObservation(status, errored))
    else:
        # Sources retain separate time slices, even when the first browser is hung.
        platforms = list(dict.fromkeys(row[2] for row in queue))
        for index, platform in enumerate(platforms):
            source_budget = max(0, deadline - time.monotonic()) / (len(platforms) - index)
            source_deadline = time.monotonic() + source_budget
            if source_budget <= 0:
                break
            try:
                async with asyncio.timeout(source_budget):
                    async with SourceProbeSession(platform) as probe:
                        for row in (row for row in queue if row[2] == platform):
                            left = source_deadline - time.monotonic()
                            if left <= 0:
                                break
                            await begin_probe(row)
                            try:
                                async with asyncio.timeout(left):
                                    observation = await probe.probe(row[1])
                            except TimeoutError:
                                observation = LinkObservation(_INCONCLUSIVE, True, "source_budget")
                            await record_probe(row, observation)
            except TimeoutError:
                report.errors += 1
                report.source_errors += 1
            except Exception:
                logger.exception("[liveness] bounded source check failed: %s", platform.value)
                report.errors += 1
                report.source_errors += 1
    report.skipped = len(queue) - report.checked

    for r in card_rows:
        members = card_members[r.sl_id]
        dead = sum(
            1
            for (_lid, is_active, url, _platform) in members
            if not is_active and url_status.get(url) == _DEAD
        )
        inconclusive = sum(
            1
            for (_lid, is_active, url, _platform) in members
            if not is_active and url_status.get(url, _INCONCLUSIVE) == _INCONCLUSIVE
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

        member_status = {
            lid: _ACTIVE if is_active else url_status.get(url, _INCONCLUSIVE)
            for lid, is_active, url, _platform in members
        }
        decision = evaluate_publication(
            member_status.values(),
            pinned=r.keep_on_site,
            auto_hide=settings.site_liveness_auto_hide,
            remaining_hides=hide_limit - len(report.hidden),
        )
        if decision.reason == "pinned":
            report.kept_pinned += 1
        elif decision.reason == "active_sibling":
            report.kept_active += 1
        elif decision.reason == "unconfirmed":
            report.candidates_unconfirmed.append(candidate)
        elif decision.should_hide:
            if dry_run:
                report.hidden.append(candidate)
            else:
                async with get_session() as s2:
                    current, count = await apply_publication_policy(
                        s2, r.sl_id, member_status_by_id=member_status,
                        remaining_hides=hide_limit-len(report.hidden),
                    )
                if count:
                    report.hidden.append(candidate)
                elif current.reason == "pinned":
                    report.kept_pinned += 1
                elif current.reason == "active_sibling":
                    report.kept_active += 1
                else:
                    report.candidates_unconfirmed.append(candidate)
        else:
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
