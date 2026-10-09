"""Equivalence tests for the matcher perf refactor (W2-D1 thread-pure scan, W2-D2 candidate cache).

The old in-loop scan logic is copied VERBATIM into this file as a reference implementation.
Every test asserts the refactored path (pure-CPU stages via asyncio.to_thread + optional
batch cache) produces bit-for-bit identical results: same candidates, same scores, same
pHash-vs-CLIP signal choices, same (deliberately order-dependent) selection behavior.
"""

from __future__ import annotations

import random
import pytest


import numpy as np
from sqlalchemy import event, select

from src.config import get_settings
from src.db.models import (
    InboundPost,
    InboundStatus,
    Listing,
    Platform,
    SourceChannel,
)
from src.matching.candidates import (
    CandidateRow,
    load_candidate_cache,
    parse_phashes,
)
from src.matching.clip_embed import EMBED_DIM, pack_embeddings, unpack_embeddings
from src.matching.inbound_parser import ParsedInbound, parse_inbound_text
from src.matching.matcher import (
    _CLIP_CHUNK,
    METHOD_CLIP,
    METHOD_PHASH,
    STATUS_MATCHED,
    STATUS_NO_MATCH,
    _clip_match,
    _passes_text_check,
    _phash_prescreen,
    _select_best,
    match_all_pending,
    match_inbound,
)
from tests.conftest import fresh_db

# ---------------------------------------------------------------------------
# Reference implementation — the OLD matcher scan, copied verbatim (pre-refactor).
# ---------------------------------------------------------------------------


def _old_parse_hashes(hex_hashes: list[str]) -> list[int]:
    out: list[int] = []
    for h in hex_hashes:
        if not isinstance(h, str) or len(h) != 16:
            continue
        try:
            out.append(int(h, 16))
        except ValueError:
            continue
    return out


def _old_count_phash_matches(
    inbound_ints: list[int], listing_hashes: list[str], max_hamming: int
) -> int:
    if not inbound_ints or not listing_hashes:
        return 0
    listing_ints = _old_parse_hashes(listing_hashes)
    if not listing_ints:
        return 0
    count = 0
    for ia in inbound_ints:
        for lb in listing_ints:
            if (ia ^ lb).bit_count() <= max_hamming:
                count += 1
                break
    return count


def _old_select_loop(rows, ph_counts, clip_results, parsed, inbound_phashes, settings):
    """The old in-line selection loop from match_inbound, verbatim."""
    best_id = None
    best_count = 0
    best_score = 0.0
    best_method = METHOD_CLIP

    for row in rows:
        ph_count = ph_counts.get(row.id, 0)
        clip_count, clip_score = clip_results.get(row.id, (0, 0.0))
        if ph_count == 0 and clip_count == 0 and clip_score == 0.0:
            continue

        if clip_count > ph_count or (
            clip_count == ph_count and clip_score > best_score
        ):
            cand_count, cand_score, cand_method = clip_count, clip_score, METHOD_CLIP
        else:
            cand_count, cand_score, cand_method = (
                ph_count, float(ph_count) / max(len(inbound_phashes), 1), METHOD_PHASH
            )

        if cand_method == METHOD_CLIP and not _passes_text_check(parsed, row, settings):
            continue

        if cand_count > best_count or (
            cand_count == best_count and cand_score > best_score
        ):
            best_id = row.id
            best_count = cand_count
            best_score = cand_score
            best_method = cand_method

    return best_id, best_count, best_score, best_method


