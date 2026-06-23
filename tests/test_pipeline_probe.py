"""Tests for the synthetic «golden inbound» end-to-end pipeline probe (B2).

The probe seeds a self-contained, deterministic golden into an isolated in-memory DB:
a known Listing whose photo pHashes correspond to two committed golden images, plus a
known inbound carrying the SAME images. It then runs the REAL matcher entrypoint
(`src.matching.matcher.match_inbound`) and asserts a confident match back to the seeded
listing.

The golden deliberately matches through the **pHash path only** (the inbound carries no
CLIP embeddings → the matcher's `inbound_embs` is empty → `_select_best` picks METHOD_PHASH
and stage-3 text-check / LLM validation only run for CLIP candidates). This keeps the probe
free of the heavy/network CLIP model while still exercising the real phash + matching +
threshold + DB code, so a pHash/threshold regression trips it.

DB isolation mirrors tests/test_pipeline.py / tests/test_orchestrator_reseen.py: an
in-memory SQLite engine swapped into the global session factory by the probe itself
(injectable), so the probe never touches the real data/otz.db.
"""

from __future__ import annotations

import pytest

from src.jobs.probe import GOLDEN_EXTERNAL_ID, ProbeResult, run_pipeline_probe


@pytest.mark.asyncio
async def test_golden_inbound_matches_with_high_confidence() -> None:
    """The golden inbound MUST match the seeded golden listing through the real matcher,
    pointing at the expected external_id with confidence at/above the probe threshold."""
    result = await run_pipeline_probe()

    assert isinstance(result, ProbeResult)
    assert result.error is None, f"probe errored: {result.error}"
    assert result.matched is True
    assert result.listing_external_id == GOLDEN_EXTERNAL_ID
    # Two golden photos, both seeded identically → both pHash-match → confidence 1.0.
    assert result.confidence >= 0.99, result.confidence
    assert result.method == "phash"


@pytest.mark.asyncio
async def test_probe_reports_failure_when_golden_listing_absent() -> None:
    """The probe must be trustworthy: if the golden listing is NOT seeded (e.g. a pHash or
    matcher regression that would prevent the seed from ever matching), the probe REPORTS a
    non-match cleanly rather than crashing — so a real pipeline break surfaces as matched=False,
    not an exception that masquerades as infra noise."""
    result = await run_pipeline_probe(seed_listing=False)

    assert isinstance(result, ProbeResult)
    assert result.error is None, f"probe should report, not crash: {result.error}"
    assert result.matched is False
    assert result.listing_external_id is None
