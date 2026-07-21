"""Wave 4 island wiring — render-smoke for the EXTRAS islands (agent F).

Two client-side islands plus their template hooks:
- W4-F2 favorites: a localStorage save control on every card + apt detail (data-fav-id),
  a counter badge in the nav, and favorites.js loaded site-wide.
- W4-F3 lead form: honeypot name="company" + csrf_token field + lead.js wiring, with the
  page-wide hx-headers X-CSRF-Token (mirroring admin) so the hx-post passes site CSRF.

Like test_render.py, these render the REAL templates through a plain Jinja env and assert
load-bearing HOOKS only. JS RUNTIME behavior (math, localStorage toggle, inline validation)
is Wave 5 Playwright — NOT asserted here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES_DIR = ROOT / "src" / "site" / "templates"
STATIC_JS = ROOT / "src" / "site" / "static" / "js"


@dataclass(frozen=True)
class FakeView:
    """Mirror of SiteListingView (DATA_CONTRACT §1) — only the fields templates may read."""

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
    return Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
    )


def _render(name: str, **ctx: object) -> str:
    return _env().get_template(name).render(**ctx)


_STATS = {"active": 47, "districts": 8}
_CSRF = "test-csrf-token"


# =====================================================================================
# W4-F2 · favorites (localStorage, no accounts)
# =====================================================================================
def test_card_has_favorite_control() -> None:
    html = _render("partials/_card.html", item=FakeView())
    assert 'data-fav-id="128"' in html
    # tasteful glyph/SVG, NOT an emoji in chrome
    assert "<svg" in html
    # control is a real button (keyboard-operable), not a bare span
    assert "data-fav" in html


def test_apt_has_favorite_control() -> None:
    html = _render("apt.html", item=FakeView(), similar=[], csrf_token=_CSRF)
    assert 'data-fav-id="128"' in html
    assert "<svg" in html


def test_card_favorite_control_no_emoji() -> None:
    """Anti-slop: no emoji-in-chrome heart; use a glyph/SVG instead."""
    html = _render("partials/_card.html", item=FakeView())
    for emoji in ("❤", "♥", "💚", "🤍", "⭐", "🌟"):
        assert emoji not in html


def test_base_has_favorites_counter_and_script() -> None:
    html = _render("base.html", csrf_token=_CSRF)
    # counter badge lives in the nav CTA, reflects localStorage count
    assert "data-fav-count" in html
    assert "/static/js/favorites.js" in html


def test_favorites_script_uses_localstorage_no_accounts() -> None:
    src = (STATIC_JS / "favorites.js").read_text(encoding="utf-8")
    assert "localStorage" in src
    assert "data-fav" in src  # binds to the card control
    assert "fetch(" not in src  # no backend; localStorage only


# =====================================================================================
# W4-F3 · lead form UX
# =====================================================================================
def test_lead_form_honeypot_is_company() -> None:
    html = _render("partials/_lead_form.html", csrf_token=_CSRF)
    assert 'name="company"' in html  # honeypot, EXACT field name per §6
    assert 'name="csrf_token"' in html  # token field (not _csrf)
    assert _CSRF in html


def test_lead_form_required_fields_and_htmx() -> None:
    html = _render("partials/_lead_form.html", csrf_token=_CSRF)
    assert 'name="name"' in html
    assert 'name="contact"' in html
    # no-JS fallback: plain POST still works
    assert 'method="post"' in html
    assert 'action="/lead"' in html
    # JS-enhanced path: htmx hx-post → swaps in the ok partial
    assert 'hx-post="/lead"' in html
    # lead.js wires inline validation
    assert "/static/js/lead.js" in html


def test_lead_form_has_inline_error_targets() -> None:
    """Inline errors (never alert): each required field has an error slot lead.js fills."""
    html = _render("partials/_lead_form.html", csrf_token=_CSRF)
    assert "data-error-for" in html or "field-error" in html


def test_base_sets_csrf_hx_header() -> None:
    """Mirror admin: page-wide hx-headers carries X-CSRF-Token so hx-post passes site CSRF."""
    html = _render("base.html", csrf_token=_CSRF)
    assert "hx-headers" in html
    assert "X-CSRF-Token" in html
    assert _CSRF in html


def test_lead_script_no_alert_inline_validation() -> None:
    src = (STATIC_JS / "lead.js").read_text(encoding="utf-8")
    assert "alert(" not in src  # inline errors only
    assert "company" in src  # honeypot awareness / required-field checks
    assert "contact" in src


# =====================================================================================
# whole-page render smoke (no exceptions) — extras context only
# =====================================================================================
@pytest.mark.parametrize(
    ("template", "ctx"),
    [
        ("index.html", {"featured": [FakeView()], "stats": _STATS, "csrf_token": _CSRF}),
        ("apt.html", {"item": FakeView(), "similar": [FakeView(id=200)], "csrf_token": _CSRF}),
    ],
)
def test_pages_render_without_error(template: str, ctx: dict[str, object]) -> None:
    html = _render(template, **ctx)
    assert html.strip()
