"""FastAPI routes — queue, published, sources, listings, metrics, reject / mark-posted actions."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select, update
from sqlalchemy.orm import selectinload

from src.admin.auth import require_admin
from src.admin.csrf import CSRF_TOKEN, require_csrf
from src.admin.leads_report import build_leads_report
from src.admin.publish import auto_publish_site_listing, get_or_create_site_listing
from src.db import (
    InboundPost,
    Listing,
    Match,
    MatchStatus,
    PublishedPost,
    SourceChannel,
    get_session,
)
from src.db.models import DailyMetrics, Lead, Platform, SiteListing, utcnow
from src.post import render_post

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["csrf_token"] = CSRF_TOKEN

router = APIRouter()


# --------------------------------------------------------------------------- Dashboard


@router.get("/", include_in_schema=False)
async def root(_: str = Depends(require_admin)) -> RedirectResponse:
    return RedirectResponse(url="/queue", status_code=status.HTTP_302_FOUND)


@router.get("/queue", response_class=HTMLResponse)
async def queue_view(request: Request, _: str = Depends(require_admin)) -> HTMLResponse:
    cards = await _load_pending_cards()
    return templates.TemplateResponse(
        request, "queue.html", {"cards": cards, "active_nav": "queue"}
    )


# Retention: a listing live on the public site this long should come down (warn-first, no auto-delete).
SITE_RETENTION_DAYS = 21
# Time windows for the «Опубликованные» list — «за неделю» for the one-time catch-up, «все» for everything.
_PUBLISHED_WINDOWS = {"week": 7, "3weeks": 21, "all": None}


@router.get("/published", response_class=HTMLResponse)
async def published_view(
    request: Request,
    window: str = Query("3weeks"),
    _: str = Depends(require_admin),
) -> HTMLResponse:
    """Delivered candidates + per-flat site controls — Дарья's real curation surface.

    /queue only holds PENDING matches, but delivery flips every match to PUBLISHED, so the
    candidates live here. Each row exposes the SiteListing toggles («На сайт» / «Квартира недели» /
    flags / note), whether it's live on the site, whether the source is «снято» (is_active=0), and
    a «пора убрать» flag once it has been on the site ≥ SITE_RETENTION_DAYS.
    """
    window = window if window in _PUBLISHED_WINDOWS else "3weeks"
    days = _PUBLISHED_WINDOWS[window]
    # SQLite returns naive datetimes; use naive-UTC `now` so age subtraction doesn't mix awareness.
    now = utcnow().replace(tzinfo=None)
    async with get_session() as s:
        stmt = (
            select(PublishedPost)
            .options(selectinload(PublishedPost.match).selectinload(Match.listing))
            .order_by(PublishedPost.published_at.desc())
            .limit(300)
        )
        if days is not None:
            stmt = stmt.where(PublishedPost.published_at >= now - timedelta(days=days))
        rows = (await s.execute(stmt)).scalars().all()
        match_ids = [p.match_id for p in rows if p.match_id]
        site_by_match: dict[int, SiteListing] = {}
        if match_ids:
            sl_rows = (
                await s.execute(select(SiteListing).where(SiteListing.match_id.in_(match_ids)))
            ).scalars().all()
            site_by_match = {sl.match_id: sl for sl in sl_rows}
        items = []
        for p in rows:
            listing = p.match.listing if p.match else None
            site = site_by_match.get(p.match_id)
            live = bool(site and site.is_public)
            # Age counts from when it was put on the site (created_at), only while it IS live.
            age_days = (now - site.created_at).days if (site is not None and live) else None
            items.append({
                "match_id": p.match_id,
                "tg_message_id": p.tg_message_id,
                "published_at": p.published_at,
                "platform": listing.platform.value if listing else "—",
                "listing_url": listing.url if listing else None,
                "price_eur": listing.price_eur if listing else None,
                "area_m2": listing.area_m2 if listing else None,
                "district": listing.district if listing else None,
                "is_rented": bool(listing and not listing.is_active),
                "site": site,
                "live": live,
                "age_days": age_days,
                "overdue": age_days is not None and age_days >= SITE_RETENTION_DAYS,
            })
    return templates.TemplateResponse(
        request,
        "published.html",
        {
            "items": items,
            "active_nav": "published",
            "window": window,
            "windows": list(_PUBLISHED_WINDOWS.keys()),
            "retention_days": SITE_RETENTION_DAYS,
        },
    )


@router.get("/sources", response_class=HTMLResponse)
async def sources_view(request: Request, _: str = Depends(require_admin)) -> HTMLResponse:
    async with get_session() as s:
        rows = (
            await s.execute(select(SourceChannel).order_by(SourceChannel.id))
        ).scalars().all()
    return templates.TemplateResponse(
        request, "sources.html", {"sources": rows, "active_nav": "sources"}
    )


@router.post("/sources/{source_id}/toggle")
async def toggle_source(
    source_id: int,
    _: str = Depends(require_admin),
    __: None = Depends(require_csrf),
) -> RedirectResponse:
    async with get_session() as s:
        row = await s.get(SourceChannel, source_id)
        if row is None:
            raise HTTPException(status_code=404, detail="Source not found")
        row.is_active = not row.is_active
    return RedirectResponse(url="/sources", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/listings", response_class=HTMLResponse)
async def listings_view(
    request: Request,
    platform: str | None = None,
    _: str = Depends(require_admin),
) -> HTMLResponse:
    async with get_session() as s:
        stmt = select(Listing).order_by(Listing.scraped_at.desc()).limit(100)
        if platform:
            from src.db.models import Platform as P
            try:
                stmt = stmt.where(Listing.platform == P(platform.upper()))
            except ValueError:
                pass
        rows = (await s.execute(stmt)).scalars().all()
    return templates.TemplateResponse(
        request,
        "listings.html",
        {"listings": rows, "platform_filter": platform, "active_nav": "listings"},
    )


# --------------------------------------------------------------------------- Match actions
# Status markup ONLY — these never touch Telegram. Publishing stays a manual
# copy-paste step for Дарья; the bot must never post to the public channel.


@router.post("/match/{match_id}/reject", response_class=HTMLResponse)
async def reject_action(
    request: Request,
    match_id: int,
    reason: str = Form(""),
    _: str = Depends(require_admin),
    __: None = Depends(require_csrf),
) -> HTMLResponse:
    match = await _set_match_status(match_id, MatchStatus.REJECTED, reason=reason or None)
    if match is None:
        return _flash_card(request, match_id, error=f"Match {match_id} не найден")
    return templates.TemplateResponse(
        request,
        "_card_rejected.html",
        {"match_id": match_id, "reason": match.rejection_reason or ""},
    )


@router.post("/match/{match_id}/mark-posted", response_class=HTMLResponse)
async def mark_posted_action(
    request: Request,
    match_id: int,
    _: str = Depends(require_admin),
    __: None = Depends(require_csrf),
) -> HTMLResponse:
    """For 'Дарья posted it manually, the bot doesn't know' — no PublishedPost row is created.

    Decision #3: mark-posted auto-exposes the apartment on the public site by upserting
    a SiteListing(is_public=True) with ВНЖ/белый-картон flags defaulted from the post text.
    Re-marking is idempotent: an EXISTING overlay keeps Дарья's manual flags/is_public/note.
    """
    async with get_session() as s:
        match = await s.get(Match, match_id)
        if match is None:
            return _flash_card(request, match_id, error=f"Match {match_id} не найден")
        match.status = MatchStatus.PUBLISHED
        match.rejection_reason = None
        # Same idempotent upsert + create-time flag defaulting the delivery hot path uses
        # (X1). is_public stays the DB default (True) — decision #3; an existing overlay keeps
        # Дарья's manual flags/is_public/note.
        await auto_publish_site_listing(s, match_id)
    return templates.TemplateResponse(
        request, "_card_posted.html", {"match_id": match_id}
    )


# ------------------------------------------------------------------------- Publish-to-site (W2-A1)
# Each route upserts the SiteListing overlay for a match and toggles/sets one field.
# All idempotent, all create the row if absent (toggles never run the text-parse —
# that default only applies at mark-posted; a manual toggle is an explicit choice).


def _site_controls(request: Request, match_id: int, site: SiteListing) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "_site_controls.html", {"match_id": match_id, "site": site}
    )


async def _require_match(s, match_id: int) -> Match:
    match = await s.get(Match, match_id)
    if match is None:
        raise HTTPException(status_code=404, detail=f"Match {match_id} not found")
    return match


@router.post("/match/{match_id}/site/toggle-public", response_class=HTMLResponse)
async def site_toggle_public(
    request: Request,
    match_id: int,
    _: str = Depends(require_admin),
    __: None = Depends(require_csrf),
) -> HTMLResponse:
    async with get_session() as s:
        await _require_match(s, match_id)
        site, created = await get_or_create_site_listing(s, match_id)
        # Absent → create exposed (is_public default True, decision #3); present → flip.
        if not created:
            site.is_public = not site.is_public
    # session commits on block exit; expire_on_commit=False keeps `site` readable here.
    return _site_controls(request, match_id, site)


@router.post("/match/{match_id}/site/toggle-vnj", response_class=HTMLResponse)
async def site_toggle_vnj(
    request: Request,
    match_id: int,
    _: str = Depends(require_admin),
    __: None = Depends(require_csrf),
) -> HTMLResponse:
    async with get_session() as s:
        await _require_match(s, match_id)
        site, _created = await get_or_create_site_listing(s, match_id)
        site.is_for_vnj = not site.is_for_vnj
    return _site_controls(request, match_id, site)


@router.post("/match/{match_id}/site/toggle-white-card", response_class=HTMLResponse)
async def site_toggle_white_card(
    request: Request,
    match_id: int,
    _: str = Depends(require_admin),
    __: None = Depends(require_csrf),
) -> HTMLResponse:
    async with get_session() as s:
        await _require_match(s, match_id)
        site, _created = await get_or_create_site_listing(s, match_id)
        site.is_for_white_card = not site.is_for_white_card
    return _site_controls(request, match_id, site)


@router.post("/match/{match_id}/site/toggle-featured", response_class=HTMLResponse)
async def site_toggle_featured(
    request: Request,
    match_id: int,
    _: str = Depends(require_admin),
    __: None = Depends(require_csrf),
) -> HTMLResponse:
    """«Квартира недели» flag — EXCLUSIVE: at most one featured listing at a time.

    Turning a listing on first clears is_featured on every OTHER overlay (so the hero card is
    deterministic), then sets this one. Turning off just clears this one. Upsert; creating via
    this toggle never runs the text-parse and leaves other fields at their DB defaults."""
    async with get_session() as s:
        await _require_match(s, match_id)
        site, _created = await get_or_create_site_listing(s, match_id)
        if site.is_featured:
            site.is_featured = False
        else:
            # Exclusivity: demote any other currently-featured listing before promoting this one.
            await s.execute(
                update(SiteListing)
                .where(SiteListing.id != site.id, SiteListing.is_featured.is_(True))
                .values(is_featured=False)
            )
            site.is_featured = True
    return _site_controls(request, match_id, site)


@router.post("/match/{match_id}/site/meta", response_class=HTMLResponse)
async def site_meta(
    request: Request,
    match_id: int,
    channel_post_url: str = Form(""),
    note: str = Form(""),
    _: str = Depends(require_admin),
    __: None = Depends(require_csrf),
) -> HTMLResponse:
    async with get_session() as s:
        await _require_match(s, match_id)
        site, _created = await get_or_create_site_listing(s, match_id)
        # Empty string clears the override; flags/is_public are left untouched.
        site.channel_post_url = channel_post_url.strip() or None
        site.note = note.strip() or None
    return _site_controls(request, match_id, site)


# --------------------------------------------------------------------------- Metrics


@router.get("/metrics", response_class=HTMLResponse)
async def metrics_view(request: Request, _: str = Depends(require_admin)) -> HTMLResponse:
    async with get_session() as s:
        rows = (
            await s.execute(
                select(DailyMetrics).order_by(DailyMetrics.snapshot_date, DailyMetrics.platform)
            )
        ).scalars().all()
    labels = sorted({r.snapshot_date for r in rows})
    by_platform: dict[str, dict[str, int]] = {}
    for r in rows:
        by_platform.setdefault(r.platform, {})[r.snapshot_date] = r.active_count
    chart = {
        "labels": labels,
        "datasets": [
            # None → null in JSON: Chart.js skips missing days instead of drawing 0.
            {"label": platform, "data": [points.get(d) for d in labels]}
            for platform, points in sorted(by_platform.items())
        ],
    }
    latest = sorted(rows, key=lambda r: (r.snapshot_date, r.platform), reverse=True)[:30]
    return templates.TemplateResponse(
        request,
        "metrics.html",
        {"chart": chart, "latest": latest, "active_nav": "metrics"},
    )


# --------------------------------------------------------------------------- Health dashboard (B3)
# One read-only «всё ли живо» screen for Дарья: per-platform scraper liveness + monitor
# staleness, built ONLY from signals that already exist (no new table, no migration).

# Status thresholds (module constants so the rules live in one place and tests can import them).
SCRAPE_STALE_WARN_DAYS = 2  # last scrape older than this → ⚠️ (площадка молчит)
DEACTIVATED_WINDOW_HOURS = 24  # how far back «deactivated за сутки» counts
TREND_DAYS = 7  # DailyMetrics active-count window shown as the 7-day trend
INBOUND_STALE_WARN_HOURS = 24  # no inbound newer than this → ⚠️ (монитор тихий)


@dataclass
class HealthReport:
    """Aggregated liveness snapshot rendered by /health (and unit-tested directly)."""

    platforms: list[dict] = field(default_factory=list)
    monitor: dict = field(default_factory=dict)
    fingerprint: dict = field(default_factory=dict)
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def _as_aware(dt: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; treat them as UTC so age math never mixes awareness."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _read_fingerprint_state() -> dict:
    """Last-known-good structure-fingerprint baseline (A2), read defensively.

    The file (`data/state/parser_fingerprints.json`) may be absent (smoke probe never ran) or
    corrupt — either way we degrade to «n/a» rather than crashing the dashboard.
    """
    from src.config import get_settings

    path = get_settings().data_dir / "state" / "parser_fingerprints.json"
    if not path.exists():
        return {"present": False, "mtime": None, "platforms": []}
    info: dict = {"present": True, "mtime": None, "platforms": []}
    try:
        info["mtime"] = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        info["mtime"] = None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            info["platforms"] = sorted(str(k) for k in data.keys())
    except (json.JSONDecodeError, OSError):
        # corrupt/unreadable baseline: file is there but we can't trust its contents
        info["platforms"] = []
    return info


def _scrape_status(last_seen: datetime | None, active_count: int, now: datetime) -> str:
    """ok / warn / crit for one platform's scraper liveness."""
    if active_count == 0 and last_seen is None:
        return "crit"  # площадка вообще ничего не отдала — почти наверняка сломана
    if last_seen is None:
        return "warn"
    if (now - last_seen) > timedelta(days=SCRAPE_STALE_WARN_DAYS):
        return "warn"
    return "ok"


