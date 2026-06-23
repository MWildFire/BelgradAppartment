"""Synthetic «golden inbound» end-to-end pipeline probe (B2).

A deploy-time / periodic probe that catches regressions in the WHOLE matching pipeline
(pHash → candidate scan → thresholds → DB), not just the parser. It seeds a self-contained,
deterministic golden into an **isolated in-memory DB** and runs the REAL matcher entrypoint
(`src.matching.matcher.match_inbound`), asserting a confident match back to the seeded listing.

Why deterministic instead of prod-data-dependent
-------------------------------------------------
A probe pinned to a live prod listing is flaky — listings churn and deactivate. Instead we
commit two tiny golden PNGs (`tests/fixtures/golden/photo_{a,b}.png`) and seed BOTH a Listing
and an inbound from the same two images. Their real pHashes (computed via the production
`compute_phash_from_path`) are bit-identical on both sides, so the listing collects two pHash
hits — meeting `min_matched_photos` (default 2) with Hamming distance 0.

pHash-only by design (no CLIP / no LLM)
---------------------------------------
The seeded inbound carries **no** CLIP embeddings, so the matcher's `inbound_embs` is empty:
stage 2 is skipped, `_select_best` picks `METHOD_PHASH`, and the stage-3 text cross-check and
stage-4 LLM validation only run for `METHOD_CLIP` candidates. A strong pHash match therefore
yields a decision on its own — no model download, no network, no API key required. This keeps
the probe runnable in any environment (incl. CI) while still exercising the real phash +
candidate-scan + threshold + DB code, so a pHash/threshold/DB regression trips it.

Isolation
---------
The probe builds its own in-memory SQLite engine and swaps it into the global session factory
(`src.db.session`) for the duration of the run, restoring the originals in a finally block — so
it never reads from or writes to the real `data/otz.db`. The DB/golden are injectable
(`engine`, `seed_listing`) so the unit test drives the probe deterministically, including the
"golden absent" trust case.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

import src.db.session as db_session
from src.db.models import Base, InboundPost, InboundStatus, Listing, Platform, SourceChannel
from src.matching.matcher import STATUS_MATCHED, match_inbound
from src.matching.phash import compute_phash_from_path

logger = logging.getLogger(__name__)

# Stable identity of the seeded golden listing — the probe asserts the match points here.
GOLDEN_EXTERNAL_ID = "golden-0001"
GOLDEN_PLATFORM = Platform.NEKRETNINE

# Committed golden images (deterministic, ~6 KB each). Both are seeded as the listing's photos
# AND the inbound's photos, so the pipeline sees two bit-identical pHash pairs.
_GOLDEN_DIR = Path(__file__).resolve().parent.parent.parent / "tests" / "fixtures" / "golden"
_GOLDEN_PHOTOS = ("photo_a.png", "photo_b.png")

# Minimum confidence we require from the probe. Two identical photos → pHash confidence 1.0;
# a comfortable floor below that flags any threshold/pHash regression that erodes the score.
PROBE_MIN_CONFIDENCE = 0.99


@dataclass
class ProbeResult:
    """Outcome of one probe run. `error` is set only on an unexpected failure (the probe
    reports a clean non-match as matched=False, error=None — see the absent-golden case)."""

    matched: bool
    listing_external_id: str | None
    confidence: float
    method: str | None
    error: str | None = None


def _golden_phashes() -> list[str]:
    """Real pHashes of the committed golden images via the production hashing code."""
    return [compute_phash_from_path(_GOLDEN_DIR / name) for name in _GOLDEN_PHOTOS]


async def run_pipeline_probe(
    *,
    engine: AsyncEngine | None = None,
    seed_listing: bool = True,
) -> ProbeResult:
    """Seed the golden into an isolated DB, run the REAL matcher, return the verdict.

    Args:
        engine: optional pre-built async engine to use (tests may inject one). When omitted the
            probe creates a throwaway in-memory SQLite engine. Either way the engine is swapped
            into the global session factory for the run and restored afterwards.
        seed_listing: when False, the golden Listing is NOT seeded — used to verify the probe
            reports a non-match cleanly (matched=False) instead of crashing.

    Never raises for an ordinary "no match" — only truly unexpected errors land in
    `ProbeResult.error`.
    """
    own_engine = engine is None
    if engine is None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False, future=True)

    orig_engine = db_session._engine
    orig_factory = db_session._session_factory
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        db_session._engine = engine
        db_session._session_factory = factory

        phashes = _golden_phashes()

        async with factory() as s:
            channel = SourceChannel(tg_id=-1, username="golden-probe", is_active=True)
            s.add(channel)
            await s.flush()

            if seed_listing:
                s.add(
                    Listing(
                        platform=GOLDEN_PLATFORM,
                        external_id=GOLDEN_EXTERNAL_ID,
                        url=f"https://www.nekretnine.rs/oglasi/{GOLDEN_EXTERNAL_ID}/",
                        photo_urls=list(_GOLDEN_PHOTOS),
                        photo_phashes=phashes,
                        is_active=True,
                    )
                )

            inbound = InboundPost(
                source_channel_id=channel.id,
                tg_message_id=1,
                raw_text="golden synthetic probe",
                photo_paths=[str(_GOLDEN_DIR / name) for name in _GOLDEN_PHOTOS],
                photo_phashes=phashes,
                status=InboundStatus.NEW,
            )
            s.add(inbound)
            await s.flush()
            inbound_id = inbound.id
            await s.commit()

        result = await match_inbound(inbound_id)

        matched = result.status == STATUS_MATCHED
        external_id: str | None = None
        if matched and result.listing_id is not None:
            async with factory() as s:
                listing = await s.get(Listing, result.listing_id)
                external_id = listing.external_id if listing is not None else None

        return ProbeResult(
            matched=matched,
            listing_external_id=external_id,
            confidence=result.confidence,
            method=result.method,
            error=None,
        )
    except Exception as e:  # noqa: BLE001 — the probe must report, never crash the caller.
        logger.exception("[probe] pipeline probe failed unexpectedly")
        return ProbeResult(
            matched=False,
            listing_external_id=None,
            confidence=0.0,
            method=None,
            error=str(e) or type(e).__name__,
        )
    finally:
        db_session._engine = orig_engine
        db_session._session_factory = orig_factory
        if own_engine:
            await engine.dispose()
