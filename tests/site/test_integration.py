"""End-to-end INTEGRATION gate for the public catalog (Stage W, Wave 3).

Unlike test_routes.py (DictLoader stubs) and test_render.py (templates rendered in isolation),
this wires the REAL app (src.site.app:app) to the REAL templates and the REAL read-model and
proves the whole stack renders together against a seeded temp-file DB.

Seam wiring (mirrors test_routes.py):
- the seeded file DB is reused from tests/site/conftest.py's ``site_db`` fixture;
- the app reads it via app.dependency_overrides[catalog_db_url];
- the lead notifier (src.site.leads.send_text_to_recipients) is patched so POST /lead sends
  nothing real — the lead still commits to the temp DB and a row is asserted there.

What the seed gives us (see conftest._seed), via the real read-model:
- site_listing 1: Врачар, 2+1, 600€, coords, PUBLIC, for_vnj=True   (featured + map)
- site_listing 2: Нови Београд, 1+1, 800€, NO coords, PUBLIC, white_card
- site_listing 3: Врачар, студия, 500€, coords, PUBLIC, RENTED (is_active False), note
- site_listing 4: Земун, 3+1, 1200€, coords, PRIVATE (is_public False) -> excluded / 404
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import src.site.leads as leads
from src.db.models import Lead
from src.site import cache
from src.site.app import create_app
from src.site.deps import (
    CSRF_FORM_FIELD,
    SITE_CSRF_TOKEN,
    catalog_db_url,
    lead_rate_limiter,
)

# The site's OWN Telegram links are allowed everywhere: the channel and (Stage W2 F5) the
# permanent agent-DM CTA. Everything else with a scheme (source listing URLs http://x/1,
# remote photo CDNs http://cdn/..., a площадка t.me deep-link to a listing) must NEVER reach
# a public template.
ALLOWED_CHANNEL = "https://t.me/RentBelgrad"
ALLOWED_AGENT_DM = "https://t.me/Darya_kralj"  # F5 «Написать в Telegram» CTA (header/footer/sticky)


@pytest.fixture(autouse=True)
def _clear_state():
    """Reset the process-wide caches + rate-limiter (module singletons) around each test."""
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
def notifier_spy(monkeypatch):
    """Patch the lead notifier so POST /lead sends nothing real. Records calls."""
    calls: list[str] = []

    async def _fake_send(_recipients, text):  # noqa: ANN001
        calls.append(text)

    monkeypatch.setattr(leads, "send_text_to_recipients", _fake_send)
    return calls


@pytest.fixture
def client(site_db, notifier_spy):
    """Real app, real templates, real read-model — DB seam pointed at the seeded temp file."""
    app = create_app()
    app.dependency_overrides[catalog_db_url] = lambda: site_db
    with TestClient(app) as c:
        c.notifier_spy = notifier_spy  # type: ignore[attr-defined]
        c.site_db = site_db  # type: ignore[attr-defined]
        yield c


# --------------------------------------------------------------------------- helpers


def _no_listing_url_leaks(html: str) -> None:
    """Assert the rendered HTML leaks no source listing / remote-photo / площадка URL.

    The site's own Telegram links (channel + agent DM) are allowed; we strip every occurrence
    of them first, then assert nothing else carrying a URL scheme or a площадка marker survives.
    """
    stripped = html.replace(ALLOWED_CHANNEL, "").replace(ALLOWED_AGENT_DM, "")
    # Stage S Wave 2: the /apt/{id} fuzzy map loads Leaflet from the plan-sanctioned jsdelivr CDN
    # (same provider as /map, CSP-allowed). Strip that allowed origin first — like the Telegram
    # URLs above — so the broad "://cdn" photo-CDN guard below still catches REAL leaks.
    stripped = stripped.replace("cdn.jsdelivr.net", "")
    # Seeded source listing urls (http://x/N) and remote photo CDNs (http://cdn/...).
    assert "http://x/" not in stripped, "source listing URL (listings.url) leaked"
    assert "http://cdn/" not in stripped, "remote photo CDN URL leaked"
    assert "://cdn" not in stripped, "remote photo CDN URL leaked"
    # No площадка name or a t.me deep-link to anything other than the channel.
    lowered = stripped.lower()
    assert "площадк" not in lowered, "площадка marker leaked into public HTML"
    for platform in ("nekretnine", "halooglasi", "4zida", "cetiri", "zida"):
        assert platform not in lowered, f"platform marker {platform!r} leaked"
    # Any residual t.me/... link after stripping the channel would be a listing deep-link leak.
    assert "t.me/" not in stripped, "a non-channel t.me link leaked into public HTML"


async def _count_leads(database_url: str) -> int:
    engine = create_async_engine(database_url, future=True)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as s:
            rows = (await s.execute(select(Lead))).scalars().all()
            return len(rows)
    finally:
        await engine.dispose()


async def _fetch_leads(database_url: str) -> list[Lead]:
    engine = create_async_engine(database_url, future=True)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as s:
            return list((await s.execute(select(Lead))).scalars().all())
    finally:
        await engine.dispose()


def _csrf_from_form(html: str) -> str:
    """Harvest the site CSRF token from a rendered lead form's hidden field."""
    m = re.search(r'name="csrf_token"\s+value="([^"]*)"', html)
    assert m, "no csrf_token hidden field found in the rendered form"
    return m.group(1)


