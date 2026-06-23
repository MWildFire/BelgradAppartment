"""C3 — HTTP caching for the genuinely-static content pages + /static assets.

Static content pages (/vnj, /how, /calculator) re-render identically on every request, so they
carry an ETag (stable hash of the rendered body) + Last-Modified and short-circuit to 304 when
the client sends a matching If-None-Match. Dynamic routes (/, /catalog, /lead) must NOT carry a
static ETag (their bodies depend on DB state / filters). /static asset responses carry a
Cache-Control whose max-age lets the asset-version token (app.py) do the busting.

These run through the same DictLoader-stub seam as test_routes (no dependency on agent D's real
markup); the stubs are deterministic, so the ETag is stable across requests.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from jinja2 import DictLoader

import src.site.routes as routes
from src.site import cache
from src.site.app import create_app
from src.site.deps import catalog_db_url, lead_rate_limiter

_STUBS = {
    "base.html": "BASE",
    "index.html": "TPL:index featured={{ featured|length }} active={{ stats.active }}",
    "catalog.html": "TPL:catalog items={{ items|length }} total={{ total }}",
    "vnj.html": "TPL:vnj договор",
    "how.html": "TPL:how 50%",
    "calculator.html": "TPL:calculator",
    "partials/_results.html": "TPL:_results items={{ items|length }}",
    "partials/_card.html": "TPL:_card id={{ item.id }}",
    "partials/_filters.html": "TPL:_filters",
    "404.html": "TPL:404",
}

# The static content pages C3 targets — each must carry an ETag + honor If-None-Match → 304.
STATIC_PAGES = ("/vnj", "/how", "/calculator")


@pytest.fixture(autouse=True)
def _stub_templates(monkeypatch):
    monkeypatch.setattr(routes.templates.env, "loader", DictLoader(_STUBS))
    yield


@pytest.fixture(autouse=True)
def _clear_state():
    cache.catalog_cache.clear()
    cache.facets_cache.clear()
    cache.map_cache.clear()
    lead_rate_limiter.reset()
    yield
    cache.catalog_cache.clear()
    cache.facets_cache.clear()
    cache.map_cache.clear()
    lead_rate_limiter.reset()


@pytest.fixture
def client(site_db):
    app = create_app()
    app.dependency_overrides[catalog_db_url] = lambda: site_db
    with TestClient(app) as c:
        yield c


@pytest.mark.parametrize("path", STATIC_PAGES)
def test_static_page_first_get_returns_200_with_etag(client, path):
    r = client.get(path)
    assert r.status_code == 200
    assert r.headers.get("ETag")  # non-empty validator present
    assert r.text  # body served on the first (uncached) request


@pytest.mark.parametrize("path", STATIC_PAGES)
def test_static_page_if_none_match_returns_304_empty_body(client, path):
    first = client.get(path)
    etag = first.headers["ETag"]
    second = client.get(path, headers={"If-None-Match": etag})
    assert second.status_code == 304
    assert second.text == ""  # 304 carries no body
    # the validator is echoed back so the client can keep caching
    assert second.headers.get("ETag") == etag


@pytest.mark.parametrize("path", STATIC_PAGES)
def test_static_page_stale_if_none_match_returns_200(client, path):
    r = client.get(path, headers={"If-None-Match": '"deadbeef-stale"'})
    assert r.status_code == 200
    assert r.text


def test_etag_changes_when_rendered_content_changes(client, monkeypatch):
    before = client.get("/vnj").headers["ETag"]
    # change the rendered body of /vnj → its ETag must change (content-derived validator).
    new_stubs = {**_STUBS, "vnj.html": "TPL:vnj договор — ОБНОВЛЕНО"}
    monkeypatch.setattr(routes.templates.env, "loader", DictLoader(new_stubs))
    after = client.get("/vnj").headers["ETag"]
    assert before != after


def test_dynamic_routes_have_no_static_etag(client):
    # A misleading static ETag on a DB/filter-dependent route would serve stale 304s.
    for path in ("/", "/catalog"):
        r = client.get(path)
        assert r.status_code == 200
        assert "ETag" not in r.headers


def test_static_assets_have_cache_control(client):
    # /static responses carry a Cache-Control with a max-age (asset-version busts on change).
    r = client.get("/static/img/placeholder.svg")
    assert r.status_code == 200
    cc = r.headers.get("Cache-Control", "")
    assert "max-age=" in cc
