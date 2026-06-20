"""WCAG-AA + impeccable static checks for the public catalog (Stage W, W5-H1).

No browser: every assertion is a static check over the RENDERED templates (plain Jinja env,
same seam as test_render.py) or over the owned CSS file. We assert load-bearing a11y/SEO nodes:

- skip-to-content link + <html lang="ru"> in the shell;
- every <img> in every rendered template carries an ``alt`` attribute;
- per-listing OpenGraph (og:title / og:description / og:image) on apt.html, absolute via
  the ``site_base_url`` Jinja global, plus base.html default OG + twitter:card;
- site.css carries :focus-visible rules and a prefers-reduced-motion block, and the marquee
  animation is disabled inside that block;
- the computed WCAG contrast for the body/caption combos actually used clears AA (4.5:1).

Runtime keyboard/focus BEHAVIOR is Wave 5 Playwright (agent G) — here we prove the hooks ship.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES_DIR = ROOT / "src" / "site" / "templates"
SITE_CSS = ROOT / "src" / "site" / "static" / "css" / "site.css"

# Absolute base used to build OG image URLs (mirror of the site_public_base_url setting; the
# route exposes it as the ``site_base_url`` Jinja global next to csrf_token).
SITE_BASE = "https://rent.example.com"


@dataclass(frozen=True)
class FakeView:
    """Mirror of SiteListingView (DATA_CONTRACT §1) — only fields templates may read."""

    id: int = 128
    price_eur: int = 650
    area_m2: float | None = 48.0
    rooms: str = "1+1"
    floor: str | None = "3/5"
    district: str = "Врачар"
    address: str | None = None
    lat: float | None = 44.8
    lon: float | None = 20.47
    for_vnj: bool = True
    for_white_card: bool = True
    is_rented: bool = False
    photos: list[str] = field(default_factory=lambda: ["/photos/a.webp", "/photos/b.webp"])
    note: str | None = "Хозяин оформит ugovor на 12 месяцев."
    channel_url: str = "https://t.me/RentBelgrad/128"
    posted_at: str = "2026-06-18T10:00:00"


def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
    )
    # The route registers this global; render-time tests must supply it too.
    env.globals["site_base_url"] = SITE_BASE
    return env


def _render(name: str, **ctx: object) -> str:
    return _env().get_template(name).render(**ctx)


_STATS = {"active": 47, "districts": 8}
_CSRF = "test-csrf-token"
_FACETS = {
    "districts": [("Врачар", 7), ("Дорћол", 3)],
    "price_bounds": (450, 900),
    "rooms": [("1+1", 5)],
    "counts": {"vnj": 6, "white_card": 4, "total": 10},
}
_APPLIED = {
    "vnj": True,
    "wc": False,
    "district": ["Врачар"],
    "price_min": None,
    "price_max": 700,
    "rooms": ["1"],
    "sort": "new",
    "show_rented": False,
}

# Every full page rendered through base.html, with the context each one needs.
_PAGES: list[tuple[str, dict[str, object]]] = [
    ("base.html", {"csrf_token": _CSRF}),
    ("index.html", {"featured": [FakeView()], "stats": _STATS, "csrf_token": _CSRF}),
    (
        "catalog.html",
        {"items": [FakeView()], "facets": _FACETS, "applied": _APPLIED, "total": 1, "csrf_token": _CSRF},
    ),
    ("apt.html", {"item": FakeView(), "similar": [FakeView(id=200)], "csrf_token": _CSRF}),
    ("map.html", {"csrf_token": _CSRF}),
    ("calculator.html", {"csrf_token": _CSRF}),
]

# Img-bearing fragments rendered standalone, with context.
_IMG_FRAGMENTS: list[tuple[str, dict[str, object]]] = [
    ("partials/_card.html", {"item": FakeView()}),
    ("index.html", {"featured": [FakeView()], "stats": _STATS, "csrf_token": _CSRF}),
    ("apt.html", {"item": FakeView(), "similar": [FakeView(id=200)], "csrf_token": _CSRF}),
]


# =============================================================== skip-link / lang / structure
def test_base_has_skip_link() -> None:
    html = _render("base.html", csrf_token=_CSRF)
    assert 'class="skip-link"' in html
    assert 'href="#main"' in html
    assert 'id="main"' in html  # the skip target exists


def test_base_html_lang_ru() -> None:
    html = _render("base.html", csrf_token=_CSRF)
    assert '<html lang="ru"' in html


@pytest.mark.parametrize(("template", "ctx"), _PAGES)
def test_pages_have_single_h1_at_most(template: str, ctx: dict[str, object]) -> None:
    """Logical heading order: a content page has at most one <h1> (the page title)."""
    if template == "base.html":
        pytest.skip("base shell has no content <h1> by itself")
    html = _render(template, **ctx)
    assert len(re.findall(r"<h1[ >]", html)) <= 1


def test_footer_headings_no_level_skip() -> None:
    """Footer column titles must not be <h5>/<h4> (would skip levels after section <h2>s)."""
    html = _render("base.html", csrf_token=_CSRF)
    foot = html[html.find("<footer") :]
    assert "<h5" not in foot, "footer uses <h5> — skips heading levels (WCAG heading-order)"
    assert "<h2>Каталог</h2>" in foot  # promoted to a sequential level


def test_card_link_has_no_paraphrasing_aria_label() -> None:
    """WCAG 2.5.3: the card link's accessible name must come from its visible content,
    not a paraphrasing aria-label that omits the visible text ("в районе")."""
    html = _render("partials/_card.html", item=FakeView())
    link = re.search(r"<a href=\"/apt/\d+\"[^>]*>", html)
    assert link, "card detail link not found"
    assert "aria-label=" not in link.group(0)


# =========================================================================== images have alt
@pytest.mark.parametrize(("template", "ctx"), _IMG_FRAGMENTS)
def test_every_img_has_alt(template: str, ctx: dict[str, object]) -> None:
    """Every <img> rendered must carry an alt attribute (meaningful or empty for decorative)."""
    html = _render(template, **ctx)
    imgs = re.findall(r"<img\b[^>]*>", html)
    assert imgs, f"expected at least one <img> in {template}"
    for img in imgs:
        assert "alt=" in img, f"<img> without alt in {template}: {img}"


# ================================================================================ OpenGraph
def test_base_has_default_opengraph_and_twitter() -> None:
    html = _render("base.html", csrf_token=_CSRF)
    assert 'property="og:title"' in html
    assert 'property="og:description"' in html
    assert 'property="og:type"' in html
    assert 'name="twitter:card"' in html


def test_apt_has_per_listing_opengraph_absolute_image() -> None:
    item = FakeView()
    html = _render("apt.html", item=item, similar=[], csrf_token=_CSRF)
    # og:title carries price + district (the listing identity).
    m = re.search(r'<meta property="og:title" content="([^"]*)"', html)
    assert m, "og:title missing on apt.html"
    og_title = m.group(1)
    assert str(item.price_eur) in og_title
    assert item.district in og_title
    # og:description present.
    assert 'property="og:description"' in html
    # og:image is ABSOLUTE — built from the site_base_url global + the first photo.
    m_img = re.search(r'<meta property="og:image" content="([^"]*)"', html)
    assert m_img, "og:image missing on apt.html"
    assert m_img.group(1) == SITE_BASE + item.photos[0]
    # twitter card present per-listing too.
    assert 'name="twitter:card"' in html


def test_apt_opengraph_no_photo_falls_back_gracefully() -> None:
    """A listing with no photos must not emit a dangling/relative og:image."""
    html = _render("apt.html", item=FakeView(photos=[]), similar=[], csrf_token=_CSRF)
    m_img = re.search(r'<meta property="og:image" content="([^"]*)"', html)
    if m_img:  # if present it must still be absolute (no bare relative path)
        assert m_img.group(1).startswith("http")


# ============================================================================ focus-visible
def test_css_has_focus_visible_rules() -> None:
    css = SITE_CSS.read_text(encoding="utf-8")
    assert ":focus-visible" in css
    # an outline (or an equally-visible ring) must be defined, not removed
    assert re.search(r":focus-visible\s*{[^}]*outline", css) or re.search(
        r":focus-visible\s*{[^}]*box-shadow", css
    )


def test_css_never_kills_focus_without_replacement() -> None:
    """A `:focus` state must never set `outline:none` unless scoped `:focus:not(:focus-visible)`.

    A bare `:focus{outline:none}` also matches keyboard focus (which is :focus-visible), and if
    it has equal-or-higher specificity / later source order it suppresses the ring — the exact
    WCAG 2.4.7 regression a string-presence check misses. Base-element `outline:none` (no :focus
    in the selector) is allowed because a higher-specificity :focus-visible rule restores the ring.
    """
    css = SITE_CSS.read_text(encoding="utf-8")
    assert ":focus-visible" in css, "outline removed but no :focus-visible ring defined"
    assert re.search(r":focus-visible\s*{[^}]*outline\s*:[^;}]*solid", css)
    offenders: list[str] = []
    for m in re.finditer(r"([^{}]+)\{([^}]*)\}", css):
        selector, decls = m.group(1), m.group(2)
        if not re.search(r"outline\s*:\s*none", decls):
            continue
        # selector uses :focus as a state (not :focus-visible)?
        if not re.search(r":focus\b(?!-visible)", selector):
            continue  # base-element outline:none — ring restored by higher-specificity :focus-visible
        if ":not(:focus-visible)" not in selector:
            offenders.append(selector.strip()[:120])
    assert not offenders, f":focus rules drop the ring without :not(:focus-visible) guard: {offenders}"


# ======================================================================= reduced-motion
def test_css_has_reduced_motion_block() -> None:
    css = SITE_CSS.read_text(encoding="utf-8")
    assert "@media (prefers-reduced-motion: reduce)" in css or "@media(prefers-reduced-motion:reduce)" in css


def _reduced_motion_blocks(css: str) -> str:
    """Return the concatenated bodies of every @media (prefers-reduced-motion: reduce) block.

    `re` can't balance nested braces, so we hand-scan brace depth from each media-query head.
    """
    out: list[str] = []
    for m in re.finditer(r"@media\s*\(prefers-reduced-motion:\s*reduce\)\s*{", css):
        i = m.end()
        depth = 1
        while i < len(css) and depth:
            if css[i] == "{":
                depth += 1
            elif css[i] == "}":
                depth -= 1
            i += 1
        out.append(css[m.end() : i - 1])
    return "\n".join(out)


def test_marquee_animation_inside_reduced_motion_guard() -> None:
    """The marquee track animation must be disabled under prefers-reduced-motion."""
    css = SITE_CSS.read_text(encoding="utf-8")
    body = _reduced_motion_blocks(css)
    assert ".strip .track" in body or ".track" in body
    assert "animation:none" in body.replace(" ", "")


def test_reduced_motion_disables_btn_and_card_transforms() -> None:
    css = SITE_CSS.read_text(encoding="utf-8")
    # gather the whole reduced-motion region (greedy to last brace of the section)
    idx = css.find("prefers-reduced-motion")
    assert idx != -1
    region = css[idx:]
    compact = region.replace(" ", "").replace("\n", "")
    # btn transforms + card-l hover lift neutralized
    assert "transition:none" in compact or "transform:none" in compact
    assert "animation:none" in compact


# ====================================================== computed WCAG contrast (AA) of usage
_TOKENS = {
    "paper": "#F4EFE6",
    "paper-2": "#ECE4D6",
    "paper-3": "#E3D8C5",
    "card": "#FBF8F1",
    "ink": "#1B1A17",
    "ink-2": "#4A463E",
    "ink-3": "#8A8275",
    "oxblood": "#A8362A",
    "navy": "#15324C",
    "sage": "#4F6B57",
    "white": "#FFFFFF",
}


def _lin(c: int) -> float:
    x = c / 255
    return x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4


def _lum(hexs: str) -> float:
    h = hexs.lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def _ratio(a: str, b: str) -> float:
    la, lb = _lum(_TOKENS[a]), _lum(_TOKENS[b])
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


# Body/caption text combos we ACTUALLY ship after the usage fix — all must clear 4.5:1.
@pytest.mark.parametrize(
    ("fg", "bg"),
    [
        ("ink", "paper"),
        ("ink-2", "paper"),
        ("ink-2", "card"),
        ("ink-2", "paper-2"),
        ("oxblood", "paper"),
        ("oxblood", "card"),
        ("navy", "paper"),
        ("sage", "paper"),
        ("white", "oxblood"),
        ("white", "navy"),
        ("white", "sage"),
        ("paper", "ink"),
        ("paper", "navy"),
    ],
)
def test_body_text_combos_meet_aa(fg: str, bg: str) -> None:
    assert _ratio(fg, bg) >= 4.5, f"{fg} on {bg} = {_ratio(fg, bg):.2f}:1 < 4.5"


def test_ink3_on_paper_is_below_aa_body_documented() -> None:
    """Guardrail: --ink-3 on --paper is < 4.5:1, so it must NOT be used for body text.

    This pins the measured ratio so a regression (someone re-applying --ink-3 to body copy)
    is caught. Decorative/large use (separators) is fine; meaningful captions use --ink-2.
    """
    assert _ratio("ink-3", "paper") < 4.5
    assert _ratio("ink-3", "paper") >= 3.0  # still OK for non-text / large/decorative


def test_meta_captions_do_not_use_ink3_for_meaningful_text() -> None:
    """Meaningful captions/labels must not be set in --ink-3 (fails AA body).

    Allowed --ink-3 survivors are decorative separators (.meta i, .apt-params i) and the
    large display caption (.empty .big inherits a darker tone). Everything else uses --ink-2.
    """
    css = SITE_CSS.read_text(encoding="utf-8")
    # These selectors carry meaningful small text and MUST NOT use ink-3.
    must_not_be_ink3 = [
        ".stat .l{",
        ".sec-head .idx{",
        ".feed-note{",
        ".card-l .price small{",
        ".results-head .count{",
        ".crumbs{",
        ".apt-side .per{",
        ".field label{",
        ".filters h3{",
        ".foot .col h2{",
        ".opt .cnt{",
        ".legal{",
    ]
    for sel in must_not_be_ink3:
        i = css.find(sel)
        assert i != -1, f"selector {sel!r} not found in site.css"
        rule = css[i : css.find("}", i)]
        assert "var(--ink-3)" not in rule, f"{sel} still uses --ink-3 (fails AA body)"
