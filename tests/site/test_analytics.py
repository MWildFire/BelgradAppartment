"""Stage T / T2-2 — event logger: analytics.record_event + pageview hooks (DATA_CONTRACT-T §A.1/§A.3).

Что закрепляем контрактно:
- visitor = sha256(daily_salt‖ip‖ua)[:16], daily_salt = sha256(SITE_ANALYTICS_SALT‖YYYY-MM-DD):
  стабилен внутри суток, меняется на следующий день, сырые IP/UA не попадают в строку никогда;
- аллоулист путей нормализуется ('/apt/{id}' → path='/apt' + apt_id); HX-Request и не-2xx — скип;
- бот-эвристика по UA-подстрокам → is_bot=1 (строка пишется, отсев — на агрегатах);
- kill-switch SITE_ANALYTICS_ENABLED=false → ноль записей;
- КЛЮЧЕВОЕ: любой сбой писателя аналитики глотается с warn-логом — страница НИКОГДА не 5xx-ит.

Как в test_routes.py: стаб-шаблоны через DictLoader — тесты не зависят от реальной разметки.
"""

from __future__ import annotations

import hashlib
import logging
import re

import pytest
from fastapi.testclient import TestClient
from jinja2 import DictLoader

import src.site.routes as routes
from src.config import get_settings
from src.site import analytics, cache
from src.site.app import create_app
from src.site.deps import catalog_db_url, lead_rate_limiter

_STUBS = {
    "index.html": "TPL:index",
    "catalog.html": "TPL:catalog",
    "apt.html": "TPL:apt id={{ item.id }}",
    "map.html": "TPL:map",
    "calculator.html": "TPL:calculator",
    "vnj.html": "TPL:vnj",
    "how.html": "TPL:how",
    "offline.html": "TPL:offline",
    "404.html": "TPL:404",
    "partials/_results.html": "TPL:_results",
}

# Обычный браузерный UA — ни одного бот-маркера (дефолтный "testclient" тоже чист, но явный надёжнее).
_HUMAN_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/125.0 Safari/537.36"}


@pytest.fixture(autouse=True)
def _stub_templates(monkeypatch):
    monkeypatch.setattr(routes.templates.env, "loader", DictLoader(_STUBS))
    yield


@pytest.fixture(autouse=True)
def _clear_state():
    """Module-singleton caches/limiter shared across tests — reset so each test's tmp DB is clean."""
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


def _fetch_events(client) -> list[dict]:
    """Read site_events rows written by the background task into the client's seeded tmp DB."""
    import asyncio

    from sqlalchemy import select

    from src.db.models import SiteEvent
    from src.site import db as sitedb

    url = client.app.dependency_overrides[catalog_db_url]()

    async def _go() -> list[dict]:
        async with sitedb.read_session(database_url=url) as s:
            rows = (await s.execute(select(SiteEvent).order_by(SiteEvent.id))).scalars().all()
            return [
                {
                    "type": r.type,
                    "path": r.path,
                    "apt_id": r.apt_id,
                    "visitor": r.visitor,
                    "referrer": r.referrer,
                    "utm_source": r.utm_source,
                    "utm_medium": r.utm_medium,
                    "utm_campaign": r.utm_campaign,
                    "is_bot": r.is_bot,
                    "ts": r.ts,
                }
                for r in rows
            ]

    return asyncio.run(_go())


# ============================================================== unit: visitor hash (§A.1)


def test_visitor_hash_contract_construction():
    # Точная формула §A.1: daily_salt = sha256(salt‖YYYY-MM-DD) hex; visitor = sha256(daily_salt‖ip‖ua)[:16].
    daily = hashlib.sha256(b"s3cret2026-07-02").hexdigest()
    expected = hashlib.sha256((daily + "1.2.3.4" + "UA/1.0").encode()).hexdigest()[:16]
    assert analytics.visitor_hash("1.2.3.4", "UA/1.0", secret="s3cret", day="2026-07-02") == expected


def test_visitor_hash_is_16_hex():
    assert re.fullmatch(r"[0-9a-f]{16}", analytics.visitor_hash("9.9.9.9", "X", secret="", day="2026-07-02"))


