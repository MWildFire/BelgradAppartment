"""FastAPI routes — queue, published, sources, listings, metrics, reject / mark-posted actions."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from src.admin.auth import require_admin
from src.admin.csrf import CSRF_TOKEN, require_csrf
from src.db import (
    InboundPost,
    Listing,
    Match,
    MatchStatus,
    PublishedPost,
    SourceChannel,
    get_session,
)
from src.db.models import DailyMetrics
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


@router.get("/published", response_class=HTMLResponse)
async def published_view(request: Request, _: str = Depends(require_admin)) -> HTMLResponse:
    async with get_session() as s:
        rows = (
            await s.execute(
                select(PublishedPost)
                .options(
                    selectinload(PublishedPost.match)
                    .selectinload(Match.listing)
                )
                .order_by(PublishedPost.published_at.desc())
                .limit(200)
            )
        ).scalars().all()
        items = []
        for p in rows:
            listing = p.match.listing if p.match else None
            items.append({
                "id": p.id,
                "tg_message_id": p.tg_message_id,
                "published_at": p.published_at,
                "post_text": p.post_text,
                "platform": listing.platform.value if listing else "—",
                "listing_url": listing.url if listing else None,
                "price_eur": listing.price_eur if listing else None,
                "area_m2": listing.area_m2 if listing else None,
            })
    return templates.TemplateResponse(
        request, "published.html", {"items": items, "active_nav": "published"}
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
    """For 'Дарья posted it manually, the bot doesn't know' — no PublishedPost row is created."""
    match = await _set_match_status(match_id, MatchStatus.PUBLISHED)
    if match is None:
        return _flash_card(request, match_id, error=f"Match {match_id} не найден")
    return templates.TemplateResponse(
        request, "_card_posted.html", {"match_id": match_id}
    )


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


# --------------------------------------------------------------------------- Photo proxy


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
    cards: list[dict[str, object]] = []
    for m in rows:
        listing: Listing | None = m.listing
        inbound: InboundPost | None = m.inbound_post
        if listing is None or inbound is None:
            continue
        rendered = render_post(listing, inbound)
        inbound_url = _inbound_post_url(inbound)
        cards.append({
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
