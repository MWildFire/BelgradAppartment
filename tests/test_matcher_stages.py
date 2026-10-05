"""W4-T1 — behavioral tests for matcher stages 2-4 decision logic.

Complements (does not duplicate) the Wave-2 equivalence suite in test_matcher_perf.py:
those tests prove old==new bit-for-bit, so they keep passing even if a decision rule
breaks in BOTH implementations. These tests pin the decisions themselves — the text
cross-check rejection, the LLM gate (accept / reject / fail-open), method labels,
and the min_matched_photos floor.

Determinism: CLIP embeddings are standard-basis unit vectors (cosine is exactly 1.0 or
0.0, and float16 pack/unpack is lossless for {0, 1} components); pHashes are hex strings
with controlled Hamming distances. The LLM validator is replaced with a programmable
stub — no network, no API key, no real photos needed (except where the test says so).
"""

from __future__ import annotations

import numpy as np
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.config import get_settings
from src.db.models import (
    InboundPost,
    InboundStatus,
    Listing,
    Match,
    MatchStatus,
    Platform,
    SourceChannel,
)
from src.matching import llm_validator
from src.matching import matcher as matcher_mod
from src.matching.clip_embed import EMBED_DIM, pack_embeddings
from src.matching.llm_validator import DuplicateCheck, LLMValidatorError
from src.matching.matcher import (
    METHOD_CLIP,
    METHOD_CLIP_LLM,
    METHOD_CLIP_LLM_REJECTED,
    METHOD_PHASH,
    STATUS_MATCHED,
    STATUS_NO_MATCH,
    STATUS_PENDING,
    match_inbound,
)

# ---------------------------------------------------------------------------
# Deterministic builders
# ---------------------------------------------------------------------------


def make_phash(hex_char: str) -> str:
    """16 repeated hex chars = 64-bit pHash. Distinct chars are ≥16 bits apart."""
    assert len(hex_char) == 1
    return hex_char * 16


def flip_bits(hex_hash: str, positions: list[int]) -> str:
    """Toggle exactly `len(positions)` distinct bits → controlled Hamming distance."""
    val = int(hex_hash, 16)
    for pos in positions:
        val ^= 1 << pos
    return f"{val:016x}"


def unit_vec(axis: int) -> np.ndarray:
    """Standard-basis vector: cosine 1.0 with itself, 0.0 with any other axis — exact."""
    v = np.zeros(EMBED_DIM, dtype=np.float32)
    v[axis] = 1.0
    return v


# Inbound pHashes for CLIP scenarios — ≥32 bits away from the listing's single pHash,
# so stage 1 never contributes and the outcome is decided purely by stages 2-4.
CLIP_INBOUND_PHASHES = [make_phash("1"), make_phash("2")]
CLIP_LISTING_PHASH = make_phash("f")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def settings_pin(monkeypatch):
    """Pin every threshold these scenarios depend on — .env must not influence outcomes."""
    s = get_settings()
    monkeypatch.setattr(s, "phash_max_hamming", 6)
    monkeypatch.setattr(s, "min_matched_photos", 2)
    monkeypatch.setattr(s, "clip_similarity_threshold", 0.92)
    monkeypatch.setattr(s, "cross_check_price_tolerance_pct", 0.25)
    monkeypatch.setattr(s, "cross_check_area_tolerance_pct", 0.20)
    monkeypatch.setattr(s, "enable_llm_validation", True)
    monkeypatch.setattr(s, "llm_validation_min_confidence", 0.7)
    return s


