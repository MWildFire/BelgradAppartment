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

import asyncio
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
from src.matching.candidates import (
    CandidateCache,
    CandidateRow,
    candidate_scan_select,
    load_candidate_cache,
    parse_phashes,
    scan_rows_from_db,
)
from src.matching.clip_embed import unpack_embeddings
from src.matching.inbound_parser import ParsedInbound, parse_inbound_text

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


async def match_inbound(inbound_id: int, cache: CandidateCache | None = None) -> MatchResult:
    """Run the multi-stage matcher for one inbound post; persist Match on success.

    Pipeline: pHash pre-screen → CLIP cosine → text cross-check → (optional) Claude LLM vision check.

    `cache` — optional batch-level candidate snapshot (see candidates.py); default None keeps
    the per-call candidates SELECT exactly as before (the live monitor path is unchanged).
    The CPU scan itself always runs in a worker thread so the event loop stays responsive.
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

        inbound_phashes = list(inbound.photo_phashes)
        inbound_embs = unpack_embeddings(inbound.photo_embeddings) if inbound.photo_embeddings else []

        # Early exit: matched_photo_count can never exceed the number of inbound photos, so an
        # inbound with fewer photos than min_matched_photos is a guaranteed NO_MATCH — skip the
        # full listing scan (which costs seconds across ~20k active listings).
        if max(len(inbound_phashes), len(inbound_embs)) < min_photos:
            inbound.status = InboundStatus.NO_MATCH
            logger.info(
                "[match] inbound=%s NO_MATCH (only %d photo(s) < min_matched_photos=%d — scan skipped)",
                inbound_id, max(len(inbound_phashes), len(inbound_embs)), min_photos,
            )
            return MatchResult(inbound_id, None, 0, 0.0, STATUS_NO_MATCH)

        if cache is not None:
            rows = cache.rows
        else:
            # Lightweight scan rows only — photo_embeddings BLOBs (~20 KB/row, hundreds of MB
            # total) are fetched separately in chunks during stage 2, never all at once.
            db_rows = (await s.execute(candidate_scan_select())).all()
            # Hex→int pHash parsing over ~20k rows is CPU — off the loop like the scan stages.
            rows = await asyncio.to_thread(scan_rows_from_db, db_rows)

        # Parse price/area from inbound caption — used to reject CLIP false-positives.
        parsed = parse_inbound_text(inbound.raw_text)

        # Stage 1 — pHash pre-screen for every listing. Plain 64-bit XOR+popcount: identical
        # result to imagehash's hamming distance but ~40x faster — the old per-call
        # hex_to_hash() parsing froze the event loop for 40-75s per inbound at 20k listings.
        # Runs in a worker thread (pure CPU, no session) so the live monitor keeps ticking.
        inbound_ints = parse_phashes(inbound_phashes)
        ph_counts = await asyncio.to_thread(_phash_prescreen, inbound_ints, rows, phash_thr)

        # Stage 2 — CLIP cosine for every listing that has embeddings: BLOBs fetched on the
        # loop in bounded chunks, unpack + matmul per chunk handed to a worker thread.
        clip_results: dict[int, tuple[int, float]] = {}
        if inbound_embs:
            emb_ids = [row.id for row in rows if row.has_embs]
            for start in range(0, len(emb_ids), _CLIP_CHUNK):
                chunk_ids = emb_ids[start:start + _CLIP_CHUNK]
                blob_rows = (
                    await s.execute(
                        select(Listing.id, Listing.photo_embeddings)
                        .where(Listing.id.in_(chunk_ids))
                    )
                ).all()
                clip_results.update(
                    await asyncio.to_thread(_clip_score_chunk, inbound_embs, blob_rows, clip_thr)
                )

        # Stage 3 + winner selection — pure CPU, also off the loop.
        best_id, best_count, best_score, best_method = await asyncio.to_thread(
            _select_best, rows, ph_counts, clip_results, parsed, len(inbound_phashes), settings,
        )

        best_listing: Listing | None = (
            await s.get(Listing, best_id) if best_id is not None else None
        )
        if best_listing is None or best_count < min_photos:
            inbound.status = InboundStatus.NO_MATCH
            logger.info(
                "[match] inbound=%s NO_MATCH (best_count=%d listings=%d threshold=%d phash≤%d clip≥%.2f)",
                inbound_id, best_count, len(rows), min_photos, phash_thr, clip_thr,
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
            inbound_id=inbound.id,
            listing_id=listing.id,
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


async def match_all_pending(cache: CandidateCache | None = None) -> list[MatchResult]:
    """Run matcher for every InboundPost in NEW status. Useful for batch backfill.

    Builds the candidate snapshot ONCE (unless the caller already passes one), so a batch
    of N inbounds does a single candidates SELECT instead of N full table scans.
    """
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
        if ids and cache is None:
            cache = await load_candidate_cache(s)
    results: list[MatchResult] = []
    for inbound_id in ids:
        results.append(await match_inbound(inbound_id, cache=cache))
    return results


# Listings per photo_embeddings fetch — bounds peak BLOB memory to ~chunk × ~20 KB.
_CLIP_CHUNK = 500


# ---------------------------------------------------------------------------
# Pure-CPU scan stages. No session/ORM/await inside — they run via asyncio.to_thread
# so the per-inbound ~1s of XOR/popcount + cosine math never blocks the event loop.
# ---------------------------------------------------------------------------


def _count_phash_matches(
    inbound_ints: list[int], listing_ints: tuple[int, ...] | list[int], max_hamming: int
) -> int:
    """How many inbound photos have at least one near-duplicate in the listing (pHash).

    Hamming distance via int XOR + popcount — bit-identical to imagehash's `hash1 - hash2`
    (both count differing bits of the same 64-bit hash) but ~40x faster, because it skips
    re-parsing hex into numpy bool arrays on every one of the ~1M comparisons per inbound.
    Hex→int parsing (with the same malformed-entry skipping as the old imagehash path)
    happens once per scan row in candidates.parse_phashes.
    """
    if not inbound_ints or not listing_ints:
        return 0
    count = 0
    for ia in inbound_ints:
        for lb in listing_ints:
            if (ia ^ lb).bit_count() <= max_hamming:
                count += 1
                break
    return count


def _phash_prescreen(
    inbound_ints: list[int], rows: list[CandidateRow], max_hamming: int
) -> dict[int, int]:
    """Stage 1 over all candidate rows. Pure CPU — runs in a worker thread."""
    counts: dict[int, int] = {}
    for row in rows:
        if row.phash_ints:
            counts[row.id] = _count_phash_matches(inbound_ints, row.phash_ints, max_hamming)
    return counts


def _clip_score_chunk(
    inbound_embs: list, blob_rows: list, threshold: float
) -> dict[int, tuple[int, float]]:
    """Stage 2 for one pre-fetched chunk of (listing_id, blob). Pure CPU — worker thread."""
    out: dict[int, tuple[int, float]] = {}
    for lid, blob in blob_rows:
        if not blob:
            continue
        try:
            listing_embs = unpack_embeddings(blob)
        except Exception:
            # One corrupt BLOB must not take the whole matcher down for every inbound.
            logger.warning("[match] corrupt photo_embeddings for listing=%s — skipped", lid)
            continue
        if listing_embs:
            out[lid] = _clip_match(inbound_embs, listing_embs, threshold)
    return out


def _select_best(
    rows: list[CandidateRow],
    ph_counts: dict[int, int],
    clip_results: dict[int, tuple[int, float]],
    parsed: ParsedInbound,
    n_inbound_phashes: int,
    settings,
) -> tuple[int | None, int, float, str]:
    """Stage 3 text cross-check + winner selection. Pure CPU — runs in a worker thread.

    NOTE: the pHash-vs-CLIP signal choice reads the RUNNING best_score, so it depends on
    listing iteration order. Preserved bit-for-bit on purpose — determinization is
    explicitly deferred to a separate PR.
    """
    best_id: int | None = None
    best_count = 0
    best_score = 0.0
    best_method = METHOD_CLIP
    best_is_primary = True

    for row in rows:
        ph_count = ph_counts.get(row.id, 0)
        clip_count, clip_score = clip_results.get(row.id, (0, 0.0))
        if ph_count == 0 and clip_count == 0 and clip_score == 0.0:
            continue

        # Pick the stronger signal per listing.
        if clip_count > ph_count or (
            clip_count == ph_count and clip_score > best_score
        ):
            cand_count, cand_score, cand_method = clip_count, clip_score, METHOD_CLIP
        else:
            cand_count, cand_score, cand_method = (
                ph_count, float(ph_count) / max(n_inbound_phashes, 1), METHOD_PHASH
            )

        # Stage 3 — text cross-check (rejects CLIP false positives between unrelated apartments
        # that happen to have similar-looking interiors). Only applies to CLIP-based candidates;
        # pHash with ≥2 matches is already strong enough on its own.
        if cand_method == METHOD_CLIP and not _passes_text_check(parsed, row, settings):
            continue

        # Track best across listings (prefer higher count; tie-break by score; A4 final
        # tie-break: on an EXACT (count, score) tie, prefer the cross-platform-dedup primary
        # so the site links the better representative. This only fires on exact ties, so it
        # cannot change any non-tied outcome the matcher made before A4).
        better = cand_count > best_count or (cand_count == best_count and cand_score > best_score)
        primary_tiebreak = (
            best_id is not None
            and cand_count == best_count
            and cand_score == best_score
            and row.is_primary
            and not best_is_primary
        )
        if better or primary_tiebreak:
            best_id = row.id
            best_count = cand_count
            best_score = cand_score
            best_method = cand_method
            best_is_primary = row.is_primary

    return best_id, best_count, best_score, best_method


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


def _passes_text_check(parsed: ParsedInbound, listing, settings) -> bool:
    """Reject candidate if inbound caption is incompatible with the listing.

    `listing` is anything with price_eur/area_m2/district attributes — a Listing ORM object
    or the lightweight CandidateRow used by the scan.

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
