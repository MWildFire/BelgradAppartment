"""C3 — HTTP caching for the genuinely-static content pages + /static assets.

Static content pages (/vnj, /how) re-render identically on every request, so they
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
    "apt.html": "TPL:apt id={{ item.id }} similar={{ similar|length }}",
    "vnj.html": "TPL:vnj договор",
    "how.html": "TPL:how 50%",
    "partials/_results.html": "TPL:_results items={{ items|length }}",
    "partials/_card.html": "TPL:_card id={{ item.id }}",
    "partials/_filters.html": "TPL:_filters",
    "404.html": "TPL:404",
}

# The static content pages C3 targets — each must carry an ETag + honor If-None-Match → 304.
STATIC_PAGES = ("/vnj", "/how")


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


# ================================================= TTLCache: bounded size (memory-DoS guard)
# Ключи каталожного кэша строятся из СЫРЫХ query-параметров, т.е. контролируются атакующим:
# без maxsize уникальные query-строки растят _store бесконечно (истёкшие записи чистились
# только expire-on-read того же ключа). Инвариант: len(_store) ≤ maxsize ВСЕГДА.


def test_ttlcache_default_maxsize_bounds_1000_unique_keys():
    clock = {"t": 0.0}
    c = cache.TTLCache(ttl=300, clock=lambda: clock["t"])
    for i in range(1000):
        c.set(("attack", i), i)
    assert cache.CACHE_MAXSIZE == 512
    assert len(c._store) <= cache.CACHE_MAXSIZE
    # свежайший ключ — всегда живой (эвикция выкидывает старейшие, не новые)
    assert c.get(("attack", 999)) == 999


def test_ttlcache_set_purges_expired_before_evicting_fresh():
    clock = {"t": 0.0}
    c = cache.TTLCache(ttl=100, clock=lambda: clock["t"], maxsize=4)
    for k in ("stale1", "stale2", "stale3"):
        c.set(k, "old")
    clock["t"] = 150.0  # все три истекли (expiry@100), но лежат в _store
    c.set("hot", "hv")  # 4-я запись — переполнения ещё нет
    clock["t"] = 160.0
    c.set("new", "nv")  # переполнение: сначала выкидываются ИСТЁКШИЕ, живой hot выживает
    assert c.get("hot") == "hv"
    assert c.get("new") == "nv"
    assert c.get("stale1") is cache.MISS
    assert len(c._store) == 2


def test_ttlcache_evicts_oldest_when_none_expired():
    clock = {"t": 0.0}
    c = cache.TTLCache(ttl=100, clock=lambda: clock["t"], maxsize=3)
    for i, k in enumerate(("a", "b", "c")):
        clock["t"] = float(i)
        c.set(k, k.upper())
    clock["t"] = 3.0
    c.set("d", "D")  # ничего не истекло → уходит старейший (a)
    assert c.get("a") is cache.MISS
    assert c.get("b") == "B"
    assert c.get("c") == "C"
    assert c.get("d") == "D"
    assert len(c._store) == 3


def test_ttlcache_reset_key_refreshes_its_eviction_order():
    clock = {"t": 0.0}
    c = cache.TTLCache(ttl=100, clock=lambda: clock["t"], maxsize=2)
    c.set("a", 1)
    c.set("b", 2)
    c.set("a", 3)  # обновление существующего ключа НЕ эвиктит и делает его «свежим»
    assert len(c._store) == 2
    c.set("cc", 4)  # теперь старейший — b
    assert c.get("b") is cache.MISS
    assert c.get("a") == 3
    assert c.get("cc") == 4


# ============================================== / и /apt/{id} идут через TTL-кэш (как /catalog)
# Раньше каждый GET / делал 3 полных _load_rows-гидрации (list_public + featured_one + facets),
# а GET /apt/{id} — 2 (get_public + similar). Повторный запрос обязан отдаваться из кэша.


def _counting_load_rows(monkeypatch):
    from src.site import readmodel

    real = readmodel._load_rows
    calls = {"n": 0}

    async def counting(database_url):
        calls["n"] += 1
        return await real(database_url)

    monkeypatch.setattr(readmodel, "_load_rows", counting)
    return calls


def test_index_second_get_serves_from_cache(client, monkeypatch):
    calls = _counting_load_rows(monkeypatch)
    assert client.get("/").status_code == 200
    first = calls["n"]
    assert first >= 1  # первый запрос честно ходит в readmodel
    assert client.get("/").status_code == 200
    assert calls["n"] == first  # повторный GET / — ни одной новой гидрации


def test_apt_second_get_serves_from_cache(client, monkeypatch):
    calls = _counting_load_rows(monkeypatch)
    r1 = client.get("/apt/1")
    assert r1.status_code == 200
    first = calls["n"]
    assert first >= 1
    r2 = client.get("/apt/1")
    assert r2.status_code == 200
    assert r2.text == r1.text
    assert calls["n"] == first  # повторный GET /apt/1 — ни одной новой гидрации


def test_apt_different_ids_do_not_share_cache_entries(client, monkeypatch):
    calls = _counting_load_rows(monkeypatch)
    r1 = client.get("/apt/1")
    r2 = client.get("/apt/2")
    assert r1.status_code == r2.status_code == 200
    assert "id=1" in r1.text and "id=2" in r2.text
    assert calls["n"] >= 2  # разные id — разные ключи, второй id тоже ходит в readmodel