class LLMStub:
    """Programmable stand-in for llm_validator.validate_match."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.result: DuplicateCheck | None = None
        self.exc: Exception | None = None


@pytest.fixture
def llm_stub(monkeypatch):
    """Replace validate_match with a programmable verdict; records every call.

    matcher._run_llm_validation re-imports validate_match from the module on each call,
    so patching the module attribute is always picked up.
    """
    stub = LLMStub()
    # These are gate tests; real evidence normalization is covered in test_validation_safety.
    from pathlib import Path
    from src.matching import evidence

    def fake_evidence(inbound, listing, settings):
        root = settings.listings_photos_dir / listing.platform.value / listing.external_id
        if not root.exists():
            raise LLMValidatorError("missing_photos", verdict="invalid_evidence")
        return [Path(p) for p in inbound.photo_paths], list(root.glob("*.jpg"))

    monkeypatch.setattr(evidence, "select_evidence", fake_evidence)

    async def fake_validate_match(inbound_photos, listing_photos, **kwargs):
        stub.calls.append(
            {
                "inbound_photos": list(inbound_photos),
                "listing_photos": list(listing_photos),
                **kwargs,
            }
        )
        if stub.exc is not None:
            raise stub.exc
        assert stub.result is not None, "llm_stub called without a programmed verdict"
        return stub.result

    monkeypatch.setattr(llm_validator, "validate_match", fake_validate_match)
    return stub


# ---------------------------------------------------------------------------
# Seeding / persistence helpers
# ---------------------------------------------------------------------------


async def _seed_clip_scenario(
    db,
    *,
    caption: str = "Сдается, цена 600€, 50 м²",
    listing_price: int | None = 600,
    listing_area: float | None = 50.0,
    inbound_vecs: list[np.ndarray] | None = None,
    listing_vecs: list[np.ndarray] | None = None,
    external_id: str = "w4t1-clip",
) -> tuple[async_sessionmaker, int, int]:
    """One inbound (2 photos with embeddings) + one listing; CLIP-only by construction
    (pHashes are far apart). Returns (session_factory, inbound_id, listing_id)."""
    inbound_vecs = [unit_vec(0), unit_vec(1)] if inbound_vecs is None else inbound_vecs
    listing_vecs = [unit_vec(0), unit_vec(1)] if listing_vecs is None else listing_vecs
    factory = async_sessionmaker(db, expire_on_commit=False)
    async with factory() as s:
        ch = SourceChannel(tg_id=-74001, username="w4t1", is_active=True)
        s.add(ch)
        await s.flush()
        listing = Listing(
            platform=Platform.NEKRETNINE,
            external_id=external_id,
            url=f"https://example.com/{external_id}",
            photo_urls=["a", "b"],
            photo_phashes=[CLIP_LISTING_PHASH],
            photo_embeddings=pack_embeddings(listing_vecs),
            price_eur=listing_price,
            area_m2=listing_area,
        )
        inbound = InboundPost(
            source_channel_id=ch.id,
            tg_message_id=1,
            raw_text=caption,
            photo_paths=["/tmp/w4t1-inb-0.jpg", "/tmp/w4t1-inb-1.jpg"],
            photo_phashes=CLIP_INBOUND_PHASHES,
            photo_embeddings=pack_embeddings(inbound_vecs),
            status=InboundStatus.NEW,
        )
        s.add_all([listing, inbound])
        await s.commit()
        return factory, inbound.id, listing.id


def _put_listing_photos(settings, monkeypatch, tmp_path, external_id: str) -> None:
    """Point listings_photos_dir at tmp and drop one fake JPEG so stage 4 can run."""
    monkeypatch.setattr(settings, "listings_photos_dir", tmp_path / "listings")
    d = tmp_path / "listings" / Platform.NEKRETNINE.value / external_id
    d.mkdir(parents=True)
    (d / "000.jpg").write_bytes(b"\xff\xd8fake-jpeg")


async def _match_rows(factory) -> list[Match]:
    async with factory() as s:
        return list((await s.execute(select(Match))).scalars().all())


async def _inbound_status(factory, inbound_id: int) -> InboundStatus:
    async with factory() as s:
        return (await s.get(InboundPost, inbound_id)).status


# ---------------------------------------------------------------------------
# Stage 3 — text cross-check
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("caption", "listing_price", "listing_area"),
    [
        pytest.param("Сдается, цена 600€, 50 м²", 2000, 50.0, id="price-mismatch"),
        pytest.param("Сдается, 50 м²", None, 120.0, id="area-mismatch"),
    ],
)
async def test_clip_candidate_rejected_by_text_cross_check(
    db, settings_pin, llm_stub, caption, listing_price, listing_area
):
    """Perfect CLIP similarity (cosine 1.0 on both photos) is still NOT a match when the
    caption's price/area contradict the listing — and the LLM gate is never consulted."""
    factory, inbound_id, _listing_id = await _seed_clip_scenario(
        db, caption=caption, listing_price=listing_price, listing_area=listing_area
    )

    result = await match_inbound(inbound_id)

    assert result.status == STATUS_NO_MATCH
    assert result.listing_id is None
    assert result.matched_photos == 0  # candidate discarded entirely, not just down-ranked
    assert await _match_rows(factory) == []
    assert await _inbound_status(factory, inbound_id) == InboundStatus.NO_MATCH
    assert llm_stub.calls == []  # rejected before stage 4


