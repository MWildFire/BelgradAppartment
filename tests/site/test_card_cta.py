"""Stage S Wave 1 — card CTA + equal-height structure (S1-1 / S1-2).

Render-smoke layer (like test_islands_catalog.py): renders the REAL _card.html through a plain
Jinja env and asserts the STRUCTURE that the visual fixes depend on — the CTA is a styled
button-class anchor that lives INSIDE the <article> card (sibling of the card link, not nested),
points at /apt/{id}#lead, and that the CSS carries the rules pinning the CTA to the bottom and
clamping the title. Visual invariants (bottom-edges align, zero overflow) are the Playwright
e2e in tests/e2e/site/test_card_grid.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "src" / "site" / "templates"
CSS_FILE = Path(__file__).resolve().parents[2] / "src" / "site" / "static" / "css" / "site.css"


@dataclass(frozen=True)
class FakeView:
    id: int = 42
    price_eur: int = 650
    area_m2: float | None = 48.0
    rooms: str = "1+1"
    floor: str | None = "3/5"
    district: str = "Врачар"
    address: str | None = None
    lat: float | None = 44.8
    lon: float | None = 20.47
    for_vnj: bool = True
    for_white_card: bool = False
    is_rented: bool = False
    photos: list[str] = field(default_factory=lambda: ["/photos/a.webp"])
    note: str | None = None
    channel_url: str = "https://t.me/RentBelgrad/42"
    posted_at: str = "2026-06-18T10:00:00"


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
    )


def _render_card(**over: object) -> str:
    item = FakeView(**over)  # type: ignore[arg-type]
    return _env().get_template("partials/_card.html").render(item=item)


def _css() -> str:
    return CSS_FILE.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- S1-1: CTA button

def test_cta_is_styled_button_class_not_bare_link() -> None:
    """The CTA must carry .card-cta AND a real button class (.btn family) so it is styled,
    not default-blue browser link text."""
    html = _render_card()
    # the CTA anchor exists and carries the card-cta hook
    assert 'class="card-cta' in html or "card-cta" in html
    m = re.search(r'<a[^>]*class="([^"]*card-cta[^"]*)"', html)
    assert m, "expected an <a> with a card-cta class"
    classes = m.group(1).split()
    # styled via the shared .btn system (so hover/focus/active states apply) — not a bare link
    assert "btn" in classes, f"card-cta must reuse the .btn button system, got: {classes}"


def test_cta_points_at_detail_lead_anchor() -> None:
    html = _render_card(id=7)
    assert 'href="/apt/7#lead"' in html


def test_cta_lives_inside_the_article_card() -> None:
    """The CTA must sit INSIDE the <article class="card-l"> container (so it can never overflow
    onto the beige page background) and be a SIBLING of the card link (valid HTML — no <a> in <a>)."""
    html = _render_card()
    article = re.search(r"<article[^>]*class=\"card-l[^\"]*\".*?</article>", html, re.S)
    assert article, "card-l <article> wrapper not found"
    block = article.group(0)
    assert "card-cta" in block, "CTA must be inside the <article> card container"
    # CTA is NOT nested inside the card's main <a href=/apt/{id}> link.
    # Slice up to the START of the CTA's own <a ...card-cta...> opening tag, then check that every
    # anchor opened before it has already closed (no open anchor swallowing the CTA → valid HTML).
    cta_open = re.search(r"<a\b[^>]*card-cta", block)
    assert cta_open, "CTA <a ...card-cta...> opening tag not found"
    before = block[: cta_open.start()]
    opens = len(re.findall(r"<a\b", before))
    closes = before.count("</a>")
    assert opens == closes, "card-cta appears to be nested inside the card <a> (invalid HTML)"


def test_cta_has_accessible_text() -> None:
    html = _render_card()
    assert "Оставить заявку" in html


# --------------------------------------------------------------------------- S1-2: CSS rules

def test_css_grid_rows_stretch_for_equal_height() -> None:
    """Equal-height cards: the grid rows must stretch so every card in a row is the same height."""
    css = _css()
    # .cards rule sets grid-auto-rows:1fr (or align-items:stretch is the grid default, but rows
    # must be equalised — assert the explicit 1fr rows which makes 1- and 2-line cards match).
    cards_rule = re.search(r"\.cards\{[^}]*\}", css)
    assert cards_rule, ".cards grid rule not found"
    assert "grid-auto-rows:1fr" in cards_rule.group(0).replace(" ", ""), (
        ".cards must set grid-auto-rows:1fr so rows stretch to equal height"
    )


def test_css_cta_pinned_to_bottom_with_margin_top_auto() -> None:
    css = _css()
    cta_rule = re.search(r"\.card-cta\{[^}]*\}", css)
    assert cta_rule, ".card-cta CSS rule not found"
    body = cta_rule.group(0).replace(" ", "")
    assert "margin-top:auto" in body, ".card-cta must use margin-top:auto to pin to the card bottom"


def test_css_title_line_clamp_with_min_height() -> None:
    """Title clamps to 2 lines and reserves min-height for 2 lines so 1- and 2-line cards match."""
    css = _css()
    ttl_rule = re.search(r"\.card-l \.ttl\{[^}]*\}", css)
    assert ttl_rule, ".card-l .ttl rule not found"
    body = ttl_rule.group(0).replace(" ", "")
    assert "line-clamp:2" in body or "-webkit-line-clamp:2" in body, (
        ".ttl must clamp to 2 lines"
    )
    assert "min-height" in body, ".ttl must reserve a min-height for 2 lines (height parity)"


def test_css_photo_wrapper_is_block_for_aspect_ratio() -> None:
    """Stage R bug guard: the photo lives in an <a> whose default display:inline IGNORES
    aspect-ratio. The card link (or the .ph wrapper) must be display:block."""
    css = _css()
    # the card's main link must be a block-level box (display:block OR flex) so the child .ph
    # aspect-ratio applies — an inline <a> would ignore it (the Stage R .hero-photo bug).
    # Selector may scope out the CTA (.card-l>a:not(.card-cta)) — that's the Wave 4 balloon fix.
    block_rule = re.search(r"\.card-l\s*>\s*a(?::not\([^)]*\))?\s*\{[^}]*\}", css)
    assert block_rule, "expected a .card-l>a rule forcing a block-level display on the photo link"
    body = block_rule.group(0).replace(" ", "")
    assert "display:block" in body or "display:flex" in body, (
        ".card-l>a must be block-level (block/flex) so .ph aspect-ratio is honored"
    )