# =========================================================== GET / (homepage, real render)


def test_index_renders_featured_cards_with_price_euro_mono(client):
    r = client.get("/")
    assert r.status_code == 200
    html = r.text
    # Real shell rendered (base.html extended).
    assert "<!DOCTYPE html>" in html
    assert "RentBelgrad" in html
    # Featured cards: price with € in MONO (card .price uses <span class="mono">€...</span>).
    assert "€600" in html
    assert re.search(r'class="mono">€600<', html), "featured price not in MONO span"
    # Stats counter (active = 3 public listings) rendered in a mono node.
    assert re.search(r'class="n mono">3<', html), "active-listings stat not rendered"
    # The private (Земун, 1200€) listing must never surface as a card. (Note: «Земун» also
    # appears in the decorative marquee strip, so we key on its distinguishing price.)
    assert "€1200" not in html
    _no_listing_url_leaks(html)


# =================================================== GET /catalog (full page, real render)


def test_catalog_lists_public_excludes_private(client):
    # show_rented=1 so the rented public listing (id3 €500) is part of this private-exclusion
    # check (C1 hides rented by default; that behavior is covered by the readmodel tests).
    r = client.get("/catalog", params={"show_rented": "1"})
    assert r.status_code == 200
    html = r.text
    assert "<!DOCTYPE html>" in html  # full shell, not the partial
    # public districts present...
    assert "Нови Београд" in html  # public listing 2
    assert "€600" in html and "€800" in html and "€500" in html
    # ...private one absent. (Note: "Земун" appears as a selectable district OPTION in the C2
    # subscribe form's curated list, so the listing-leak check keys on the private listing's
    # data — its price — not the bare district string.)
    assert "€1200" not in html
    # total count badge: 3 public.
    assert re.search(r'class="mono">3</span>\s*квартир', html)
    _no_listing_url_leaks(html)


def test_catalog_vnj_narrows_to_vnj_only(client):
    r = client.get("/catalog", params={"vnj": "1"})
    assert r.status_code == 200
    html = r.text
    # Only listing 1 (Врачар, 600€) is for_vnj in the seed.
    assert "€600" in html
    assert "€800" not in html  # listing 2 is white_card, not vnj
    assert "€500" not in html  # listing 3 is neither
    assert re.search(r'class="mono">1</span>\s*квартира', html)  # singular count
    _no_listing_url_leaks(html)


def test_catalog_empty_filter_params_are_ignored_not_zero(client):
    # W5-1 bug: the hero/filter forms submit empty district=/rooms=/price_max= for unfilled
    # fields. Those must mean "no filter", not "match nothing" — else a district-only search
    # returned 0. Врачар has two public listings (id1 €600, id3 €500 rented-but-shown).
    r = client.get(
        "/catalog",
        params=[("district", "Врачар"), ("rooms", ""), ("price_max", ""), ("show_rented", "1")],
    )
    assert r.status_code == 200
    html = r.text
    assert "€600" in html and "€500" in html  # both Врачар public listings, not 0
    assert "€800" not in html  # Нови Београд excluded by the district filter
    assert re.search(r'class="mono">2</span>\s*квартир', html)  # 2, not 0
    _no_listing_url_leaks(html)


def test_catalog_hx_request_returns_results_partial_only(client):
    r = client.get("/catalog", headers={"HX-Request": "true"})
    assert r.status_code == 200
    html = r.text
    # The HTMX swap target is the results grid only — NO full document / chrome.
    assert "<!DOCTYPE html>" not in html
    assert "<html" not in html
    assert "<header" not in html
    assert 'id="results"' in html  # the grid wrapper from partials/_results.html
    assert "€600" in html  # real cards still rendered inside the partial
    _no_listing_url_leaks(html)


# ================================================ GET /apt/{id} (detail, real render)


def test_apt_public_shows_price_channel_link(client):
    # site_listing 1: public, NOT rented.
    r = client.get("/apt/1")
    assert r.status_code == 200
    html = r.text
    assert "<!DOCTYPE html>" in html
    # Price in MONO on the sticky CTA side.
    assert re.search(r'class="mono">€600<', html)
    # Channel link present (Telegram CTA) and pointing at the allowed channel URL.
    assert ALLOWED_CHANNEL in html
    assert "Открыть в Telegram" in html
    # Not rented -> no «снято» badge in THIS listing's own badge block. (A rented sibling can
    # still show «снято» inside the «похожие» similar cards, so we scope to apt-badges.)
    badges = re.search(r'<div class="apt-badges">(.*?)</div>', html, re.S)
    assert badges, "apt-badges block not rendered"
    assert "снято" not in badges.group(1)
    assert "подходит для ВНЖ" in badges.group(1)  # listing 1 is for_vnj
    _no_listing_url_leaks(html)