async def build_health(s) -> HealthReport:
    """Gather per-platform + monitor liveness from existing tables / state.

    Per platform we show ONLY persisted signals:
      * last_seen_at  = MAX(Listing.last_seen_at)            — last-successful-scrape proxy
      * active_count  = COUNT(is_active=True)                — current active inventory
      * trend         = DailyMetrics.active_count over TREND_DAYS (oldest→newest)
      * deactivated_24h = COUNT(is_active=False AND details_refreshed_at within the window)
    Parse-success-rate (B1) is computed per-run in memory and is NOT persisted, so it is
    intentionally OMITTED here (no table to read it from — we don't fabricate it).
    """
    now = datetime.now(timezone.utc)
    cutoff_window = now - timedelta(hours=DEACTIVATED_WINDOW_HOURS)
    today = now.date()
    trend_floor = (today - timedelta(days=TREND_DAYS - 1)).isoformat()

    platforms: list[dict] = []
    for plat in Platform:
        last_seen = _as_aware(
            (
                await s.execute(
                    select(func.max(Listing.last_seen_at)).where(Listing.platform == plat)
                )
            ).scalar_one_or_none()
        )
        active_count = (
            await s.execute(
                select(func.count())
                .select_from(Listing)
                .where(Listing.platform == plat, Listing.is_active.is_(True))
            )
        ).scalar_one() or 0
        deactivated_24h = (
            await s.execute(
                select(func.count())
                .select_from(Listing)
                .where(
                    Listing.platform == plat,
                    Listing.is_active.is_(False),
                    Listing.details_refreshed_at.is_not(None),
                    Listing.details_refreshed_at >= cutoff_window,
                )
            )
        ).scalar_one() or 0
        # 7-day trend: DailyMetrics stores platform as Platform.value (e.g. "NEKRETNINE").
        trend_rows = (
            await s.execute(
                select(DailyMetrics.active_count)
                .where(
                    DailyMetrics.platform == plat.value,
                    DailyMetrics.snapshot_date >= trend_floor,
                )
                .order_by(DailyMetrics.snapshot_date)
            )
        ).scalars().all()
        platforms.append(
            {
                "platform": plat.value,
                "last_seen_at": last_seen,
                "active_count": active_count,
                "deactivated_24h": deactivated_24h,
                "trend": list(trend_rows),
                "status": _scrape_status(last_seen, active_count, now),
            }
        )

    # --- monitor: how long since the last inbound post arrived ---
    last_inbound = _as_aware(
        (await s.execute(select(func.max(InboundPost.received_at)))).scalar_one_or_none()
    )
    if last_inbound is None:
        monitor = {"last_inbound_at": None, "age_hours": None, "status": "crit"}
    else:
        age_hours = (now - last_inbound).total_seconds() / 3600.0
        monitor = {
            "last_inbound_at": last_inbound,
            "age_hours": age_hours,
            "status": "ok" if age_hours <= INBOUND_STALE_WARN_HOURS else "warn",
        }

    return HealthReport(
        platforms=platforms,
        monitor=monitor,
        fingerprint=_read_fingerprint_state(),
        now=now,
    )


