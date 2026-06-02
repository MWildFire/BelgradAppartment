"""FastAPI routes — queue, published, sources, listings, publish / reject actions."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from src.admin.auth import require_admin
from src.db import (
    InboundPost,
    Listing,
    Match,
    MatchStatus,
    PublishedPost,
    SourceChannel,
    get_session,
)
from src.post import render_post
from src.telegram.delivery import DeliveryError, reject_match

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

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
async def toggle_source(source_id: int, _: str = Depends(require_admin)) -> RedirectResponse:
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


@router.post("/match/{match_id}/reject", response_class=HTMLResponse)
async def reject_action(
    request: Request,
    match_id: int,
    reason: str = Form(""),
    _: str = Depends(require_admin),
) -> HTMLResponse:
    try:
        await reject_match(match_id, reason or None)
    except DeliveryError as e:
        return _flash_card(request, match_id, error=str(e))
    return templates.TemplateResponse(
        request, "_card_rejected.html", {"match_id": match_id, "reason": reason}
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