def test_apt_rented_shows_rented_badge(client):
    # site_listing 3: public + rented (is_active False).
    r = client.get("/apt/3")
    assert r.status_code == 200
    html = r.text
    assert re.search(r'class="mono">€500<', html)
    assert "снято" in html  # rented badge shown, not hidden
    assert "Дарья: торг" in html  # the curated note is rendered
    _no_listing_url_leaks(html)


def test_apt_private_is_404(client):
    # site_listing 4 is is_public=False -> never servable.
    r = client.get("/apt/4")
    assert r.status_code == 404


def test_apt_missing_is_404(client):
    r = client.get("/apt/9999")
    assert r.status_code == 404


# ============================================================ GET /api/map.json


def test_map_json_only_public_with_coords(client):
    r = client.get("/api/map.json")
    assert r.status_code == 200
    body = r.json()
    assert "points" in body
    # Only available public listing 1 has coords; 2 has no coords; 3 is rented; 4 private.
    ids = sorted(p["id"] for p in body["points"])
    assert ids == [1, 3]  # public authorization retains the annotated dead card.
    for p in body["points"]:
        assert p["url"] == f"/apt/{p['id']}"
        assert p["lat"] is not None and p["lon"] is not None
    # No remote / source URL leaks in the JSON payload either.
    _no_listing_url_leaks(r.text)


# ============================================================ GET /calculator + /healthz


def test_calculator_redirects_to_lead(client):
    r = client.get("/calculator", follow_redirects=False)
    assert r.status_code == 301
    assert r.headers["location"] == "/#lead"


def test_healthz_ok(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


# ============================================================ POST /lead (end-to-end)


def test_lead_without_csrf_is_403(client):
    r = client.post("/lead", data={"name": "Анна", "contact": "@anna"})
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_lead_valid_writes_row_and_notifier_patched(client):
    # Harvest a real CSRF token from a freshly rendered lead form on the homepage.
    page = client.get("/")
    token = _csrf_from_form(page.text)
    assert token == SITE_CSRF_TOKEN  # rendered token must match the live server token

    before = await _count_leads(client.site_db)  # type: ignore[attr-defined]
    r = client.post(
        "/lead",
        data={
            CSRF_FORM_FIELD: token,
            "name": "Анна",
            "contact": "@anna",
            "budget_eur": "700",
            "districts": "Врачар",
            "company": "",  # honeypot empty -> a real submission
        },
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    assert "заявка отправлена" in r.text  # _lead_ok.html success state rendered

    leads_after = await _fetch_leads(client.site_db)  # type: ignore[attr-defined]
    assert len(leads_after) == before + 1
    saved = leads_after[-1]
    assert saved.name == "Анна"
    assert saved.contact == "@anna"
    assert saved.budget_eur == 700
    assert saved.source == "site"

    # The notifier was patched: it recorded one call and sent nothing real.
    assert len(client.notifier_spy) == 1  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_lead_honeypot_filled_writes_nothing(client):
    page = client.get("/")
    token = _csrf_from_form(page.text)

    before = await _count_leads(client.site_db)  # type: ignore[attr-defined]
    r = client.post(
        "/lead",
        data={
            CSRF_FORM_FIELD: token,
            "name": "Бот",
            "contact": "@bot",
            "company": "Acme Spam Co",  # honeypot filled -> silently dropped
        },
        headers={"HX-Request": "true"},
    )
    # Silent to the bot: still 200, success-looking, but NO DB row + NO notifier call.
    assert r.status_code == 200
    after = await _count_leads(client.site_db)  # type: ignore[attr-defined]
    assert after == before
    assert len(client.notifier_spy) == 0  # type: ignore[attr-defined]


# ============================================================ S3-3: rate-limit keys on real IP


def test_lead_ratelimit_separate_cf_ips_are_separate_buckets(site_db, notifier_spy, monkeypatch):
    """Behind CF (trust_cloudflare=on), two distinct CF-Connecting-IP values from the SAME proxy
    peer must NOT share a rate-limit bucket — otherwise every visitor collapses into one CF-edge
    IP and the per-IP limit stops discriminating (the "схлопываются за CF" risk row)."""
    from src.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "trust_cloudflare", True)
    monkeypatch.setattr(s, "site_lead_ratelimit_max", 2)
    monkeypatch.setattr(s, "site_lead_ratelimit_window", 3600)

    app = create_app()
    app.dependency_overrides[catalog_db_url] = lambda: site_db
    # One proxy peer (simulates the CF edge / Caddy) for BOTH visitors — identity is carried
    # only by CF-Connecting-IP.
    with TestClient(app, client=("127.0.0.1", 1)) as c:
        token = SITE_CSRF_TOKEN

        def _post(cf_ip: str):
            return c.post(
                "/lead",
                data={CSRF_FORM_FIELD: token, "name": "X", "contact": "@x", "company": ""},
                headers={"HX-Request": "true", "CF-Connecting-IP": cf_ip},
            )

        # Saturate visitor A (limit = 2).
        assert _post("11.11.11.11").status_code == 200
        assert _post("11.11.11.11").status_code == 200
        assert _post("11.11.11.11").status_code == 429
        # Visitor B is a DIFFERENT bucket — still allowed despite the shared proxy peer.
        assert _post("22.22.22.22").status_code == 200
