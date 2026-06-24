"""S5-1 (PWA) — manifest, service worker, offline shell, base.html integration.

These exercise the REAL templates + static files (no DictLoader stub): the SW route bakes the
live asset version into the cache name, the manifest is valid + standalone with 192/512 icons,
and the offline shell renders branded with a Telegram lifeline. base.html carries the manifest
link, iOS meta, the SW-register script, and the mobile bottom-nav markup.

A TestClient over a fresh create_app() is enough — none of these routes touch the DB.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import src.site.routes as routes
from src.site.app import create_app

STATIC = Path(__file__).resolve().parents[2] / "src" / "site" / "static"


@pytest.fixture
def client():
    # No dependency_overrides: /sw.js, /static/*, /offline don't read the catalog DB.
    with TestClient(create_app()) as c:
        yield c


# --------------------------------------------------------------------------- service worker


def test_sw_js_served_at_root_scope(client):
    """GET /sw.js → 200 application/javascript, root-scope header, no-cache."""
    r = client.get("/sw.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]
    # Root scope is the whole point — a /static-scoped worker can't control navigations.
    assert r.headers.get("service-worker-allowed") == "/"
    assert "no-cache" in r.headers.get("cache-control", "")


def test_sw_cache_name_tied_to_asset_version(client):
    """The SW cache name must carry the live asset_v so a deploy invalidates it."""
    version = str(routes.templates.env.globals["asset_v"])
    body = client.get("/sw.js").text
    # The placeholder must be substituted, and the real token must appear in CACHE_VERSION,
    # which composes CACHE_NAME ("rentbelgrad-" + version) → a deploy busts the SW cache.
    assert "__CACHE_VERSION__" not in body
    assert version in body
    assert re.search(r'CACHE_VERSION\s*=\s*"' + re.escape(version) + r'"', body)


def test_sw_strategy_markers_present(client):
    """Sanity: the worker declares the install/activate/fetch handlers + offline fallback."""
    body = client.get("/sw.js").text
    assert "addEventListener(\"install\"" in body
    assert "addEventListener(\"activate\"" in body
    assert "addEventListener(\"fetch\"" in body
    assert "/offline" in body  # network-first HTML falls back to the precached shell


# --------------------------------------------------------------------------- manifest


def test_manifest_served(client):
    """GET /static/manifest.webmanifest → 200 with standalone display + icons."""
    r = client.get("/static/manifest.webmanifest")
    assert r.status_code == 200
    body = r.text
    assert '"display": "standalone"' in body or '"display":"standalone"' in body
    assert "icons" in body


def test_manifest_is_valid_and_complete():
    """Parse the manifest JSON; assert required keys + 192/512 icons + a maskable purpose."""
    data = json.loads((STATIC / "manifest.webmanifest").read_text(encoding="utf-8"))
    for key in ("name", "short_name", "start_url", "display", "theme_color",
                "background_color", "icons", "lang"):
        assert key in data, f"manifest missing required key: {key}"
    assert data["display"] == "standalone"
    assert data["start_url"] == "/"
    assert data["lang"] == "ru"
    # theme/background from brand tokens (paper).
    assert data["theme_color"].upper() == "#F4EFE6"

    sizes = {ic["sizes"] for ic in data["icons"]}
    assert "192x192" in sizes and "512x512" in sizes, f"need 192+512 icons, got {sizes}"
    purposes = {ic.get("purpose", "any") for ic in data["icons"]}
    assert any("maskable" in p for p in purposes), "manifest needs a maskable icon"
    # every referenced icon file must exist on disk.
    for ic in data["icons"]:
        rel = ic["src"].removeprefix("/static/")
        assert (STATIC / rel).is_file(), f"icon file missing: {ic['src']}"


def test_icon_files_exist():
    for name in ("icon-192.png", "icon-512.png", "icon-maskable.png", "apple-touch-icon.png"):
        assert (STATIC / "icons" / name).is_file(), f"missing icon: {name}"


# --------------------------------------------------------------------------- offline shell


def test_offline_route_renders_shell(client):
    """GET /offline → 200 renders the branded «Нет соединения» shell with a Telegram link."""
    r = client.get("/offline")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    body = r.text
    assert "Нет соединения" in body
    assert "RentBelgrad" in body
    assert "t.me" in body  # the Telegram lifeline


# --------------------------------------------------------------------------- base.html wiring


def test_base_html_has_pwa_head_and_register(client):
    """A real page carries the manifest link, iOS meta, SW-register script + bottom-nav."""
    body = client.get("/calculator").text  # a static page that extends base.html
    assert 'rel="manifest"' in body
    assert "/static/manifest.webmanifest" in body
    assert 'name="apple-mobile-web-app-capable"' in body
    assert 'rel="apple-touch-icon"' in body and "apple-touch-icon.png" in body
    # SW registration script is included.
    assert "/static/js/pwa.js" in body
    # bottom-nav present with the four destinations.
    assert 'class="bottom-nav"' in body
    assert 'href="/catalog"' in body
    assert 'href="/map"' in body
    assert 'href="/calculator"' in body