def test_visitor_hash_stable_same_day_changes_next_day():
    a = analytics.visitor_hash("1.2.3.4", "UA", secret="s", day="2026-07-02")
    b = analytics.visitor_hash("1.2.3.4", "UA", secret="s", day="2026-07-02")
    c = analytics.visitor_hash("1.2.3.4", "UA", secret="s", day="2026-07-03")
    assert a == b  # стабилен в сутках
    assert a != c  # уники не связываемы между днями


def test_visitor_hash_discriminates_ip_and_ua():
    base = analytics.visitor_hash("1.2.3.4", "UA", secret="s", day="2026-07-02")
    assert analytics.visitor_hash("1.2.3.5", "UA", secret="s", day="2026-07-02") != base
    assert analytics.visitor_hash("1.2.3.4", "UB", secret="s", day="2026-07-02") != base


# ============================================================== unit: bot heuristic (§A.3)


@pytest.mark.parametrize(
    "ua",
    [
        "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
        "Baiduspider+(+http://www.baidu.com/search/spider.htm)",
        "Mozilla/5.0 (compatible; YandexCrawler/1.0)",
        "Mozilla/5.0 (compatible; Yahoo! Slurp)",
        "Mozilla/5.0 HeadlessChrome/125.0",
        "python-requests/2.31.0",
        "curl/8.4.0",
        "Wget/1.21.4",
        "Site24x7-Monitor",
        "UptimeRobot/2.0",
        "Chrome-Lighthouse",
        "Pingdom.com_bot_version_1.4",
    ],
)
def test_bot_ua_detected(ua):
    assert analytics.is_bot_ua(ua) is True  # case-insensitive substring match


@pytest.mark.parametrize(
    "ua",
    [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/125.0 Safari/537.36",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Mobile/15E148 Safari/604.1",
        "",
    ],
)
def test_human_ua_not_flagged(ua):
    assert analytics.is_bot_ua(ua) is False


# ============================================================== routes: pageview hooks (allowlist)


def test_all_allowlist_paths_logged_and_normalized(client):
    for path in ("/", "/catalog", "/apt/1", "/map", "/calculator", "/vnj", "/how"):
        assert client.get(path, headers=_HUMAN_UA).status_code == 200
    events = _fetch_events(client)
    assert [(e["type"], e["path"], e["apt_id"]) for e in events] == [
        ("pageview", "/", None),
        ("pageview", "/catalog", None),
        ("pageview", "/apt", 1),  # §A.1: '/apt/{id}' хранится как path='/apt' + apt_id
        ("pageview", "/map", None),
        ("pageview", "/calculator", None),
        ("pageview", "/vnj", None),
        ("pageview", "/how", None),
    ]


def test_pageview_visitor_is_hash_not_raw_ip_ua(client):
    r = client.get("/", headers=_HUMAN_UA)
    assert r.status_code == 200
    events = _fetch_events(client)
    assert len(events) == 1
    e = events[0]
    assert re.fullmatch(r"[0-9a-f]{16}", e["visitor"])  # хэш, а не сырьё
    assert e["is_bot"] is False
    assert e["ts"] is not None
    # сырые IP/UA не пишутся никогда — ни в visitor, ни в любую другую колонку
    ua = _HUMAN_UA["User-Agent"]
    for value in e.values():
        text = str(value)
        assert "testclient" not in text  # TestClient's peer IP
        assert ua not in text


def test_visitor_stable_within_day_and_differs_by_ua(client):
    client.get("/", headers=_HUMAN_UA)
    client.get("/catalog", headers=_HUMAN_UA)
    client.get("/map", headers={"User-Agent": "Mozilla/5.0 (Macintosh) Firefox/126.0"})
    events = _fetch_events(client)
    assert events[0]["visitor"] == events[1]["visitor"]  # тот же ip+ua, те же сутки
    assert events[2]["visitor"] != events[0]["visitor"]  # другой UA → другой уник


def test_bot_pageview_marked(client):
    client.get("/", headers={"User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1)"})
    events = _fetch_events(client)
    assert len(events) == 1
    assert events[0]["is_bot"] is True  # пишем, отсев делают агрегаты


