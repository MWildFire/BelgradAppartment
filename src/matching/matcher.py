"""Match inbound posts (Telegram) against scraped listings (platforms).

**Two-stage algorithm** (replaces the old pHash-only matcher — too brittle for watermarks):

Stage 1 — Fast pHash pre-screen (best-effort optimisation):
- For each inbound photo, count listings with at least one photo whose pHash Hamming
  distance is ≤ `PHASH_MAX_HAMMING`. If any listing collects ≥2 such hits, we still
  validate with CLIP, but the candidate pool is already narrowed.
- Most matches don't need stage 2 — pHash alone catches bit-identical re-uploads.

Stage 2 — CLIP cosine similarity (main signal):
- Each photo has a 512-dim CLIP ViT-B/32 embedding stored as a BLOB in the DB.
- For every (inbound, listing) pair, compute pairwise cosine similarity matrix
  (cheap: O(Ni·Nl·512) ≈ negligible).
- An inbound photo "matches" if its best similarity to any listing photo is ≥ `CLIP_SIMILARITY_THRESHOLD` (default 0.85).
- If ≥ `MIN_MATCHED_PHOTOS` inbound photos have a match, the listing is a candidate.
- Among candidates, the listing with the highest aggregate score wins.

CLIP is robust to watermarks (nekretnine's logo overlay), JPEG re-encoding, mild cropping,
and brightness shifts — which is exactly how owners' photos differ from platform-uploaded versions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select

from src.config import get_settings
from src.db import (
    InboundPost,
    InboundStatus,
    Listing,
    Match,
    MatchStatus,
    get_session,
)
from src.matching.clip_embed import unpack_embeddings
from src.matching.inbound_parser import ParsedInbound, parse_inbound_text
from src.matching.phash import hamming_distance

logger = logging.getLogger(__name__)

# Status values for MatchResult.status
STATUS_MATCHED = "matched"
STATUS_NO_MATCH = "no_match"

# Method markers for MatchResult.method — describe HOW the candidate was chosen / rejected.
METHOD_PHASH = "phash"
METHOD_CLIP = "clip"
METHOD_CLIP_LLM = "clip+llm"
METHOD_CLIP_LLM_REJECTED = "clip+llm_rejected"


@dataclass
class MatchResult:
    inbound_id: int
    listing_id: int | None
    matched_photos: int
    confidence: float
    status: str  # STATUS_MATCHED | STATUS_NO_MATCH
    match_id: int | None = None
    method: str = METHOD_CLIP


async def match_inbound(inbound_id: int) -> MatchResult:
    """Run the multi-stage matcher for one inbound post; persist Match on success.

    Pipeline: pHash pre-screen → CLIP cosine → text cross-check → (optional) Claude LLM vision check.
    """
    settings = get_settings()
    clip_thr = settings.clip_similarity_threshold
    min_photos = settings.min_matched_photos
    phash_thr = settings.phash_max_hamming

    async with get_session() as s:
        inbound = await s.get(InboundPost, inbound_id)
        if inbound is None:
            raise ValueError(f"InboundPost {inbound_id} not found")
        if not inbound.photo_phashes:
            inbound.status = InboundStatus.NO_MATCH
            return MatchResult(inbound_id, None, 0, 0.0, STATUS_NO_MATCH)

        existing = (
            await s.execute(
                select(Match)
                .where(Match.inbound_post_id == inbound_id)
                .order_by(Match.matched_photo_count.desc())
            )
        ).scalars().first()
        if existing is not None:
            return MatchResult(
                inbound_id, existing.listing_id, existing.matched_photo_count,
                existing.confidence, STATUS_MATCHED, match_id=existing.id, method=existing.method,
            )

        listings = (
            await s.execute(select(Listing).where(Listing.is_active.is_(True)))
        ).scalars().all()

        inbound_phashes = list(inbound.photo_phashes)
        inbound_embs = unpack_embeddings(inbound.photo_embeddings) if inbound.photo_embeddings else []

        # Parse price/area from inbound caption — used to reject CLIP false-positives.
        parsed = parse_inbound_text(inbound.raw_text)

        best_listing: Listing | None = None
        best_count = 0
        best_score = 0.0
        best_method = METHOD_CLIP

        for listing in listings:
            if not listing.photo_phashes and not listing.photo_embeddings:
                continue

            # Stage 1 — pHash pre-screen. If pHash already finds enough matches, trust it.
            ph_count = _count_phash_matches(
                inbound_phashes, list(listing.photo_phashes or []), phash_thr
            )

            # Stage 2 — CLIP, if embeddings are available on both sides.
            clip_count, clip_score = 0, 0.0
            if inbound_embs and listing.photo_embeddings:
                listing_embs = unpack_embeddings(listing.photo_embeddings)
                if listing_embs:
                    clip_count, clip_score = _clip_match(
                        inbound_embs, listing_embs, clip_thr
                    )

            # Pick the stronger signal per listing.
            if clip_count > ph_count or (
                clip_count == ph_count and clip_score > best_score
            ):
                cand_count, cand_score, cand_method = clip_count, clip_score, METHOD_CLIP
            else:
                cand_count, cand_score, cand_method = (
                    ph_count, float(ph_count) / max(len(inbound_phashes), 1), METHOD_PHASH
                )

            # Stage 3 — text cross-check (rejects CLIP false positives between unrelated apartments
            # that happen to have similar-looking interiors). Only applies to CLIP-based candidates;
            # pHash with ≥2 matches is already strong enough on its own.
            if cand_method == METHOD_CLIP and not _passes_text_check(parsed, listing, settings):
                continue

            # Track best across listings (prefer higher count; tie-break by score).
            if cand_count > best_count or (
                cand_count == best_count and cand_score > best_score
            ):
                best_listing = listing
                best_count = cand_count
                best_score = cand_score
                best_method = cand_method

        if best_listing is None or best_count < min_photos:
            inbound.status = InboundStatus.NO_MATCH
            logger.info(
                "[match] inbound=%s NO_MATCH (best_count=%d listings=%d threshold=%d phash≤%d clip≥%.2f)",
                inbound_id, best_count, len(listings), min_photos, phash_thr, clip_thr,
            )
            return MatchResult(inbound_id, None, best_count, best_score, STATUS_NO_MATCH, method=best_method)

        # Stage 4 — LLM final validation. Reads inbound + listing photos from disk and asks Claude
        # whether they show the same apartment. Catches false positives that pass CLIP + text checks.
        llm_method = best_method
        if settings.enable_llm_validation and best_method == METHOD_CLIP:
            llm_ok = await _run_llm_validation(inbound, best_listing, settings)
            if llm_ok is False:
                inbound.status = InboundStatus.NO_MATCH
                logger.info(
                    "[match] inbound=%s REJECTED by LLM (CLIP candidate was listing=%s/%s)",
                    inbound_id, best_listing.platform.value, best_listing.external_id,
                )
                return MatchResult(
                    inbound_id, None, best_count, best_score, STATUS_NO_MATCH, method=METHOD_CLIP_LLM_REJECTED,
                )
            if llm_ok is True:
                llm_method = METHOD_CLIP_LLM

        confidence = round(best_score if best_method == METHOD_CLIP else best_count / max(len(inbound_phashes), 1), 3)
        match = Match(
            inbound_post_id=inbound_id,
            listing_id=best_listing.id,
            confidence=confidence,
            matched_photo_count=best_count,
            method=llm_method,
            status=MatchStatus.PENDING,
        )
        s.add(match)
        inbound.status = InboundStatus.MATCHED
        await s.flush()
        await s.refresh(match)
        logger.info(
            "[match] inbound=%s ↔ listing=%s/%s (matched=%d/%d, conf=%.2f, method=%s)",
            inbound_id, best_listing.platform.value, best_listing.external_id,
            best_count, len(inbound_phashes), confidence, llm_method,
        )
        return MatchResult(
            inbound_id, best_listing.id, best_count, confidence, STATUS_MATCHED,
            match_id=match.id, method=llm_method,
        )


async def _run_llm_validation(
    inbound: InboundPost, listing: Listing, settings
) -> bool | None:
    """Call Claude vision API to confirm the match. Returns True/False, or None on transport error
    (in which case we keep the CLIP candidate — fail-open rather than drop a possibly-good match).
    """
    from pathlib import Path

    from src.matching.llm_validator import LLMValidatorError, validate_match

    inbound_paths = [Path(p) for p in (inbound.photo_paths or [])]
    listing_photos_dir = (
        settings.listings_photos_dir / listing.platform.value / listing.external_id
    )
    listing_paths: list[Path] = []
    if listing_photos_dir.exists():
        listing_paths = sorted(listing_photos_dir.glob("*.*"))

    if not inbound_paths or not listing_paths:
        logger.warning(
            "[match] LLM validation skipped — missing photos on disk (inbound=%d listing=%d)",
            len(inbound_paths), len(listing_paths),
        )
        return None

    try:
        result = await validate_match(
            inbound_paths,
            listing_paths,
            inbound_text=inbound.raw_text,
            listing_meta={
                "platform": listing.platform.value,
                "price_eur": listing.price_eur,
                "area_m2": listing.area_m2,
                "rooms": listing.rooms,
                "district": listing.district,
                "address_raw": listing.address_raw,
                "title": listing.title,
            },
        )
    except LLMValidatorError as e:
        logger.warning("[match] LLM validator failed (kept CLIP verdict): %s", e)
        return None
    except Exception:
        logger.exception("[match] LLM validator crashed (kept CLIP verdict)")
        return None

    if not result.is_same_apartment:
        return False
    if result.confidence < settings.llm_validation_min_confidence:
        logger.info(
            "[match] LLM said same but confidence %.2f < threshold %.2f → reject",
            result.confidence, settings.llm_validation_min_confidence,
        )
        return False
    return True


async def match_all_pending() -> list[MatchResult]:
    """Run matcher for every InboundPost in NEW status. Useful for batch backfill."""
    async with get_session() as s:
        ids = list(
            (
                await s.execute(
                    select(InboundPost.id)
                    .where(InboundPost.status == InboundStatus.NEW)
                    .order_by(InboundPost.received_at)
                )
            ).scalars().all()
        )
    results: list[MatchResult] = []
    for inbound_id in ids:
        results.append(await match_inbound(inbound_id))
    return results


def _count_phash_matches(
    inbound_hashes: list[str], listing_hashes: list[str], max_hamming: int
) -> int:
    """How many inbound photos have at least one near-duplicate in the listing (pHash)."""
    if not inbound_hashes or not listing_hashes:
        return 0
    count = 0
    for ih in inbound_hashes:
        for lh in listing_hashes:
            try:
                if hamming_distance(ih, lh) <= max_hamming:
                    count += 1
                    break
            except (ValueError, TypeError):
                continue
    return count


def _clip_match(
    inbound_embs: list, listing_embs: list, threshold: float
) -> tuple[int, float]:
    """Pairwise cosine similarity, count inbound photos with best-listing-sim ≥ threshold.

    Returns (count, mean_top_similarity).
    """
    import numpy as np

    A = np.stack(inbound_embs)  # (Ni, 512)
    B = np.stack(listing_embs)  # (Nl, 512)
    sim = A @ B.T  # (Ni, Nl)
    per_inbound_max = sim.max(axis=1)
    count = int((per_inbound_max >= threshold).sum())
    mean_top = float(per_inbound_max.mean())
    return count, mean_top


def _passes_text_check(parsed: ParsedInbound, listing: Listing, settings) -> bool:
    """Reject candidate if inbound caption is incompatible with the listing.

    Permissive on missing data (one side unknown → pass). Three signals:
    - Price within ±cross_check_price_tolerance_pct (default 25%).
    - Area within ±cross_check_area_tolerance_pct (default 20%).
    - District (if inbound mentions any) overlaps with the listing's district tag.

    All known signals must pass. Unknown signals don't penalize.
    """
    if listing.price_eur and parsed.price_eur:
        ratio = abs(listing.price_eur - parsed.price_eur) / max(parsed.price_eur, 1)
        if ratio > settings.cross_check_price_tolerance_pct:
            return False
    # Hashtag bucket like "#до1500" — listing must be ≤ bucket + small slack.
    if listing.price_eur and parsed.price_bucket_max:
        if listing.price_eur > parsed.price_bucket_max * 1.05:
            return False
    if listing.area_m2 and parsed.area_m2:
        ratio = abs(listing.area_m2 - parsed.area_m2) / max(parsed.area_m2, 1)
        if ratio > settings.cross_check_area_tolerance_pct:
            return False
    # District: if inbound names any district, the listing must be in one of them.
    if parsed.districts and listing.district:
        from src.post.hashtags import district_tag

        tag = district_tag(listing.district)
        listing_district = tag.lstrip("#") if tag else None
        if listing_district and listing_district not in parsed.districts:
            return False
    return True