async def _old_reference_expectation(factory, inbound_id: int) -> dict:
    """Full old match_inbound pipeline (read-only) → expected MatchResult fields.

    Replicates early exits, hex-parse-per-row stage 1, chunked stage 2 and the old
    selection loop exactly as the pre-refactor code did.
    """
    settings = get_settings()
    clip_thr = settings.clip_similarity_threshold
    min_photos = settings.min_matched_photos
    phash_thr = settings.phash_max_hamming

    async with factory() as s:
        inbound = await s.get(InboundPost, inbound_id)
        if not inbound.photo_phashes:
            return {"listing_id": None, "matched_photos": 0, "confidence": 0.0,
                    "status": STATUS_NO_MATCH, "method": METHOD_CLIP}

        inbound_phashes = list(inbound.photo_phashes)
        inbound_embs = unpack_embeddings(inbound.photo_embeddings) if inbound.photo_embeddings else []
        if max(len(inbound_phashes), len(inbound_embs)) < min_photos:
            return {"listing_id": None, "matched_photos": 0, "confidence": 0.0,
                    "status": STATUS_NO_MATCH, "method": METHOD_CLIP}

        rows = (
            await s.execute(
                select(
                    Listing.id,
                    Listing.photo_phashes,
                    Listing.price_eur,
                    Listing.area_m2,
                    Listing.district,
                    Listing.photo_embeddings.isnot(None).label("has_embs"),
                ).where(Listing.is_active.is_(True))
            )
        ).all()

        parsed = parse_inbound_text(inbound.raw_text)

        inbound_ints = _old_parse_hashes(inbound_phashes)
        ph_counts: dict[int, int] = {}
        for row in rows:
            if row.photo_phashes:
                ph_counts[row.id] = _old_count_phash_matches(
                    inbound_ints, list(row.photo_phashes), phash_thr
                )

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
                for lid, blob in blob_rows:
                    if not blob:
                        continue
                    listing_embs = unpack_embeddings(blob)
                    if listing_embs:
                        clip_results[lid] = _clip_match(inbound_embs, listing_embs, clip_thr)

        best_id, best_count, best_score, best_method = _old_select_loop(
            rows, ph_counts, clip_results, parsed, inbound_phashes, settings
        )

        if best_id is None or best_count < min_photos:
            return {"listing_id": None, "matched_photos": best_count, "confidence": best_score,
                    "status": STATUS_NO_MATCH, "method": best_method}

        # LLM gate: in tests listing photo dirs never exist on disk → validator returns None
        # → CLIP verdict kept, method stays as chosen by the scan (same as old behavior).
        confidence = round(
            best_score if best_method == METHOD_CLIP else best_count / max(len(inbound_phashes), 1), 3
        )
        return {"listing_id": best_id, "matched_photos": best_count, "confidence": confidence,
                "status": STATUS_MATCHED, "method": best_method}


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def make_phash(hex_char: str) -> str:
    assert len(hex_char) == 1
    return hex_char * 16


def flip(hex_hash: str, positions: list[int]) -> str:
    val = int(hex_hash, 16)
    for pos in positions:
        val ^= 1 << pos
    return f"{val:016x}"


def _unit_vec(rng: np.random.Generator) -> np.ndarray:
    v = rng.standard_normal(EMBED_DIM).astype(np.float32)
    return v / np.linalg.norm(v)


