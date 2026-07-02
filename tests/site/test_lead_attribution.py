"""Stage T / T2-3 — атрибуция заявок: hidden-поля utm.js → leads.utm_*/referrer + submit-события.

Закрепляем контрактно (DATA_CONTRACT-T §A.1/§A.4):
- clean_utm/clean_referrer: санитайз [A-Za-z0-9._-] ≤100 / внешний ≤200; мусор → NULL, не 400;
- create_lead сохраняет атрибуцию (writer — последний рубеж §A.1, даже для «сырого» payload);
- POST /lead: приём hidden-полей; no-JS фоллбэк — utm из query Referer-страницы; свой хост → NULL;
- lead_submit/subscribe_submit пишутся ПОСЛЕ коммита и ДЛЯ HTMX-сабмитов тоже (HX-скип
  record_event — про pageview-пагинацию, не про сабмиты); honeypot/400 → события нет;
- разметка: hidden-поля + <script src=".../utm.js?v="> в шаблонах ФОРМ, не в base.html (§A.4).

Как в test_analytics.py: стаб-шаблоны через DictLoader, события читаем из файла-БД фикстуры.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from jinja2 import DictLoader
from sqlalchemy import select

import src.site.routes as routes
from src.config import get_settings
from src.db.models import Lead, SiteEvent
from src.site import cache
from src.site import db as sitedb
from src.site.app import create_app
from src.site.deps import CSRF_FORM_FIELD, SITE_CSRF_TOKEN, catalog_db_url, lead_rate_limiter
from src.site.leads import LeadPayload, clean_referrer, clean_utm, create_lead

_STUBS = {
    "partials/_lead_ok.html": "TPL:_lead_ok",
    "partials/_subscribe_ok.html": "TPL:_subscribe_ok",
}

_HUMAN_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/125.0 Safari/537.36"}


@pytest.fixture(autouse=True)
def _stub_templates(monkeypatch):
    monkeypatch.setattr(routes.templates.env, "loader", DictLoader(_STUBS))
    yield


@pytest.fixture(autouse=True)
def _clear_state():
    cache.catalog_cache.clear()
    cache.facets_cache.clear()
    lead_rate_limiter.reset()
    yield
    cache.catalog_cache.clear()
    cache.facets_cache.clear()
    lead_rate_limiter.reset()


@pytest.fixture(autouse=True)
def _mute_notifiers(monkeypatch):
    """Ни Telegram, ни e-mail не должны дёргаться в тестах — атрибуция их не касается."""
    monkeypatch.setattr("src.site.leads.send_text_to_recipients", AsyncMock())
    monkeypatch.setattr("src.site.leads.send_lead_email", AsyncMock())
    yield


@pytest.fixture
def client(site_db):
    app = create_app()
    app.dependency_overrides[catalog_db_url] = lambda: site_db
    with TestClient(app) as c:
        yield c


def _csrf_form(**extra) -> dict[str, str]:
    return {CSRF_FORM_FIELD: SITE_CSRF_TOKEN, **extra}


def _sub_form(**extra) -> dict[str, str]:
    # Подписке нужен хотя бы один критерий + контакт e-mail канала.
    return _csrf_form(price_max="800", email="you@example.com", **extra)


async def _fetch_leads_async(url: str) -> list[Lead]:
    async with sitedb.read_session(database_url=url) as s:
        return list((await s.execute(select(Lead).order_by(Lead.id))).scalars().all())


def _fetch_leads(url: str) -> list[Lead]:
    return asyncio.run(_fetch_leads_async(url))


def _fetch_events(url: str) -> list[dict]:
    async def _go() -> list[dict]:
        async with sitedb.read_session(database_url=url) as s:
            rows = (await s.execute(select(SiteEvent).order_by(SiteEvent.id))).scalars().all()
            return [
                {
                    "type": r.type, "path": r.path, "apt_id": r.apt_id, "visitor": r.visitor,
                    "referrer": r.referrer, "utm_source": r.utm_source,
                    "utm_medium": r.utm_medium, "utm_campaign": r.utm_campaign,
                }
                for r in rows
            ]

    return asyncio.run(_go())


# ============================================================== unit: clean_utm (§A.1)


def test_clean_utm_passthrough_for_valid_value():
    assert clean_utm("tg-adv_01.b") == "tg-adv_01.b"


def test_clean_utm_strips_forbidden_chars():
    assert clean_utm("cpc<script>&?=") == "cpcscript"


def test_clean_utm_truncates_to_100():
    assert clean_utm("x" * 300) == "x" * 100


def test_clean_utm_garbage_only_is_none():
    assert clean_utm("<>!&? ") is None


def test_clean_utm_empty_and_none_are_none():
    assert clean_utm("") is None
    assert clean_utm(None) is None


# ============================================================== unit: clean_referrer (§A.1/§A.4)


def test_clean_referrer_keeps_external_url():
    assert clean_referrer("https://www.google.com/search?q=belgrade") == (
        "https://www.google.com/search?q=belgrade"
    )


def test_clean_referrer_truncates_to_200():
    ref = "https://www.google.com/search?q=" + "a" * 300
    assert clean_referrer(ref) == ref[:200]


def test_clean_referrer_drops_own_hosts_case_insensitive():
    assert clean_referrer("https://RentBelgrad.com/catalog", own_hosts=("rentbelgrad.com",)) is None
    assert clean_referrer("http://testserver/", own_hosts=("testserver", "")) is None


def test_clean_referrer_relative_or_hostless_is_none():
    assert clean_referrer("/catalog?page=2") is None
    assert clean_referrer("not a url") is None


def test_clean_referrer_malformed_is_none_not_raise():
    assert clean_referrer("http://[broken") is None  # urlsplit кидает ValueError — глотаем


def test_clean_referrer_empty_and_none_are_none():
    assert clean_referrer("") is None
    assert clean_referrer("   ") is None
    assert clean_referrer(None) is None


# ============================================================== unit: create_lead persists (§A.1)


@pytest.mark.asyncio
async def test_create_lead_persists_attribution(site_db: str) -> None:
    payload = LeadPayload(
        name="Иван", contact="@ivan",
        utm_source="tg", utm_medium="social", utm_campaign="summer",
        referrer="https://t.me/some_channel/42",
    )
    lead = await create_lead(payload, database_url=site_db)
    assert lead is not None
    saved = (await _fetch_leads_async(site_db))[0]
    assert saved.utm_source == "tg"
    assert saved.utm_medium == "social"
    assert saved.utm_campaign == "summer"
    assert saved.referrer == "https://t.me/some_channel/42"


@pytest.mark.asyncio
async def test_create_lead_sanitizes_garbage_attribution_to_null(site_db: str) -> None:
    # Писатель — последний рубеж §A.1: даже сырой payload не роняет заявку и не пишет мусор.
    payload = LeadPayload(
        name="Иван", contact="@ivan",
        utm_source="<>!?", utm_medium="cpc<b>", utm_campaign="y" * 300,
        referrer="not-a-url",
    )
    lead = await create_lead(payload, database_url=site_db)
    assert lead is not None
    saved = (await _fetch_leads_async(site_db))[0]
    assert saved.utm_source is None
    assert saved.utm_medium == "cpcb"
    assert saved.utm_campaign == "y" * 100
    assert saved.referrer is None


@pytest.mark.asyncio
async def test_create_lead_defaults_attribution_to_null(site_db: str) -> None:
    lead = await create_lead(LeadPayload(name="Иван", contact="@ivan"), database_url=site_db)
    assert lead is not None
    saved = (await _fetch_leads_async(site_db))[0]
    assert (saved.utm_source, saved.utm_medium, saved.utm_campaign, saved.referrer) == (
        None, None, None, None,
    )


# ============================================================== POST /lead: атрибуция в БД


def test_lead_form_attribution_lands_in_db(client, site_db):
    r = client.post(
        "/lead",
        data=_csrf_form(
            name="Анна", contact="@anna",
            utm_source="tg", utm_medium="social", utm_campaign="summer",
            referrer="https://t.me/some_channel/42",
        ),
        headers=_HUMAN_UA,
        follow_redirects=False,
    )
    assert r.status_code in (200, 303)
    saved = _fetch_leads(site_db)[0]
    assert saved.utm_source == "tg"
    assert saved.utm_medium == "social"
    assert saved.utm_campaign == "summer"
    assert saved.referrer == "https://t.me/some_channel/42"


def test_lead_garbage_utm_is_null_not_400(client, site_db):
    r = client.post(
        "/lead",
        data=_csrf_form(name="Анна", contact="@anna", utm_source="<>&?", utm_campaign="c" * 300),
        headers=_HUMAN_UA,
        follow_redirects=False,
    )
    assert r.status_code in (200, 303)  # атрибуция best-effort: мусор → NULL, не 400
    saved = _fetch_leads(site_db)[0]
    assert saved.utm_source is None
    assert saved.utm_campaign == "c" * 100


def test_lead_internal_referrer_field_is_null(client, site_db):
    # Свой хост (текущий request) — навигация по сайту, не источник: в leads.referrer не пишем.
    client.post(
        "/lead",
        data=_csrf_form(name="Анна", contact="@anna", referrer="http://testserver/catalog"),
        headers=_HUMAN_UA,
        follow_redirects=False,
    )
    assert _fetch_leads(site_db)[0].referrer is None


def test_lead_public_base_url_referrer_is_null(client, site_db, monkeypatch):
    # И публичный base_url — тоже «свой» хост (за прокси request-хост может отличаться).
    monkeypatch.setattr(get_settings(), "site_public_base_url", "https://rent.example.com")
    client.post(
        "/lead",
        data=_csrf_form(name="Анна", contact="@anna", referrer="https://rent.example.com/x"),
        headers=_HUMAN_UA,
        follow_redirects=False,
    )
    assert _fetch_leads(site_db)[0].referrer is None


# ============================================================== POST /lead: no-JS фоллбэк (§A.4)


def test_lead_nojs_fallback_takes_utm_from_referer_query(client, site_db):
    # Без JS hidden-поля пусты, но Referer = страница формы, куда посетитель пришёл с метками.
    client.post(
        "/lead",
        data=_csrf_form(name="Анна", contact="@anna"),
        headers={"Referer": "http://testserver/?utm_source=tg&utm_medium=social", **_HUMAN_UA},
        follow_redirects=False,
    )
    saved = _fetch_leads(site_db)[0]
    assert saved.utm_source == "tg"
    assert saved.utm_medium == "social"
    assert saved.utm_campaign is None
    assert saved.referrer is None  # Referer тут — своя страница, во внешний referrer не идёт


def test_lead_form_fields_win_over_referer_fallback(client, site_db):
    client.post(
        "/lead",
        data=_csrf_form(name="Анна", contact="@anna", utm_source="js-first-touch"),
        headers={"Referer": "http://testserver/?utm_source=stale", **_HUMAN_UA},
        follow_redirects=False,
    )
    assert _fetch_leads(site_db)[0].utm_source == "js-first-touch"


def test_lead_no_attribution_at_all_is_null(client, site_db):
    client.post(
        "/lead",
        data=_csrf_form(name="Анна", contact="@anna"),
        headers=_HUMAN_UA,
        follow_redirects=False,
    )
    saved = _fetch_leads(site_db)[0]
    assert (saved.utm_source, saved.utm_medium, saved.utm_campaign, saved.referrer) == (
        None, None, None, None,
    )


# ============================================================== событие lead_submit (§A.3)


def test_lead_submit_event_written_after_commit(client, site_db):
    client.post(
        "/lead",
        data=_csrf_form(
            name="Анна", contact="@anna",
            utm_source="tg", utm_medium="social", utm_campaign="summer",
            referrer="https://news.ycombinator.com/item?id=1",
        ),
        headers=_HUMAN_UA,
        follow_redirects=False,
    )
    assert len(_fetch_leads(site_db)) == 1  # событие — строго ПОСЛЕ коммита заявки
    events = _fetch_events(site_db)
    assert len(events) == 1
    e = events[0]
    assert e["type"] == "lead_submit"
    assert e["path"] == "/lead"
    assert e["apt_id"] is None  # общая заявка — без квартиры
    # Событие несёт ту же first-touch атрибуцию, что и заявка (POST сам по себе меток не имеет).
    assert e["utm_source"] == "tg"
    assert e["utm_medium"] == "social"
    assert e["utm_campaign"] == "summer"
    assert e["referrer"] == "https://news.ycombinator.com/item?id=1"


def test_lead_submit_event_written_for_htmx_submit(client, site_db):
    # КЛЮЧЕВОЕ: реальная форма шлёт hx-post. HX-скип record_event — про pageview-пагинацию;
    # submit-событие обязано записаться и для HTMX-сабмита.
    r = client.post(
        "/lead",
        data=_csrf_form(name="Анна", contact="@anna", utm_source="tg"),
        headers={"HX-Request": "true", **_HUMAN_UA},
    )
    assert r.status_code == 200
    events = _fetch_events(site_db)
    assert len(events) == 1
    assert events[0]["type"] == "lead_submit"
    assert events[0]["utm_source"] == "tg"


def test_lead_submit_event_carries_apt_id_for_listing_lead(client, site_db):
    client.post(
        "/lead",
        data=_csrf_form(name="Пётр", contact="@petr", site_listing_id="1"),  # id=1 публичная
        headers=_HUMAN_UA,
        follow_redirects=False,
    )
    events = _fetch_events(site_db)
    assert len(events) == 1
    assert events[0]["apt_id"] == 1  # §A.4: lead_submit с apt_id для per-listing заявок


def test_lead_honeypot_writes_no_event(client, site_db):
    r = client.post(
        "/lead",
        data=_csrf_form(name="Bot", contact="spam", company="ACME"),
        headers=_HUMAN_UA,
        follow_redirects=False,
    )
    assert r.status_code in (200, 303)  # боту — тихий OK
    assert _fetch_leads(site_db) == []
    assert _fetch_events(site_db) == []  # нет коммита — нет события


def test_lead_validation_error_writes_no_event(client, site_db):
    r = client.post("/lead", data=_csrf_form(name="", contact="@x"), headers=_HUMAN_UA)
    assert r.status_code == 400
    assert _fetch_events(site_db) == []


def test_lead_kill_switch_saves_lead_but_no_event(client, site_db, monkeypatch):
    monkeypatch.setattr(get_settings(), "site_analytics_enabled", False)
    client.post(
        "/lead",
        data=_csrf_form(name="Анна", contact="@anna", utm_source="tg"),
        headers=_HUMAN_UA,
        follow_redirects=False,
    )
    saved = _fetch_leads(site_db)
    assert len(saved) == 1
    assert saved[0].utm_source == "tg"  # атрибуция заявки живёт и без телеметрии
    assert _fetch_events(site_db) == []


# ============================================================== событие subscribe_submit (§A.3)


def test_subscribe_submit_event_written_after_commit(client, site_db):
    r = client.post(
        "/subscribe",
        data=_sub_form(utm_source="tg", referrer="https://t.me/some_channel/42"),
        headers={"HX-Request": "true", **_HUMAN_UA},
    )
    assert r.status_code == 200
    events = _fetch_events(site_db)
    assert len(events) == 1
    e = events[0]
    assert e["type"] == "subscribe_submit"
    assert e["path"] == "/subscribe"
    assert e["apt_id"] is None
    assert e["utm_source"] == "tg"
    assert e["referrer"] == "https://t.me/some_channel/42"


def test_subscribe_honeypot_writes_no_event(client, site_db):
    r = client.post(
        "/subscribe",
        data=_sub_form(company="ACME"),
        headers=_HUMAN_UA,
        follow_redirects=False,
    )
    assert r.status_code in (200, 303)
    assert _fetch_events(site_db) == []


def test_subscribe_validation_error_writes_no_event(client, site_db):
    r = client.post(
        "/subscribe",
        data=_csrf_form(price_max="", email=""),  # ни одного критерия → 400
        headers=_HUMAN_UA,
    )
    assert r.status_code == 400
    assert _fetch_events(site_db) == []


# ============================================================== разметка форм (§A.4)


def _real_env():
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
    env.globals["site_telegram_bot_username"] = "otz_bot"
    env.globals["asset_v"] = "testver"
    return env


@pytest.mark.parametrize("template", ["partials/_lead_form.html", "partials/_subscribe_form.html"])
def test_form_has_attribution_hidden_fields_and_script(template):
    html = _real_env().get_template(template).render()
    for name in ("utm_source", "utm_medium", "utm_campaign", "referrer"):
        assert f'name="{name}"' in html, f"{template}: hidden-поле {name} отсутствует"
    assert "/static/js/utm.js?v=testver" in html  # кэш-бастинг ?v={{ asset_v }} обязателен


def test_base_html_does_not_include_utm_js():
    # §A.4: подключение в base.html ЗАПРЕЩЕНО — только в шаблонах форм.
    from pathlib import Path

    base = (
        Path(__file__).resolve().parents[2] / "src" / "site" / "templates" / "base.html"
    ).read_text(encoding="utf-8")
    assert "utm.js" not in base


def test_utm_js_uses_session_storage_and_no_cookies():
    from pathlib import Path

    js = (
        Path(__file__).resolve().parents[2] / "src" / "site" / "static" / "js" / "utm.js"
    ).read_text(encoding="utf-8")
    assert "sessionStorage" in js  # §A.4: first-touch живёт в sessionStorage…
    assert "document.cookie" not in js  # …и строго без cookies
    assert "utm_source" in js and "referrer" in js