async def test_text_cross_check_within_tolerance_passes(db, settings_pin, llm_stub, monkeypatch):
    """Counterpart of the rejection test: small price/area deltas (within ±25%/±20%) keep
    the candidate. Catches an inverted or over-strict cross-check."""
    monkeypatch.setattr(settings_pin, "enable_llm_validation", False)  # isolate stage 3
    factory, inbound_id, listing_id = await _seed_clip_scenario(
        db, caption="Сдается, цена 600€, 50 м²", listing_price=650, listing_area=52.0
    )

    result = await match_inbound(inbound_id)

    assert (result.status, result.listing_id, result.method) == (
        STATUS_MATCHED, listing_id, METHOD_CLIP,
    )
    assert await _inbound_status(factory, inbound_id) == InboundStatus.MATCHED
    assert llm_stub.calls == []


# ---------------------------------------------------------------------------
# Stage 2 — min_matched_photos floor
# ---------------------------------------------------------------------------


async def test_min_matched_photos_floor_one_perfect_photo_is_not_enough(
    db, settings_pin, llm_stub
):
    """The listing nails ONE inbound photo with cosine exactly 1.0 — still NO_MATCH:
    min_matched_photos=2 is a floor on distinct matched photos, not on similarity."""
    factory, inbound_id, _listing_id = await _seed_clip_scenario(
        db, caption="", listing_vecs=[unit_vec(0)]  # listing only has inbound photo #0
    )

    result = await match_inbound(inbound_id)

    assert result.status == STATUS_NO_MATCH
    assert result.listing_id is None
    assert result.matched_photos == 1  # CLIP did see the one perfect photo
    assert result.confidence == pytest.approx(0.5)  # mean of per-photo best sims (1.0, 0.0)
    assert await _match_rows(factory) == []
    assert await _inbound_status(factory, inbound_id) == InboundStatus.NO_MATCH
    assert llm_stub.calls == []


# ---------------------------------------------------------------------------
# Stage 4 — LLM gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("llm_conf", [0.95, 0.7], ids=["above-threshold", "exactly-at-threshold"])
async def test_llm_accept_yields_clip_llm_method_and_persists_match(
    db, tmp_path, monkeypatch, settings_pin, llm_stub, llm_conf
):
    """LLM says same apartment with confidence ≥ threshold (0.7, boundary inclusive) →
    MATCHED with method clip+llm. Persisted confidence stays the CLIP score, NOT the
    LLM's confidence."""
    factory, inbound_id, listing_id = await _seed_clip_scenario(db)
    _put_listing_photos(settings_pin, monkeypatch, tmp_path, "w4t1-clip")
    llm_stub.result = DuplicateCheck(is_same_apartment=True, confidence=llm_conf, reasoning="ok")

    result = await match_inbound(inbound_id)

    assert result.status == STATUS_MATCHED
    assert result.listing_id == listing_id
    assert result.method == METHOD_CLIP_LLM
    assert result.matched_photos == 2
    assert result.confidence == 1.0  # CLIP mean-top similarity, not llm_conf

    rows = await _match_rows(factory)
    assert len(rows) == 1
    row = rows[0]
    assert result.match_id == row.id
    assert (row.inbound_post_id, row.listing_id) == (inbound_id, listing_id)
    assert row.method == METHOD_CLIP_LLM
    assert row.matched_photo_count == 2
    assert row.confidence == 1.0
    assert row.status == MatchStatus.PENDING
    assert await _inbound_status(factory, inbound_id) == InboundStatus.MATCHED

    assert len(llm_stub.calls) == 1
    call = llm_stub.calls[0]
    assert (call["inbound_id"], call["listing_id"]) == (inbound_id, listing_id)