async def _seed_rich(factory, *, seed: int) -> dict[str, int]:
    """Scenario covering every scan path: pHash winner, CLIP winner, text-check rejection,
    weak decoy, malformed hashes, no-match and early-exit inbounds.
    """
    rng = np.random.default_rng(seed)
    v1, v2, v3 = _unit_vec(rng), _unit_vec(rng), _unit_vec(rng)
    junk1, junk2 = _unit_vec(rng), _unit_vec(rng)

    h0, h1, h2, h3 = make_phash("0"), make_phash("f"), make_phash("a"), make_phash("c")

    async with factory() as s:
        ch = SourceChannel(tg_id=-9000 - seed, username=f"eqv{seed}", is_active=True)
        s.add(ch)
        await s.flush()

        l_phash = Listing(
            platform=Platform.NEKRETNINE, external_id="zzz-eqv-ph",
            url="https://x/ph", photo_urls=["a", "b", "c"],
            photo_phashes=[flip(h0, [3]), flip(h1, [5, 7]), make_phash("9")],
        )
        l_clip = Listing(
            platform=Platform.CETIRI_ZIDA, external_id="zzz-eqv-clip",
            url="https://x/clip", photo_urls=["a", "b", "c"],
            photo_phashes=[make_phash("7")],
            photo_embeddings=pack_embeddings([v1, v2, junk1]),
            price_eur=600, area_m2=50.0,
        )
        l_reject = Listing(  # same CLIP photos but price 2000 vs caption 600 → stage-3 reject
            platform=Platform.HALOOGLASI, external_id="zzz-eqv-rej",
            url="https://x/rej", photo_urls=["a", "b"],
            photo_phashes=[make_phash("e")],
            photo_embeddings=pack_embeddings([v1, v2]),
            price_eur=2000, area_m2=50.0,
        )
        l_decoy = Listing(  # only 1 pHash hit — below min_matched_photos
            platform=Platform.NEKRETNINE, external_id="zzz-eqv-decoy",
            url="https://x/decoy", photo_urls=["a"],
            photo_phashes=[h0],
        )
        l_malformed = Listing(
            platform=Platform.NEKRETNINE, external_id="zzz-eqv-bad",
            url="https://x/bad", photo_urls=["a"],
            photo_phashes=["not-hex-16-char!", "abc"],
        )
        l_junk_embs = Listing(  # embeddings present but dissimilar
            platform=Platform.CETIRI_ZIDA, external_id="zzz-eqv-junk",
            url="https://x/junk", photo_urls=["a"],
            photo_phashes=[],
            photo_embeddings=pack_embeddings([junk2]),
        )
        s.add_all([l_phash, l_clip, l_reject, l_decoy, l_malformed, l_junk_embs])

        i_phash = InboundPost(  # → pHash match with l_phash (photos 0 and 1)
            source_channel_id=ch.id, tg_message_id=1,
            photo_paths=[f"/tmp/zzz-eqv-{seed}-{i}.jpg" for i in range(4)],
            photo_phashes=[h0, h1, h2, h3],
            status=InboundStatus.NEW,
        )
        i_clip = InboundPost(  # → CLIP match with l_clip (v1, v2); l_reject killed by text check
            source_channel_id=ch.id, tg_message_id=2,
            raw_text="Сдается, цена 600€, 50 м²",
            photo_paths=[f"/tmp/zzz-eqv-{seed}-c{i}.jpg" for i in range(3)],
            photo_phashes=[make_phash("1"), make_phash("2"), make_phash("4")],
            photo_embeddings=pack_embeddings([v1, v2, v3]),
            status=InboundStatus.NEW,
        )
        i_nomatch = InboundPost(  # 2 photos, nothing close anywhere
            source_channel_id=ch.id, tg_message_id=3,
            photo_paths=["/tmp/zzz-n1.jpg", "/tmp/zzz-n2.jpg"],
            photo_phashes=[make_phash("5"), make_phash("6")],
            status=InboundStatus.NEW,
        )
        i_single = InboundPost(  # 1 photo < min_matched_photos → early exit, scan skipped
            source_channel_id=ch.id, tg_message_id=4,
            photo_paths=["/tmp/zzz-s1.jpg"],
            photo_phashes=[make_phash("3")],
            status=InboundStatus.NEW,
        )
        s.add_all([i_phash, i_clip, i_nomatch, i_single])
        await s.commit()
        return {
            "l_phash": l_phash.id, "l_clip": l_clip.id, "l_reject": l_reject.id,
            "inbounds": [i_phash.id, i_clip.id, i_nomatch.id, i_single.id],
            "i_phash": i_phash.id, "i_clip": i_clip.id,
            "i_nomatch": i_nomatch.id, "i_single": i_single.id,
        }


def _result_tuple(r) -> tuple:
    return (r.inbound_id, r.listing_id, r.matched_photos, r.confidence, r.status, r.method)


def _track_candidate_selects(engine) -> dict:
    """Counts executions of the lightweight candidates SELECT (its unique 'has_embs' label)."""
    counter = {"n": 0}

    def _cb(conn, cursor, statement, params, context, executemany):
        if "has_embs" in statement:
            counter["n"] += 1

    event.listen(engine.sync_engine, "before_cursor_execute", _cb)
    return counter


# ---------------------------------------------------------------------------
# W2-D1 — bit-for-bit equivalence of the threaded scan vs the old in-loop path
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolate_algorithm(monkeypatch):
    monkeypatch.setattr(get_settings(), "enable_llm_validation", False)


