"""Stage S Wave 5 (S5-2) — dynamic OpenGraph share-image for /apt/{id}.

render_og_png() composes a 1200x630 brand PNG (first local photo → paper panel fallback) with
price + район + ВНЖ-badge + wordmark. Privacy (D1): район ONLY — never street/address.

The route GET /apt/{id}/og.png serves it with an ETag (+ If-None-Match → 304) and a CF-friendly
Cache-Control; a private/missing id → 404 (same get_public gate as the apt page).

Font robustness: render_og_png uses a size-capable default font (Pillow ≥10 load_default(size=N)),
so it renders on both dev macOS and prod Linux without a bundled woff2/ttf.
"""

from __future__ import annotations

import inspect
from io import BytesIO

from fastapi.testclient import TestClient
from PIL import Image

from src.site import og_image
from src.site.app import create_app
from src.site.deps import catalog_db_url
from src.site.readmodel import SiteListingView

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _view(**overrides) -> SiteListingView:
    base = dict(
        id=1,
        price_eur=650,
        area_m2=45.0,
        rooms="2+1",
        floor=None,
        district="Врачар",
        address=None,
        lat=44.8,
        lon=20.47,
        for_vnj=True,
        for_white_card=False,
        is_rented=False,
        photos=[],
        note=None,
        channel_url="https://t.me/OtzRentBelgrad/1",
        posted_at="2026-06-10T12:00:00+00:00",
    )
    base.update(overrides)
    return SiteListingView(**base)


# ============================================================== unit: render_og_png


def test_render_returns_png_magic_and_1200x630_no_photo():
    """No-photo item → renders a paper-panel PNG of the OG canonical size, no crash."""
    png = og_image.render_og_png(_view(photos=[]))
    assert isinstance(png, (bytes, bytearray))
    assert png[:8] == PNG_MAGIC
    img = Image.open(BytesIO(png))
    assert img.size == (1200, 630)


def test_render_with_photo_path_that_does_not_exist_falls_back():
    """A /photos URL whose file is missing/unreadable must NOT crash — paper panel fallback."""
    png = og_image.render_og_png(_view(photos=["/photos/1/1/00.jpg"]))
    assert png[:8] == PNG_MAGIC
    img = Image.open(BytesIO(png))
    assert img.size == (1200, 630)


def test_render_with_real_photo_composites(tmp_path, monkeypatch):
    """With a readable local photo, the function composes it (still 1200x630 PNG)."""
    from src.config import get_settings

    # Lay down a real JPEG under a fake photos root and point settings at it.
    root = tmp_path / "photos"
    (root / "1" / "1").mkdir(parents=True)
    Image.new("RGB", (800, 600), (40, 90, 140)).save(root / "1" / "1" / "00.jpg")

    settings = get_settings()
    monkeypatch.setattr(settings, "site_photos_root", str(root))

    png = og_image.render_og_png(_view(photos=["/photos/1/1/00.jpg"]))
    assert png[:8] == PNG_MAGIC
    assert Image.open(BytesIO(png)).size == (1200, 630)


def test_render_vnj_false_still_renders():
    png = og_image.render_og_png(_view(for_vnj=False))
    assert Image.open(BytesIO(png)).size == (1200, 630)


# ============================================================== privacy (D1): no street/address


def test_render_signature_does_not_expose_address_to_draw():
    """Privacy guard (D1): the composition source must not reference address/street at all.

    We can't read pixels cheaply, so we assert a code-level guarantee: og_image.py never touches
    `.address` / `.street` (район only). A regression that drew the address would have to add one
    of these tokens, tripping this test.
    """
    src = inspect.getsource(og_image)
    assert ".address" not in src
    assert ".street" not in src
    assert "street" not in src.lower().replace("# ", "")  # belt-and-suspenders


def test_render_does_not_crash_on_address_set():
    """Even if an item carries an address, render must succeed (and, per the guard above, never
    draw it)."""
    png = og_image.render_og_png(_view(address="Njegoševa 5"))
    assert Image.open(BytesIO(png)).size == (1200, 630)


# ============================================================== route: GET /apt/{id}/og.png


def _client(site_db) -> TestClient:
    app = create_app()
    app.dependency_overrides[catalog_db_url] = lambda: site_db
    return TestClient(app)


def test_og_png_route_200_image_png_with_etag(site_db):
    with _client(site_db) as c:
        r = c.get("/apt/1/og.png")
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/png"
        assert r.content[:8] == PNG_MAGIC
        assert r.headers.get("etag")
        assert "max-age" in r.headers.get("cache-control", "")


def test_og_png_route_if_none_match_returns_304(site_db):
    with _client(site_db) as c:
        first = c.get("/apt/1/og.png")
        etag = first.headers["etag"]
        second = c.get("/apt/1/og.png", headers={"If-None-Match": etag})
        assert second.status_code == 304
        assert second.content == b""


def test_og_png_route_private_listing_is_404(site_db):
    # site_listing 4 is is_public=False in the seed.
    with _client(site_db) as c:
        assert c.get("/apt/4/og.png").status_code == 404


def test_og_png_route_missing_listing_is_404(site_db):
    with _client(site_db) as c:
        assert c.get("/apt/9999/og.png").status_code == 404


# ============================================================== apt.html: og:image → og.png route


def test_apt_og_image_points_at_og_png_route():
    """The og:image meta must point at the absolute /apt/{id}/og.png route (W2 made it the first
    photo; S5-2 swaps it for the dynamic image)."""
    from pathlib import Path

    from jinja2 import Environment, FileSystemLoader, select_autoescape

    tpl_dir = Path(__file__).resolve().parents[2] / "src" / "site" / "templates"
    env = Environment(
        loader=FileSystemLoader(str(tpl_dir)),
        autoescape=select_autoescape(["html"]),
    )
    env.globals["csrf_token"] = "tkn"
    env.globals["site_base_url"] = "https://rent.example.com"
    env.globals["telegram_url"] = "https://t.me/Darya_kralj"
    env.filters.setdefault("urlencode", lambda s: str(s))

    item = _view()
    html = env.get_template("apt.html").render(item=item, similar=[], coords=None, maps=None)
    assert (
        'property="og:image" content="https://rent.example.com/apt/1/og.png"' in html
    )
    assert 'property="og:image:width" content="1200"' in html
    assert 'property="og:image:height" content="630"' in html
    # the og:image must no longer be a raw /photos url
    assert 'property="og:image" content="https://rent.example.com/photos' not in html