@pytest.mark.parametrize(
    ("same", "conf"),
    [
        pytest.param(True, 0.69, id="same-but-below-confidence"),
        pytest.param(False, 0.99, id="different-apartment"),
    ],
)
async def test_llm_reject_kills_clip_match(
    db, tmp_path, monkeypatch, settings_pin, llm_stub, same, conf
):
    """LLM gate rejects (same=False, or same=True below min confidence) → NO_MATCH with
    the dedicated clip+llm_rejected label and NO persisted Match row."""
    factory, inbound_id, _listing_id = await _seed_clip_scenario(db)
    _put_listing_photos(settings_pin, monkeypatch, tmp_path, "w4t1-clip")
    llm_stub.result = DuplicateCheck(is_same_apartment=same, confidence=conf, reasoning="no")

    result = await match_inbound(inbound_id)

    assert result.status == (STATUS_PENDING if same else STATUS_NO_MATCH)
    assert result.listing_id is None
    assert result.method == (METHOD_CLIP if same else METHOD_CLIP_LLM_REJECTED)
    assert result.matched_photos == 2  # CLIP agreed — the rejection came from the LLM stage
    assert await _match_rows(factory) == []
    assert await _inbound_status(factory, inbound_id) == (InboundStatus.NEW if same else InboundStatus.NO_MATCH)
    assert len(llm_stub.calls) == 1


@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(
            LLMValidatorError("Anthropic API still overloaded after 4 retries: 529"),
            id="validator-error",
        ),
        pytest.param(RuntimeError("validator crashed"), id="unexpected-exception"),
    ],
)
async def test_llm_failure_is_durable_pending_without_match(
    db, tmp_path, monkeypatch, settings_pin, llm_stub, exc
):
    """Transport/validator failures must NOT drop a good CLIP match: the verdict stands,
    but the method label stays plain `clip` (fail-open ≠ LLM-confirmed)."""
    factory, inbound_id, _listing_id = await _seed_clip_scenario(db)
    _put_listing_photos(settings_pin, monkeypatch, tmp_path, "w4t1-clip")
    llm_stub.exc = exc

    result = await match_inbound(inbound_id)

    assert result.status == STATUS_PENDING
    assert result.listing_id is None
    assert result.match_id is None
    assert await _match_rows(factory) == []
    assert await _inbound_status(factory, inbound_id) == InboundStatus.NEW
    assert len(llm_stub.calls) == 1


async def test_missing_listing_photos_require_review(
    db, tmp_path, monkeypatch, settings_pin, llm_stub
):
    """No listing photos on disk → stage 4 cannot run; treated like fail-open and the
    validator is never even invoked."""
    factory, inbound_id, _listing_id = await _seed_clip_scenario(db)
    monkeypatch.setattr(settings_pin, "listings_photos_dir", tmp_path / "listings")  # empty

    result = await match_inbound(inbound_id)

    assert result.status == STATUS_PENDING
    assert result.match_id is None
    assert await _match_rows(factory) == []
    assert llm_stub.calls == []