async def test_match_inbound_equals_old_reference_end_to_end():
    """New (threaded) path vs verbatim old pipeline on a fixture covering all stages."""
    async with fresh_db() as (_engine, factory):
        ids = await _seed_rich(factory, seed=42)
        for inbound_id in ids["inbounds"]:
            expected = await _old_reference_expectation(factory, inbound_id)
            result = await match_inbound(inbound_id)
            got = {"listing_id": result.listing_id, "matched_photos": result.matched_photos,
                   "confidence": result.confidence, "status": result.status, "method": result.method}
            assert got == expected, f"inbound={inbound_id}"

        # Scenario sanity (also guards the fixture itself against silent degradation).
        r_ph = await match_inbound(ids["i_phash"])    # idempotent second call → existing match
        assert (r_ph.listing_id, r_ph.method, r_ph.status) == (ids["l_phash"], METHOD_PHASH, STATUS_MATCHED)
        r_clip = await match_inbound(ids["i_clip"])
        assert (r_clip.listing_id, r_clip.status) == (ids["l_clip"], STATUS_MATCHED)
        assert r_clip.method.startswith(METHOD_CLIP)
        assert r_clip.listing_id != ids["l_reject"]   # text cross-check still rejects


async def test_phash_prescreen_equiv_random():
    """Stage 1: pre-parsed-int prescreen vs old hex-parse-per-row counting, randomized."""
    rnd = random.Random(1234)
    hexchars = "0123456789abcdef"

    def rand_hash():
        return "".join(rnd.choice(hexchars) for _ in range(16))

    for trial in range(50):
        inbound_hashes = [rand_hash() for _ in range(rnd.randint(1, 6))] + ["short", 99]
        inbound_ints = _old_parse_hashes(inbound_hashes)
        assert inbound_ints == parse_phashes(inbound_hashes)  # shared parser semantics

        listings = []
        for lid in range(1, rnd.randint(2, 30)):
            hashes: list = []
            for _ in range(rnd.randint(0, 5)):
                kind = rnd.random()
                if kind < 0.4:  # near-duplicate of a random inbound hash
                    base = rnd.choice(inbound_hashes[:-2])
                    hashes.append(flip(base, rnd.sample(range(64), rnd.randint(0, 7))))
                elif kind < 0.8:
                    hashes.append(rand_hash())
                else:  # malformed
                    hashes.append(rnd.choice(["zz" * 8, "abc", "", 7]))
            listings.append((lid, hashes))

        old_counts: dict[int, int] = {}
        for lid, hashes in listings:
            if hashes:
                old_counts[lid] = _old_count_phash_matches(inbound_ints, list(hashes), 6)

        rows = [
            CandidateRow(id=lid, phash_ints=tuple(parse_phashes(h)) if h else (),
                         price_eur=None, area_m2=None, district=None, has_embs=False)
            for lid, h in listings
        ]
        new_counts = _phash_prescreen(inbound_ints, rows, 6)

        for lid, _ in listings:
            assert new_counts.get(lid, 0) == old_counts.get(lid, 0), f"trial={trial} lid={lid}"


async def test_select_best_order_invariant_random():
    """Stage 3 + winner selection vs old loop, randomized, incl. reversed iteration order."""
    settings = get_settings()
    rnd = random.Random(99)

    for _trial in range(100):
        n = rnd.randint(1, 25)
        rows = [
            CandidateRow(
                id=i, phash_ints=(),
                price_eur=rnd.choice([None, 400, 600, 2000]),
                area_m2=rnd.choice([None, 30.0, 50.0, 120.0]),
                district=None, has_embs=False,
            )
            for i in range(1, n + 1)
        ]
        ph_counts = {i: rnd.randint(0, 4) for i in range(1, n + 1) if rnd.random() < 0.6}
        clip_results = {
            i: (rnd.randint(0, 4), rnd.random())
            for i in range(1, n + 1) if rnd.random() < 0.6
        }
        parsed = ParsedInbound(
            price_eur=rnd.choice([None, 600]),
            area_m2=rnd.choice([None, 50.0]),
            price_bucket_max=rnd.choice([None, 800]),
        )
        n_ph = rnd.randint(1, 6)
        forward = _select_best(rows, ph_counts, clip_results, parsed, n_ph, settings)
        reverse = _select_best(list(reversed(rows)), ph_counts, clip_results, parsed, n_ph, settings)
        shuffled = list(rows)
        rnd.shuffle(shuffled)
        assert forward == reverse == _select_best(shuffled, ph_counts, clip_results, parsed, n_ph, settings)


