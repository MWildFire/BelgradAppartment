"""E2E (S1-2): card grid visual invariants — equal-height cards, pinned CTA, zero overflow.

Drives the seeded catalog with headless chromium and measures real layout boxes:
- Every card in a grid ROW shares the same bottom edge (equal height).
- The .card-cta button bottom edges line up across cards in a row (pinned to bottom).
- The CTA button sits INSIDE its card's bounding box (never overflows onto the page bg).
- The CTA does NOT balloon over the card body — its height stays a small fraction of the card and
  the price (the card link's accessible name) stays visible (regression guard for the Wave 4
  card-cta-balloon blocker: .card-l>a{flex:1} used to match the CTA anchor too).
- Zero horizontal overflow at 320px and 1920px (scrollWidth <= clientWidth).

Gated by RUN_E2E=1 (conftest.requires_e2e). The seed has 3 public listings but C1 hides the
rented one by default, so the catalog renders 2 visible cards in one row at desktop width —
enough to assert row alignment + overflow bounds.
"""

from __future__ import annotations

from tests.e2e.conftest import requires_e2e

_EPS = 1.5  # sub-pixel rounding tolerance (px)


def _row_groups(boxes: list[dict]) -> list[list[dict]]:
    """Group card boxes into visual rows by their top coordinate (within tolerance)."""
    rows: list[list[dict]] = []
    for b in sorted(boxes, key=lambda x: (round(x["top"]), x["left"])):
        placed = False
        for row in rows:
            if abs(row[0]["top"] - b["top"]) <= 4:
                row.append(b)
                placed = True
                break
        if not placed:
            rows.append([b])
    return rows


@requires_e2e
def test_cards_equal_height_and_cta_pinned(page, site_server: str) -> None:
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(f"{site_server}/catalog", wait_until="networkidle")

    cards = page.locator("#results .card-l")
    n = cards.count()
    assert n >= 2, f"need >=2 cards to compare alignment, got {n}"

    # Collect each card's box + its CTA box.
    boxes: list[dict] = []
    for i in range(n):
        card = cards.nth(i)
        cb = card.bounding_box()
        cta = card.locator(".card-cta")
        assert cta.count() == 1, "each card must have exactly one .card-cta button"
        tb = cta.bounding_box()
        assert cb is not None and tb is not None
        # Regression guard (Wave 4 blocker): the CTA must not balloon over the card body.
        assert tb["height"] <= cb["height"] * 0.4, (
            f"CTA balloons over card body: cta_h={tb['height']:.0f} is "
            f"{tb['height'] / cb['height']:.0%} of card_h={cb['height']:.0f} (must be <40%)"
        )
        # The price (part of the card link's accessible name) must stay visible, not be covered.
        price = card.locator(".body .price")
        assert price.count() == 1 and price.first.is_visible(), "card price hidden/covered by CTA"
        pb = price.first.bounding_box()
        assert pb is not None and pb["height"] > 0, "card price has zero height (covered)"
        boxes.append({
            "top": cb["y"], "bottom": cb["y"] + cb["height"], "left": cb["x"],
            "cta_top": tb["y"], "cta_bottom": tb["y"] + tb["height"],
            "cta_left": tb["x"], "cta_right": tb["x"] + tb["width"],
            "card_right": cb["x"] + cb["width"],
        })

    for row in _row_groups(boxes):
        if len(row) < 2:
            continue
        # 1) equal height → bottom edges align.
        bottoms = [b["bottom"] for b in row]
        assert max(bottoms) - min(bottoms) <= _EPS, (
            f"card bottom edges not aligned (equal-height failed): {bottoms}"
        )
        # 2) CTA pinned to bottom → CTA bottoms align across the row.
        cta_bottoms = [b["cta_bottom"] for b in row]
        assert max(cta_bottoms) - min(cta_bottoms) <= _EPS, (
            f"CTA bottom edges not aligned (pinned-CTA failed): {cta_bottoms}"
        )
        # 3) CTA stays INSIDE its card horizontally (never overflows onto the page bg).
        for b in row:
            assert b["cta_left"] >= b["left"] - _EPS, "CTA overflows card on the left"
            assert b["cta_right"] <= b["card_right"] + _EPS, "CTA overflows card on the right"
            assert b["cta_bottom"] <= b["bottom"] + _EPS, "CTA overflows below card bottom"


@requires_e2e
def test_zero_horizontal_overflow_320_and_1920(page, site_server: str) -> None:
    for width in (320, 1920):
        page.set_viewport_size({"width": width, "height": 900})
        page.goto(f"{site_server}/catalog", wait_until="networkidle")
        scroll_w, client_w = page.evaluate(
            "() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]"
        )
        assert scroll_w <= client_w + 1, (
            f"horizontal overflow at {width}px: scrollWidth={scroll_w} > clientWidth={client_w}"
        )