# ---------------------------------------------------------------------------
# Method labels — pure pHash and pure CLIP paths
# ---------------------------------------------------------------------------


async def test_pure_phash_match_labels_and_no_clip_or_llm_involvement(
    db, monkeypatch, settings_pin, llm_stub
):
    """2 of 3 inbound photos within Hamming ≤6 (one at exactly 6 — boundary inclusive;
    a 7-bit neighbour must NOT count) → method phash, confidence = matched/total photos,
    and neither CLIP scoring nor the LLM gate ever run."""
    h0, h1, h2 = make_phash("0"), make_phash("f"), make_phash("a")
    factory = async_sessionmaker(db, expire_on_commit=False)
    async with factory() as s:
        ch = SourceChannel(tg_id=-74002, username="w4t1ph", is_active=True)
        s.add(ch)
        await s.flush()
        listing = Listing(
            platform=Platform.CETIRI_ZIDA,
            external_id="w4t1-ph",
            url="https://example.com/w4t1-ph",
            photo_urls=["a", "b", "c"],
            photo_phashes=[
                flip_bits(h0, [0, 9, 18, 27, 36, 45]),      # Hamming 6 → counts (≤ max)
                flip_bits(h1, [0, 9, 18, 27, 36, 45, 54]),  # Hamming 7 → must NOT count
                flip_bits(h2, [63]),                        # Hamming 1 → counts
            ],
            price_eur=600,
        )
        inbound = InboundPost(
            source_channel_id=ch.id,
            tg_message_id=2,
            photo_paths=["/tmp/w4t1-ph-0.jpg", "/tmp/w4t1-ph-1.jpg", "/tmp/w4t1-ph-2.jpg"],
            photo_phashes=[h0, h1, h2],  # no embeddings → stage 2 has nothing to score
            status=InboundStatus.NEW,
        )
        s.add_all([listing, inbound])
        await s.commit()
        inbound_id, listing_id = inbound.id, listing.id

    clip_chunks: list = []
    real_chunk = matcher_mod._clip_score_chunk

    def chunk_spy(*args, **kwargs):
        clip_chunks.append(args)
        return real_chunk(*args, **kwargs)

    monkeypatch.setattr(matcher_mod, "_clip_score_chunk", chunk_spy)

    result = await match_inbound(inbound_id)

    assert result.status == STATUS_MATCHED
    assert result.listing_id == listing_id
    assert result.method == METHOD_PHASH
    assert result.matched_photos == 2
    assert result.confidence == round(2 / 3, 3)  # phash confidence = matched/total photos

    rows = await _match_rows(factory)
    assert len(rows) == 1
    assert rows[0].method == METHOD_PHASH
    assert rows[0].matched_photo_count == 2
    assert rows[0].confidence == round(2 / 3, 3)
    assert rows[0].status == MatchStatus.PENDING
    assert await _inbound_status(factory, inbound_id) == InboundStatus.MATCHED

    assert clip_chunks == []  # stage 2 never ran
    assert llm_stub.calls == []  # stage 4 never runs for pHash verdicts


async def test_pure_clip_method_when_llm_disabled(db, monkeypatch, settings_pin, llm_stub):
    """enable_llm_validation=False → stage 4 entirely off; method stays plain `clip` and
    the validator is never called."""
    monkeypatch.setattr(settings_pin, "enable_llm_validation", False)
    factory, inbound_id, listing_id = await _seed_clip_scenario(db)

    result = await match_inbound(inbound_id)

    assert result.status == STATUS_MATCHED
    assert result.listing_id == listing_id
    assert result.method == METHOD_CLIP
    assert result.matched_photos == 2
    assert result.confidence == 1.0
    rows = await _match_rows(factory)
    assert len(rows) == 1
    assert rows[0].method == METHOD_CLIP
    assert rows[0].confidence == 1.0
    assert await _inbound_status(factory, inbound_id) == InboundStatus.MATCHED
    assert llm_stub.calls == []