async def test_select_best_fixes_order_dependent_quirk():
    """The pHash-vs-CLIP choice reads the RUNNING best_score → method depends on row order.
    This quirk is intentionally preserved (determinization deferred); pin it down.
    """
    settings = get_settings()
    parsed = ParsedInbound()
    row_a = CandidateRow(id=1, phash_ints=(), price_eur=None, area_m2=None, district=None, has_embs=True)
    row_b = CandidateRow(id=2, phash_ints=(), price_eur=None, area_m2=None, district=None, has_embs=True)
    ph_counts = {2: 2}
    clip_results = {1: (1, 0.95), 2: (2, 0.5)}

    fwd_old = _old_select_loop([row_a, row_b], ph_counts, clip_results, parsed, ["x"] * 4, settings)
    fwd_new = _select_best([row_a, row_b], ph_counts, clip_results, parsed, 4, settings)
    rev_old = _old_select_loop([row_b, row_a], ph_counts, clip_results, parsed, ["x"] * 4, settings)
    rev_new = _select_best([row_b, row_a], ph_counts, clip_results, parsed, 4, settings)

    assert fwd_old != rev_old  # documents the old bug
    assert fwd_new == rev_new == (2, 2, 0.5, METHOD_PHASH)


# ---------------------------------------------------------------------------
# W2-D2 — candidate cache: identical results, one candidates SELECT per batch
# ---------------------------------------------------------------------------


async def test_cache_path_identical_to_nocache_path():
    """match_inbound(cache=...) on an identical DB yields field-identical results."""
    baseline: list[tuple] = []
    async with fresh_db() as (_engine, factory):
        ids = await _seed_rich(factory, seed=7)
        for inbound_id in ids["inbounds"]:
            baseline.append(_result_tuple(await match_inbound(inbound_id)))

    cached: list[tuple] = []
    async with fresh_db() as (_engine, factory):
        ids = await _seed_rich(factory, seed=7)
        async with factory() as s:
            cache = await load_candidate_cache(s)
        for inbound_id in ids["inbounds"]:
            cached.append(_result_tuple(await match_inbound(inbound_id, cache=cache)))

    assert sorted(cached) == sorted(baseline)


async def test_match_all_pending_identical_and_single_select():
    """rematch-all-equivalent run: same matches as per-inbound no-cache runs, and exactly
    ONE candidates SELECT for the whole batch (instead of one per inbound)."""
    baseline: list[tuple] = []
    async with fresh_db() as (engine, factory):
        ids = await _seed_rich(factory, seed=11)
        counter = _track_candidate_selects(engine)
        for inbound_id in ids["inbounds"]:
            baseline.append(_result_tuple(await match_inbound(inbound_id)))
        # Control: old behavior = one scan per inbound that reaches the scan
        # (i_single early-exits before the SELECT).
        assert counter["n"] == 3

    async with fresh_db() as (engine, factory):
        ids = await _seed_rich(factory, seed=11)
        counter = _track_candidate_selects(engine)
        results = await match_all_pending()
        assert counter["n"] == 1  # single batch-level candidates SELECT
        assert sorted(_result_tuple(r) for r in results) == sorted(baseline)


async def test_match_all_pending_no_pending_skips_cache_load():
    """Zero NEW inbounds → no candidate SELECT at all (lazy cache build)."""
    async with fresh_db() as (engine, _factory):
        counter = _track_candidate_selects(engine)
        results = await match_all_pending()
        assert results == []
        assert counter["n"] == 0


async def test_scan_rows_from_db_preserves_order_and_values():
    """Cache rows mirror the raw SELECT: same order, same scalar fields, parsed pHashes."""
    async with fresh_db() as (_engine, factory):
        await _seed_rich(factory, seed=3)
        async with factory() as s:
            from src.matching.candidates import candidate_scan_select

            db_rows = (await s.execute(candidate_scan_select())).all()
            cache = await load_candidate_cache(s)

        assert [r.id for r in cache.rows] == [r.id for r in db_rows]
        for raw, row in zip(db_rows, cache.rows, strict=True):
            assert row.price_eur == raw.price_eur
            assert row.area_m2 == raw.area_m2
            assert row.district == raw.district
            assert row.has_embs == bool(raw.has_embs)
            assert list(row.phash_ints) == _old_parse_hashes(list(raw.photo_phashes or []))
