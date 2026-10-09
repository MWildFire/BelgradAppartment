"""Deterministic independent-photo matching with durable, fail-closed vision validation."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

import numpy as np
from numpy.typing import NDArray
from sqlalchemy import select, update

from src.config import get_settings
from src.db import (
    InboundPost,
    InboundStatus,
    Listing,
    Match,
    MatchStatus,
    get_session,
)
from src.db.models import ValidationJob
from src.matching import validation_jobs
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
from src.matching.pairs import clip_pairs, phash_pairs
from src.matching.validation_contracts import ValidationOutcome

logger = logging.getLogger(__name__)

# Status values for MatchResult.status
STATUS_MATCHED = "matched"
STATUS_NO_MATCH = "no_match"
STATUS_PENDING = "validation_pending"

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
    """Claim → local scan → bounded validation → atomic result. Provider IO holds no session."""
    async with get_session() as session:
        if await session.get(InboundPost, inbound_id) is None:
            raise ValueError(f"InboundPost {inbound_id} not found")
        existing = (
            (
                await session.execute(
                    select(Match)
                    .where(
                        Match.inbound_post_id == inbound_id,
                    )
                    .order_by(Match.id)
                )
            )
            .scalars()
            .first()
        )
        if existing is not None:
            return MatchResult(
                inbound_id,
                existing.listing_id,
                existing.matched_photo_count,
                existing.confidence,
                STATUS_MATCHED,
                existing.id,
                existing.method,
            )
    token = await validation_jobs.claim(inbound_id)
    if token is None:
        return MatchResult(inbound_id, None, 0, 0.0, STATUS_PENDING)
    try:
        return await _match_claimed(inbound_id, token, cache)
    except Exception:
        await validation_jobs.defer(inbound_id, token, "internal_error")
        raise


async def _match_claimed(
    inbound_id: int,
    token: str,
    cache: CandidateCache | None,
) -> MatchResult:
    settings = get_settings()
    async with get_session() as session:
        inbound = await session.get(InboundPost, inbound_id)
        assert inbound is not None
        inbound_ints = parse_phashes(inbound.photo_phashes or [])
        inbound_embs = (
            unpack_embeddings(inbound.photo_embeddings) if inbound.photo_embeddings else []
        )
        if max(len(inbound_ints), len(inbound_embs)) < settings.min_matched_photos:
            rows = []
        else:
            rows = (
                cache.rows
                if cache is not None
                else await asyncio.to_thread(
                    scan_rows_from_db,
                    (await session.execute(candidate_scan_select())).all(),
                )
            )
        ph_counts = await asyncio.to_thread(
            _phash_prescreen,
            inbound_ints,
            rows,
            settings.phash_max_hamming,
        )
        clip_results: dict[int, tuple[int, float]] = {}
        blob_fingerprints: dict[int, str] = {}
        if inbound_embs:
            emb_ids = [row.id for row in rows if row.has_embs]
            for start in range(0, len(emb_ids), _CLIP_CHUNK):
                blobs = (
                    await session.execute(
                        select(Listing.id, Listing.photo_embeddings).where(
                            Listing.id.in_(emb_ids[start : start + _CLIP_CHUNK]),
                        )
                    )
                ).all()
                blob_fingerprints.update(
                    {cid: hashlib.sha256(blob).hexdigest() for cid, blob in blobs if blob}
                )
                clip_results.update(
                    await asyncio.to_thread(
                        _clip_score_chunk,
                        inbound_embs,
                        blobs,
                        settings.clip_similarity_threshold,
                    )
                )
        ranked = _rank_candidates(
            rows,
            ph_counts,
            clip_results,
            parse_inbound_text(inbound.raw_text),
            len(inbound_ints),
            settings,
        )
        candidates: list[tuple[Listing, int, float, str]] = []
        snapshot_changed = False
        scan_rows = {row.id: row for row in rows}
        seen_identities: set[int] = set()
        overflow = False
        for cid, count, score, method in ranked:
            if count < settings.min_matched_photos:
                continue
            listing = await session.get(Listing, cid)
            if listing is not None and listing.is_active and count >= settings.min_matched_photos:
                scanned = scan_rows[cid]
                changed = scanned.phash_ints != tuple(
                    parse_phashes(listing.photo_phashes or [])
                ) or any(
                    getattr(scanned, name) != getattr(listing, name)
                    for name in ("price_eur", "area_m2", "district", "is_primary")
                )
                if cid in blob_fingerprints:
                    changed = (
                        changed
                        or blob_fingerprints[cid]
                        != hashlib.sha256(listing.photo_embeddings or b"").hexdigest()
                    )
                if changed:
                    snapshot_changed = True
                    continue
                identity = listing.dup_group_id or listing.id
                if identity in seen_identities:
                    continue
                seen_identities.add(identity)
                if len(candidates) == settings.llm_max_candidates:
                    overflow = True
                    break
                candidates.append((listing, count, score, method))
        # ORM objects are detached after this block (expire_on_commit=False).

    if snapshot_changed:
        await validation_jobs.defer(inbound_id, token, "candidate_snapshot_changed")
        return MatchResult(inbound_id, None, 0, 0.0, STATUS_PENDING)
    best = ranked[0] if ranked else (None, 0, 0.0, METHOD_CLIP)
    outcomes: list[dict[str, Any]] = []
    accepted: list[tuple[Listing, int, float, str]] = []
    pending_reason: str | None = None
    review = False
    if candidates and (candidates[0][3] == METHOD_PHASH or not settings.enable_llm_validation):
        accepted = [candidates[0]]
    else:
        try:
            async with asyncio.timeout(settings.llm_total_timeout_seconds):
                for listing, count, score, _method in candidates:
                    outcome = await _run_llm_validation(inbound, listing, settings)
                    outcomes.append({"listing_id": listing.id, **asdict(outcome)})
                    if outcome.accepted:
                        accepted.append((listing, count, score, METHOD_CLIP_LLM))
                    elif outcome.verdict != "different":
                        pending_reason = outcome.reason_code
                        review = review or outcome.verdict in {"uncertain", "invalid_evidence"}
        except TimeoutError:
            pending_reason = "deadline_exceeded"

    # A confirmed candidate is not unique while another candidate remains unresolved.
    identities = {item[0].dup_group_id or item[0].id for item in accepted}
    if overflow and not (accepted and accepted[0][3] == METHOD_PHASH):
        pending_reason, review = "candidate_limit", True
    if len(identities) > 1:
        pending_reason, review = "multiple_identities", True
    if pending_reason:
        await validation_jobs.defer(
            inbound_id, token, pending_reason, review=review, outcomes=outcomes
        )
        return MatchResult(inbound_id, None, best[1], best[2], STATUS_PENDING)

    # Consume the lease with a conditional write BEFORE creating a Match in the same transaction.
    async with get_session() as session:
        owned = (
            await session.execute(
                update(ValidationJob)
                .where(
                    ValidationJob.inbound_id == inbound_id,
                    ValidationJob.lease_token == token,
                    ValidationJob.lease_until > datetime.now(UTC),
                )
                .values(
                    state="complete",
                    outcomes=outcomes,
                    lease_token=None,
                    lease_until=None,
                    next_retry_at=None,
                    updated_at=datetime.now(UTC),
                )
                .returning(ValidationJob.inbound_id)
            )
        ).scalar_one_or_none()
        if owned is None:
            return MatchResult(inbound_id, None, best[1], best[2], STATUS_PENDING)
        current = await session.get(InboundPost, inbound_id)
        assert current is not None
        if (
            current.status != InboundStatus.NEW
            or current.photo_phashes != inbound.photo_phashes
            or current.photo_embeddings != inbound.photo_embeddings
            or current.photo_evidence != inbound.photo_evidence
            or current.raw_text != inbound.raw_text
        ):
            job = await session.get(ValidationJob, inbound_id)
            assert job is not None
            job.state, job.reason_code = "needs_review", "inbound_changed"
            return MatchResult(inbound_id, None, best[1], best[2], STATUS_PENDING)
        if accepted:
            listing, count, score, method = accepted[0]
            live = await session.get(Listing, listing.id)
            changed = (
                live is None
                or not live.is_active
                or any(
                    getattr(live, name) != getattr(listing, name)
                    for name in (
                        "photo_phashes",
                        "photo_embeddings",
                        "photo_evidence",
                        "price_eur",
                        "area_m2",
                        "district",
                        "dup_group_id",
                    )
                )
            )
            if changed:
                job = await session.get(ValidationJob, inbound_id)
                assert job is not None
                job.state, job.reason_code = "pending", "candidate_changed"
                return MatchResult(inbound_id, None, count, score, STATUS_PENDING)
            confidence = round(score, 3)
            match = Match(
                inbound_post_id=inbound_id,
                listing_id=listing.id,
                confidence=confidence,
                matched_photo_count=count,
                method=method,
                status=MatchStatus.PENDING,
            )
            session.add(match)
            current.status = InboundStatus.MATCHED
            await session.flush()
            return MatchResult(
                inbound_id, listing.id, count, confidence, STATUS_MATCHED, match.id, method
            )
        current.status = InboundStatus.NO_MATCH
        method = METHOD_CLIP_LLM_REJECTED if outcomes else best[3]
        return MatchResult(inbound_id, None, best[1], best[2], STATUS_NO_MATCH, method=method)


async def _run_llm_validation(
    inbound: InboundPost,
    listing: Listing,
    settings: Any,
) -> ValidationOutcome:
    from src.matching.evidence import select_evidence
    from src.matching.llm_validator import LLMValidatorError, validate_match

    try:
        inbound_paths, listing_paths = await asyncio.to_thread(
            select_evidence,
            inbound,
            listing,
            settings,
        )
        result = await validate_match(
            inbound_paths,
            listing_paths,
            inbound_id=inbound.id,
            listing_id=listing.id,
            # Captions, contact details, addresses and free-form listing text are not sent.
            listing_meta={
                "price_eur": listing.price_eur,
                "area_m2": listing.area_m2,
                "rooms": listing.rooms,
            },
        )
    except LLMValidatorError as exc:
        return ValidationOutcome(exc.verdict, exc.reason_code)
    except Exception:
        logger.warning(
            "[match] validation unavailable inbound=%s listing=%s", inbound.id, listing.id
        )
        return ValidationOutcome("unavailable", "validator_error")
    if result.uncertain or result.confidence < settings.llm_validation_min_confidence:
        return ValidationOutcome(
            "uncertain",
            "low_confidence",
            result.confidence,
            settings.llm_model,
            result._audit.get("evidence_hash", ""),
            result._audit.get("usage", {}),
        )
    return ValidationOutcome(
        "same" if result.is_same_apartment else "different",
        "model_verdict",
        result.confidence,
        settings.llm_model,
        result._audit.get("evidence_hash", ""),
        result._audit.get("usage", {}),
    )


async def match_all_pending(cache: CandidateCache | None = None) -> list[MatchResult]:
    """Run matcher for automatically eligible NEW posts. Useful for batch backfill.

    Builds the candidate snapshot ONCE (unless the caller already passes one), so a batch
    of N inbounds does a single candidates SELECT instead of N full table scans.
    """
    from src.matching.validation_queue import automatic_inbound_ids

    ids = await automatic_inbound_ids()
    async with get_session() as s:
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
    """Count independent one-to-one photo pairs; copies cannot meet the floor."""
    return len(phash_pairs(inbound_ints, listing_ints, max_hamming))


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
    inbound_embs: list[NDArray[np.float32]], blob_rows: Sequence[Any], threshold: float
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


def _rank_candidates(
    rows: list[CandidateRow],
    ph_counts: dict[int, int],
    clip_results: dict[int, tuple[int, float]],
    parsed: ParsedInbound,
    n_inbound_phashes: int,
    settings: Any,
) -> list[tuple[int, int, float, str]]:
    ranked = []
    for row in rows:
        ph = ph_counts.get(row.id, 0)
        cc, cs = clip_results.get(row.id, (0, 0.0))
        # Choose each candidate's signal without reading any other candidate's score.
        if ph >= cc and ph > 0:
            count, score, method = ph, ph / max(n_inbound_phashes, 1), METHOD_PHASH
        else:
            count, score, method = cc, cs, METHOD_CLIP
        if count == 0 or (method == METHOD_CLIP and not _passes_text_check(parsed, row, settings)):
            continue
        # Scores from different methods have different meanings. Method priority precedes score.
        key = (count, method == METHOD_PHASH, round(score, 9), row.is_primary, -row.id)
        ranked.append((key, (row.id, count, score, method)))
    return [value for _, value in sorted(ranked, reverse=True)]


def _select_best(
    rows: list[CandidateRow],
    ph_counts: dict[int, int],
    clip_results: dict[int, tuple[int, float]],
    parsed: ParsedInbound,
    n_inbound_phashes: int,
    settings: Any,
) -> tuple[int | None, int, float, str]:
    ranked = _rank_candidates(rows, ph_counts, clip_results, parsed, n_inbound_phashes, settings)
    return ranked[0] if ranked else (None, 0, 0.0, METHOD_CLIP)


def _clip_match(
    inbound_embs: list[NDArray[np.float32]],
    listing_embs: list[NDArray[np.float32]],
    threshold: float,
) -> tuple[int, float]:
    pairs = clip_pairs(inbound_embs, listing_embs, threshold)
    # Coverage-weighted confidence; unmatched inputs contribute zero.
    return len(pairs), sum(score for _, _, score in pairs) / max(len(inbound_embs), 1)


def _passes_text_check(parsed: ParsedInbound, listing: Any, settings: Any) -> bool:
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
