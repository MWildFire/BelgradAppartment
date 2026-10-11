"""Authorize existing /photos URLs against current public overlays before every GET/HEAD.

The inbound tree also contains private posts. Files are never authorized by knowing a URL,
by source activity or by global dedup status; a public overlay must reference their original.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select

from src.db.models import InboundPost, Match, MatchStatus, SiteListing
from src.site import db as sitedb
from src.site.photos import VARIANT_WIDTHS, _resolve_stored, photos_root

router = APIRouter()
_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def _permitted_file(path: str, stored_paths: list[str]) -> Path | None:
    if not path or "\\" in path or any(p in ("", ".", "..") for p in path.split("/")):
        return None
    root = photos_root()
    target = root / path
    try:
        target.resolve().relative_to(root)
    except (OSError, ValueError):
        return None
    if target.suffix.lower() not in _IMAGE_EXTENSIONS or not target.is_file():
        return None
    for stored in stored_paths:
        resolved = _resolve_stored(stored, root)
        if resolved is None:
            continue
        _, rel = resolved
        if path == rel or path in {f"{rel}.{kind}.webp" for kind in VARIANT_WIDTHS}:
            return target
    return None


@router.api_route("/photos/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
async def public_photo(path: str) -> FileResponse:
    async with sitedb.read_session() as session:
        records = (await session.execute(
            select(InboundPost.photo_paths)
            .join(Match, Match.inbound_post_id == InboundPost.id)
            .join(SiteListing, SiteListing.match_id == Match.id)
            # The same rule as the catalog: photos of a match are public only once it is PUBLISHED.
            .where(SiteListing.is_public.is_(True), Match.status == MatchStatus.PUBLISHED)
        )).scalars().all()
    stored = [p for paths in records for p in paths if p]
    target = await asyncio.to_thread(_permitted_file, path, stored)
    if target is None:
        raise HTTPException(status_code=404, detail="Not found")
    # No cached authorization survives a later unpublish. Existing third-party/browser copies
    # cannot be recalled; operator must separately purge any prior immutable edge cache.
    return FileResponse(target, headers={"Cache-Control": "no-store"})