@router.get("/health", response_class=HTMLResponse)
async def health_view(request: Request, _: str = Depends(require_admin)) -> HTMLResponse:
    async with get_session() as s:
        report = await build_health(s)
    return templates.TemplateResponse(
        request,
        "health.html",
        {"report": report, "active_nav": "health"},
    )


# --------------------------------------------------------------------------- Leads inbox (W4-1)
# Flat convention (like /queue, /metrics): GET /leads lists requests from the public
# lead form; POST /leads/{id}/status is the CSRF-guarded single status mutation.

LEAD_STATUSES = ("new", "contacted", "closed")


@router.get("/leads", response_class=HTMLResponse)
async def leads_view(
    request: Request,
    status_filter: str | None = Query(None, alias="status"),
    _: str = Depends(require_admin),
) -> HTMLResponse:
    # `status_filter` aliases ?status= so it never shadows the imported fastapi `status`.
    # Unknown values are ignored (no 500) — the filter UI only emits valid statuses.
    active_filter = status_filter if status_filter in LEAD_STATUSES else None
    async with get_session() as s:
        stmt = select(Lead).order_by(Lead.created_at.desc())  # newest first
        if active_filter:
            stmt = stmt.where(Lead.status == active_filter)
        rows = (await s.execute(stmt)).scalars().all()
        # Demand analytics (F1) always reflect the WHOLE table, not the status-filtered view —
        # the funnel/district/budget report is about overall demand, not the current sub-list.
        all_leads = (
            rows if not active_filter
            else (await s.execute(select(Lead))).scalars().all()
        )
    report = build_leads_report(all_leads)
    return templates.TemplateResponse(
        request,
        "leads.html",
        {
            "leads": rows,
            "active_nav": "leads",
            "active_filter": active_filter,
            "statuses": LEAD_STATUSES,
            "report": report,
        },
    )


