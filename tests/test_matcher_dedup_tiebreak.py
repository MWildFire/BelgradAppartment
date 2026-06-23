"""A4 — matcher tie-break: when two candidates are otherwise EQUAL (same matched-photo
count and same score), prefer the cross-platform-dedup primary (is_primary=True).

This is a pure-function test on `_select_best`; it does not touch the DB or the scan order
contract beyond passing CandidateRows in a fixed order. The non-primary listing is iterated
FIRST so that, without the tie-break, it would win by first-seen; the tie-break must flip it.
"""

from __future__ import annotations

from src.config import get_settings
from src.matching.candidates import CandidateRow
from src.matching.inbound_parser import parse_inbound_text
from src.matching.matcher import METHOD_PHASH, _select_best

# Identical pHash on both listings → identical matched-photo count and pHash score → exact tie.
_PHASH = "0" * 16
_INBOUND_INTS_PHASHES = 2  # n_inbound_phashes


def _row(listing_id: int, is_primary: bool) -> CandidateRow:
    return CandidateRow(
        id=listing_id,
        phash_ints=(int(_PHASH, 16),),
        price_eur=None,
        area_m2=None,
        district=None,
        has_embs=False,
        is_primary=is_primary,
    )


def test_select_best_prefers_primary_on_exact_tie() -> None:
    settings = get_settings()
    parsed = parse_inbound_text("")

    non_primary = _row(10, is_primary=False)  # iterated first → would win without tie-break
    primary = _row(20, is_primary=True)
    rows = [non_primary, primary]

    # Both listings get the same pHash count (1) → same count, same pHash score → exact tie.
    ph_counts = {10: 1, 20: 1}
    clip_results: dict[int, tuple[int, float]] = {}

    best_id, best_count, _score, method = _select_best(
        rows, ph_counts, clip_results, parsed, _INBOUND_INTS_PHASHES, settings
    )
    assert best_id == 20  # the primary wins the tie
    assert best_count == 1
    assert method == METHOD_PHASH


def test_select_best_unaffected_when_no_tie() -> None:
    """A strictly-stronger non-primary still beats a weaker primary (tie-break only breaks ties)."""
    settings = get_settings()
    parsed = parse_inbound_text("")

    non_primary = _row(10, is_primary=False)
    primary = _row(20, is_primary=True)
    rows = [non_primary, primary]

    ph_counts = {10: 2, 20: 1}  # non-primary matches MORE photos → it must still win
    best_id, best_count, _score, _method = _select_best(
        rows, ph_counts, {}, parsed, _INBOUND_INTS_PHASHES, settings
    )
    assert best_id == 10
    assert best_count == 2