# ============================================================== skips: HX / не-2xx / вне аллоулиста


def test_catalog_hx_request_not_logged(client):
    r = client.get("/catalog", headers={"HX-Request": "true", **_HUMAN_UA})
    assert r.status_code == 200
    assert _fetch_events(client) == []  # пагинация/фильтры ≠ pageview


def test_apt_404_not_logged(client):
    assert client.get("/apt/9999", headers=_HUMAN_UA).status_code == 404  # нет такой
    assert client.get("/apt/4", headers=_HUMAN_UA).status_code == 404  # приватная
    assert _fetch_events(client) == []


def test_static_page_304_not_logged(client):
    first = client.get("/vnj", headers=_HUMAN_UA)
    assert first.status_code == 200
    revisit = client.get("/vnj", headers={"If-None-Match": first.headers["etag"], **_HUMAN_UA})
    assert revisit.status_code == 304
    assert len(_fetch_events(client)) == 1  # только полный 200-рендер — pageview


def test_non_allowlist_routes_not_logged(client):
    for path in ("/healthz", "/sw.js", "/offline", "/api/map.json", "/robots.txt", "/sitemap.xml"):
        assert client.get(path, headers=_HUMAN_UA).status_code == 200
    assert client.get("/apt/1/og.png", headers=_HUMAN_UA).status_code == 200
    assert _fetch_events(client) == []


# ============================================================== kill-switch (§A.3)


def test_kill_switch_off_writes_nothing(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "site_analytics_enabled", False)
    for path in ("/", "/catalog", "/apt/1", "/map", "/calculator", "/vnj", "/how"):
        assert client.get(path, headers=_HUMAN_UA).status_code == 200
    assert _fetch_events(client) == []  # все хуки no-op, ни одной записи


# ============================================================== КЛЮЧЕВОЙ: сбой писателя ≠ 5xx


def test_analytics_writer_failure_never_5xx(client, monkeypatch, caplog):
    def broken_writer(database_url=None):
        raise RuntimeError("simulated analytics writer failure")

    monkeypatch.setattr(analytics, "writer_session", broken_writer)
    with caplog.at_level(logging.WARNING, logger="src.site.analytics"):
        r = client.get("/", headers=_HUMAN_UA)
    assert r.status_code == 200  # страница живёт, TestClient не получил исключения
    assert "TPL:index" in r.text
    assert _fetch_events(client) == []
    assert any("analytics" in rec.getMessage() for rec in caplog.records)  # warn-лог оставлен


# ============================================================== атрибуция: utm + referrer (§A.1)


def test_utm_params_sanitized_and_stored(client):
    client.get(
        "/",
        params={
            "utm_source": "google",
            "utm_medium": "cpc<script>",  # запрещённые символы вырезаются, не 400
            "utm_campaign": "x" * 300,  # обрезка ≤100
        },
        headers=_HUMAN_UA,
    )
    e = _fetch_events(client)[0]
    assert e["utm_source"] == "google"
    assert e["utm_medium"] == "cpcscript"
    assert e["utm_campaign"] == "x" * 100


def test_utm_garbage_only_becomes_null(client):
    client.get("/", params={"utm_source": "<>!&?"}, headers=_HUMAN_UA)
    e = _fetch_events(client)[0]
    assert e["utm_source"] is None  # мусор → NULL, атрибуция best-effort
    assert e["utm_medium"] is None  # отсутствующий параметр тоже NULL


def test_external_referrer_stored_truncated(client):
    ref = "https://www.google.com/search?q=" + "a" * 300
    client.get("/", headers={"Referer": ref, **_HUMAN_UA})
    e = _fetch_events(client)[0]
    assert e["referrer"] == ref[:200]


def test_internal_referrer_is_null(client):
    # свой хост (request) и публичный base_url — оба «внутренние», в referrer не попадают
    client.get("/catalog", headers={"Referer": "http://testserver/", **_HUMAN_UA})
    client.get("/map", headers={"Referer": "https://rent.example.com/catalog", **_HUMAN_UA})
    events = _fetch_events(client)
    assert [e["referrer"] for e in events] == [None, None]