@router.post("/leads/{lead_id}/status")
async def lead_status_action(
    lead_id: int,
    new_status: str = Form(..., alias="status"),
    _: str = Depends(require_admin),
    __: None = Depends(require_csrf),
) -> RedirectResponse:
    # Validate before any DB write — an out-of-range status is a 400, never persisted.
    if new_status not in LEAD_STATUSES:
        raise HTTPException(status_code=400, detail="Invalid status")
    async with get_session() as s:
        lead = await s.get(Lead, lead_id)
        if lead is None:
            raise HTTPException(status_code=404, detail="Lead not found")
        lead.status = new_status
    return RedirectResponse(url="/leads", status_code=status.HTTP_303_SEE_OTHER)



@router.get("/photo/inbound/{inbound_id}/{idx}")
async def inbound_photo(
    inbound_id: int, idx: int, _: str = Depends(require_admin)
) -> FileResponse:
    async with get_session() as s:
        ib = await s.get(InboundPost, inbound_id)
    if ib is None or idx >= len(ib.photo_paths):
        raise HTTPException(status_code=404)
    path = Path(ib.photo_paths[idx])
    if not path.exists():
        raise HTTPException(status_code=404)
    return FileResponse(path)


@router.get("/photo/listing/{listing_id}/{idx}")
async def listing_photo(
    listing_id: int, idx: int, _: str = Depends(require_admin)
) -> FileResponse:
    """Serve a downloaded listing photo from disk (if hash-listings was run)."""
    from src.config import get_settings

    async with get_session() as s:
        listing = await s.get(Listing, listing_id)
    if listing is None or idx >= len(listing.photo_urls):
        raise HTTPException(status_code=404)
    settings = get_settings()
    target_dir = settings.listings_photos_dir / listing.platform.value / listing.external_id
    # files are saved as 000.webp/jpg by orchestrator + hash-listings
    for ext in (".webp", ".jpg", ".jpeg", ".png"):
        candidate = target_dir / f"{idx:03d}{ext}"
        if candidate.exists():
            return FileResponse(candidate)
    raise HTTPException(status_code=404)


# --------------------------------------------------------------------------- Helpers


async def _set_match_status(
    match_id: int, target: MatchStatus, *, reason: str | None = None
) -> Match | None:
    """DB-only status markup. Idempotent: re-applying the same status is a no-op
    (an empty re-POST never clears a stored rejection_reason). Single-admin tool,
    so cross transitions (e.g. fixing a misclick REJECTED → PUBLISHED) just win.
    """
    async with get_session() as s:
        match = await s.get(Match, match_id)
        if match is None:
            return None
        match.status = target
        if target is MatchStatus.REJECTED:
            if reason:
                match.rejection_reason = reason
        else:
            match.rejection_reason = None
    return match


async def _load_pending_cards() -> list[dict[str, object]]:
    async with get_session() as s:
        rows = (
            await s.execute(
                select(Match)
                .options(
                    selectinload(Match.inbound_post).selectinload(InboundPost.source_channel),
                    selectinload(Match.listing),
                )
                .where(Match.status == MatchStatus.PENDING)
                .order_by(Match.created_at.desc())
            )
        ).scalars().all()
        # Per-match SiteListing overlay (or None) for surfacing the «на сайт» toggles.
        site_by_match: dict[int, SiteListing] = {}
        site_rows = (
            await s.execute(
                select(SiteListing).where(SiteListing.match_id.in_([m.id for m in rows]))
            )
        ).scalars().all() if rows else []
        for sr in site_rows:
            site_by_match[sr.match_id] = sr
    cards: list[dict[str, object]] = []
    for m in rows:
        listing: Listing | None = m.listing
        inbound: InboundPost | None = m.inbound_post
        if listing is None or inbound is None:
            continue
        rendered = render_post(listing, inbound)
        inbound_url = _inbound_post_url(inbound)
        cards.append({
            "site": site_by_match.get(m.id),
            "match_id": m.id,
            "confidence": m.confidence,
            "matched_photo_count": m.matched_photo_count,
            "method": m.method,
            "inbound_id": inbound.id,
            "inbound_photo_count": len(inbound.photo_paths),
            "inbound_text": inbound.raw_text,
            "inbound_url": inbound_url,
            "post_date": inbound.post_date,
            "listing_id": listing.id,
            "platform": listing.platform.value,
            "listing_url": listing.url,
            "price_eur": listing.price_eur,
            "area_m2": listing.area_m2,
            "rooms": listing.rooms,
            "district": listing.district,
            "address_raw": listing.address_raw,
            "listing_title": listing.title,
            "listing_photo_count": len(listing.photo_urls),
            "post_text": rendered.text,
        })
    return cards


def _inbound_post_url(inbound: InboundPost) -> str | None:
    """Reconstruct https://t.me/<username>/<msg_id> if we know the username."""
    src = inbound.source_channel
    if src is None or not src.username:
        return None
    return f"https://t.me/{src.username}/{inbound.tg_message_id}"


def _flash_card(request: Request, match_id: int, *, error: str) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "_card_error.html",
        {"match_id": match_id, "error": error},
        status_code=400,
    )
