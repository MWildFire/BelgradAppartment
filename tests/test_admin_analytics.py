"""Admin /analytics dashboard (T3-1) — §A.5 context builder + GET route.

build_analytics(session, days): прошлые дни читаются из site_stats_daily, «сегодня» —
live-подсчёт из raw site_events с bot-семантикой rollup-а §A.2 (pageview/uniques —
is_bot=0, leads — как есть). Уники за период — СУММА дневных уников (несвязываемы
между днями по построению соли — честная подпись в UI).
Роут: GET /analytics?days=7|30, basic-auth, read-only.
"""

from __future__ import annotations

import base64
import itertools
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

from src.admin.analytics import TOP_LIMIT, build_analytics
from src.admin.app import create_app
from src.db.models import (
    DailyMetrics,
    InboundPost,
    Listing,
    Match,
    Platform,
    SiteEvent,
    SiteListing,
    SiteStatsDaily,
    SourceChannel,
)
from tests.conftest import fresh_db

CREDS = base64.b64encode(b"darya:test_secret_123").decode()
AUTH = {"Authorization": f"Basic {CREDS}"}

# детерминированные tg_id для сид-каналов (hash() зависит от PYTHONHASHSEED — не годится)
_TG_IDS = itertools.count(900_000_001)

# Полуночный флак: сид «сегодняшних» событий и build_analytics читали wall-clock ПОРОЗНЬ —
# прогон через 00:00 UTC рассинхронизировал live-день и окно (ложное красное). Морозим одну
# дату и для сидов (_today/_seed_event), и для SUT (datetime модуля src.admin.analytics) —
# паттерн инжекции времени, как retention(now=...) в tests/site/test_analytics_rollup.py.
FROZEN_NOW = datetime(2026, 7, 2, 12, 0, tzinfo=UTC)


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return FROZEN_NOW if tz else FROZEN_NOW.replace(tzinfo=None)


@pytest.fixture(autouse=True)
def _freeze_clock(monkeypatch):
    import src.admin.analytics as admin_analytics

    monkeypatch.setattr(admin_analytics, "datetime", _FrozenDatetime)
    yield


@pytest.fixture(autouse=True)
def _force_admin_password(monkeypatch):
    from src.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "admin_username", "darya")
    monkeypatch.setattr(s, "admin_password", "test_secret_123")
    yield


@pytest_asyncio.fixture
async def db():
    async with fresh_db() as (_engine, factory):
        yield factory


@pytest.fixture
def client():
    return TestClient(create_app())


def _today() -> date:
    return FROZEN_NOW.date()  # та же замороженная дата, что видит build_analytics


async def _seed_stat(db, day: date, metric: str, key: str = "", value: int = 0) -> None:
    async with db() as s:
        s.add(SiteStatsDaily(day=day, metric=metric, key=key, value=value))
        await s.commit()


async def _seed_event(db, **kw) -> None:
    """Raw site_event «сегодня» (live-часть окна); дефолты — обычный человеческий pageview."""
    async with db() as s:
        s.add(
            SiteEvent(
                ts=kw.pop("ts", FROZEN_NOW),
                type=kw.pop("type", "pageview"),
                path=kw.pop("path", "/"),
                visitor=kw.pop("visitor", "v1"),
                **kw,
            )
        )
        await s.commit()


async def _seed_apt(db, *, district: str = "Vračar", price_eur: int = 700) -> int:
    """Полная цепочка SiteListing→Match→Listing(+InboundPost) — возвращает site_listings.id."""
    async with db() as s:
        ch = SourceChannel(tg_id=next(_TG_IDS))
        s.add(ch)
        await s.flush()
        ib = InboundPost(source_channel_id=ch.id, tg_message_id=1)
        listing = Listing(
            platform=Platform.NEKRETNINE,
            external_id=f"apt-{district}-{price_eur}",
            url="https://example.com/x",
            district=district,
            price_eur=price_eur,
        )
        s.add_all([ib, listing])
        await s.flush()
        m = Match(inbound_post_id=ib.id, listing_id=listing.id)
        s.add(m)
        await s.flush()
        site = SiteListing(match_id=m.id)
        s.add(site)
        await s.commit()
        return site.id


# --------------------------------------------------------------------- build_analytics (unit)


async def test_empty_db_zero_cards_and_zero_filled_series(db):
    async with db() as s:
        ctx = await build_analytics(s, days=7)
    assert ctx["days"] == 7
    assert ctx["cards"] == {"views": 0, "uniques": 0, "leads": 0, "conv_pct": 0.0}
    # ряд плотный: ровно 7 дней, нули вместо дыр (спарклайн не должен «прыгать» по дырам)
    assert len(ctx["series"]) == 7
    assert all(p["views"] == 0 and p["leads"] == 0 for p in ctx["series"])
    assert ctx["series"][-1]["day"] == _today()
    assert ctx["top_apts"] == []
    assert ctx["sources"] == []


async def test_past_days_come_from_daily_stats(db):
    y1 = _today() - timedelta(days=1)
    y2 = _today() - timedelta(days=2)
    outside = _today() - timedelta(days=10)  # за пределами окна 7д — не считается
    await _seed_stat(db, y1, "views_path", "/", 5)
    await _seed_stat(db, y1, "views_path", "/apt", 3)  # views дня = сумма по путям
    await _seed_stat(db, y1, "uniques", "", 4)
    await _seed_stat(db, y1, "leads", "", 2)
    await _seed_stat(db, y2, "views_path", "/", 10)
    await _seed_stat(db, y2, "uniques", "", 6)
    await _seed_stat(db, outside, "views_path", "/", 999)
    await _seed_stat(db, outside, "uniques", "", 999)

    async with db() as s:
        ctx = await build_analytics(s, days=7)
    assert ctx["cards"]["views"] == 18
    assert ctx["cards"]["uniques"] == 10  # сумма дневных уников
    assert ctx["cards"]["leads"] == 2
    by_day = {p["day"]: p for p in ctx["series"]}
    assert by_day[y1] == {"day": y1, "views": 8, "leads": 2}
    assert by_day[y2] == {"day": y2, "views": 10, "leads": 0}
    assert outside not in by_day


async def test_today_is_live_from_raw_events_not_stats(db):
    # партиал-роллап за сегодня должен ИГНОРИРОВАТЬСЯ — иначе двойной счёт с live-подсчётом;
    # views_apt/src_utm покрывают отдельный код-путь _stats_by_key (top_apts/sources)
    await _seed_stat(db, _today(), "views_path", "/", 999)
    await _seed_stat(db, _today(), "uniques", "", 999)
    await _seed_stat(db, _today(), "views_apt", "777", 999)
    await _seed_stat(db, _today(), "src_utm", "tg", 999)
    await _seed_event(db, visitor="a")
    await _seed_event(db, visitor="a", path="/catalog")
    await _seed_event(db, visitor="b")
    await _seed_event(db, visitor="bot", is_bot=True)  # боты пишутся, но не считаются
    await _seed_event(db, visitor="a", type="lead_submit")
    # вчерашнее raw-событие в live-подсчёт «сегодня» не входит (его закрывает rollup)
    await _seed_event(db, visitor="c", ts=FROZEN_NOW - timedelta(days=1))

    async with db() as s:
        ctx = await build_analytics(s, days=7)
    today_point = ctx["series"][-1]
    assert today_point["day"] == _today()
    assert today_point["views"] == 3
    assert today_point["leads"] == 1
    assert ctx["cards"]["views"] == 3
    assert ctx["cards"]["uniques"] == 2
    assert ctx["cards"]["leads"] == 1
    # live-события выше без apt_id/utm — непустой результат мог прийти только из партиала
    assert ctx["top_apts"] == []
    assert ctx["sources"] == []


async def test_leads_today_count_bots_mirroring_rollup(db):
    # §A.2: rollup считает leads/leads_apt БЕЗ фильтра is_bot=0 (в отличие от pageview/uniques).
    # Live-«сегодня» обязан зеркалить ту же семантику — иначе заявки и conv/ctr за сегодня
    # задним числом вырастут после ночного rollup-а, а окно смешает две методики.
    apt = await _seed_apt(db)
    await _seed_event(db, visitor="h")  # человеческий pageview
    await _seed_event(db, visitor="bot", is_bot=True, type="lead_submit", path="/apt", apt_id=apt)

    async with db() as s:
        ctx = await build_analytics(s, days=7)
    assert ctx["cards"]["leads"] == 1  # bot-заявка считается — как в rollup-е
    assert ctx["cards"]["views"] == 1  # pageview/uniques ботов по-прежнему исключают
    assert ctx["cards"]["uniques"] == 1
    top = {a["apt_id"]: a for a in ctx["top_apts"]}
    assert top[apt]["leads"] == 1


async def test_conversion_pct(db):
    y1 = _today() - timedelta(days=1)
    await _seed_stat(db, y1, "views_path", "/", 4)
    await _seed_stat(db, y1, "leads", "", 1)
    async with db() as s:
        ctx = await build_analytics(s, days=7)
    assert ctx["cards"]["conv_pct"] == pytest.approx(25.0)


async def test_top_apts_merges_stats_and_live_sorts_by_views(db):
    apt_a = await _seed_apt(db, district="Vračar", price_eur=700)
    apt_b = await _seed_apt(db, district="Zvezdara", price_eur=550)
    y1 = _today() - timedelta(days=1)
    # A: 5 просмотров вчера (агрегат) + 1 сегодня (live) = 6; заявки 2+1 = 3
    await _seed_stat(db, y1, "views_apt", str(apt_a), 5)
    await _seed_stat(db, y1, "leads_apt", str(apt_a), 2)
    await _seed_event(db, path="/apt", apt_id=apt_a)
    await _seed_event(db, type="lead_submit", path="/apt", apt_id=apt_a)
    # B: 10 просмотров, 0 заявок → первый по views, ctr 0
    await _seed_stat(db, y1, "views_apt", str(apt_b), 10)

    async with db() as s:
        ctx = await build_analytics(s, days=7)
    top = ctx["top_apts"]
    assert [a["apt_id"] for a in top] == [apt_b, apt_a]
    a = top[1]
    assert a["views"] == 6 and a["leads"] == 3
    assert a["district"] == "Vračar" and a["price_eur"] == 700
    assert a["ctr_pct"] == pytest.approx(50.0)
    assert top[0]["ctr_pct"] == 0.0  # div0 → 0.0


async def test_top_apts_survives_missing_site_listing(db):
    # событие — исторический факт: квартира могла быть удалена с сайта, дашборд не падает
    y1 = _today() - timedelta(days=1)
    await _seed_stat(db, y1, "views_apt", "9999", 7)
    async with db() as s:
        ctx = await build_analytics(s, days=7)
    assert ctx["top_apts"][0]["apt_id"] == 9999
    assert ctx["top_apts"][0]["district"] is None
    assert ctx["top_apts"][0]["price_eur"] is None


async def test_top_apts_capped_at_limit(db):
    y1 = _today() - timedelta(days=1)
    for i in range(TOP_LIMIT + 2):
        await _seed_stat(db, y1, "views_apt", str(100 + i), 50 - i)
    async with db() as s:
        ctx = await build_analytics(s, days=7)
    assert len(ctx["top_apts"]) == TOP_LIMIT
    # сортировка по views desc — первым идёт самый просматриваемый
    assert ctx["top_apts"][0]["views"] == 50


async def test_sources_merge_stats_and_live_with_ref_domain(db):
    y1 = _today() - timedelta(days=1)
    await _seed_stat(db, y1, "src_utm", "tg", 5)
    await _seed_stat(db, y1, "src_ref", "yandex.ru", 2)
    await _seed_event(db, utm_source="tg")  # live-надбавка к агрегату: tg = 6
    await _seed_event(db, visitor="z", referrer="https://google.com/search?q=x")

    async with db() as s:
        ctx = await build_analytics(s, days=7)
    srcs = {(r["kind"], r["key"]): r["views"] for r in ctx["sources"]}
    assert srcs[("utm", "tg")] == 6
    assert srcs[("ref", "yandex.ru")] == 2
    assert srcs[("ref", "google.com")] == 1  # live-referrer сведён к домену
    # сортировка по views desc
    views = [r["views"] for r in ctx["sources"]]
    assert views == sorted(views, reverse=True)


async def test_sources_survive_garbage_referrer_in_db(db):
    # write-side обрезает referrer до 200 символов: у длинного URL с userinfo и IPv6-хостом
    # теряется закрывающая ']' — urlparse на чтении кидает ValueError('Invalid IPv6 URL').
    # Мусорная строка должна молча выпасть из топа, а не ронять дашборд в 500.
    truncated = ("http://" + "u" * 188 + "@[::1]/x")[:200]
    await _seed_event(db, referrer=truncated)
    await _seed_event(db, visitor="z", referrer="https://google.com/x")

    async with db() as s:
        ctx = await build_analytics(s, days=7)
    srcs = {(r["kind"], r["key"]): r["views"] for r in ctx["sources"]}
    assert srcs == {("ref", "google.com"): 1}


async def test_sources_capped_at_limit(db):
    y1 = _today() - timedelta(days=1)
    for i in range(TOP_LIMIT + 3):
        await _seed_stat(db, y1, "src_utm", f"src{i}", 40 - i)
    async with db() as s:
        ctx = await build_analytics(s, days=7)
    assert len(ctx["sources"]) == TOP_LIMIT


async def test_days_30_window_includes_older_stats(db):
    old = _today() - timedelta(days=20)
    await _seed_stat(db, old, "views_path", "/", 11)
    async with db() as s:
        ctx7 = await build_analytics(s, days=7)
        ctx30 = await build_analytics(s, days=30)
    assert ctx7["cards"]["views"] == 0
    assert ctx30["cards"]["views"] == 11
    assert len(ctx30["series"]) == 30


# --------------------------------------------------------------------- GET /analytics (route)


def test_analytics_requires_auth(client, db):
    assert client.get("/analytics").status_code == 401


def test_analytics_renders_empty_db(client, db):
    r = client.get("/analytics", headers=AUTH)
    assert r.status_code == 200
    # inline-SVG спарклайн из Jinja — ноль JS-библиотек на странице
    assert "<svg" in r.text and "polyline" in r.text
    assert "chart.js" not in r.text.lower()
    # честная подпись методики уников
    assert "дневные уники, сумма" in r.text
    # «сегодня» помечено как live-подсчёт из raw
    assert "live" in r.text


def test_analytics_days_switch_and_fallback(client, db):
    r30 = client.get("/analytics?days=30", headers=AUTH)
    assert r30.status_code == 200
    assert 'aria-current="page">30' in r30.text
    # мусорное значение падает в дефолт 7, а не в 500
    r_bad = client.get("/analytics?days=999", headers=AUTH)
    assert r_bad.status_code == 200
    assert 'aria-current="page">7' in r_bad.text
    # нечисловое — тоже дефолт (ручной str-парсинг, как window у /published), а не 422
    r_nan = client.get("/analytics?days=abc", headers=AUTH)
    assert r_nan.status_code == 200
    assert 'aria-current="page">7' in r_nan.text


async def test_analytics_links_top_apt_to_public_site(client, db):
    apt = await _seed_apt(db)
    await _seed_event(db, path="/apt", apt_id=apt)
    r = client.get("/analytics", headers=AUTH)
    assert r.status_code == 200
    # ссылка ведёт на ПУБЛИЧНЫЙ /apt/{id} (другой app/домен), не на роут админки
    assert f"/apt/{apt}" in r.text
    from src.config import get_settings

    assert get_settings().site_public_base_url.rstrip("/") + f"/apt/{apt}" in r.text


def test_analytics_in_nav(client, db):
    r = client.get("/analytics", headers=AUTH)
    assert r.status_code == 200
    assert 'href="/analytics"' in r.text


# --------------------------------------------------------------- review fixes (a11y / шрифт / полировка)


async def test_analytics_tables_th_have_scope_col(client, db):
    # a11y-закон: заголовки data-таблиц обязаны объявлять scope — иначе скринридер
    # не свяжет ячейку с колонкой. Сеем данные, чтобы отрендерились ОБЕ таблицы.
    apt = await _seed_apt(db)
    await _seed_event(db, path="/apt", apt_id=apt)
    await _seed_event(db, visitor="z", utm_source="tg")
    r = client.get("/analytics", headers=AUTH)
    assert r.status_code == 200
    ths = re.findall(r"<th\b[^>]*>", r.text)
    assert len(ths) >= 9  # 6 (топ-квартиры) + 3 (источники)
    assert all('scope="col"' in th for th in ths)


def test_analytics_selfhosts_jetbrains_mono(client, db):
    # Закон «цифры — всегда mono» выполняется лишь если шрифт реально грузится:
    # страница обязана нести @font-face на woff2 со СВОЕГО origin (admin.* ≠ сайт,
    # кросс-доменный @font-face требует CORS, которого у статики сайта нет).
    r = client.get("/analytics", headers=AUTH)
    assert r.status_code == 200
    assert "@font-face" in r.text
    # все 4 сабсета, как у публичного site.css, + кэш-бастинг ?v=
    for part in ("latin", "latin-ext", "cyrillic", "cyrillic-ext"):
        assert f"/analytics/fonts/jetbrains-mono-{part}.woff2?v=" in r.text


def test_analytics_font_route_serves_public_site_woff2(client, db):
    r = client.get("/analytics/fonts/jetbrains-mono-latin.woff2", headers=AUTH)
    assert r.status_code == 200
    assert r.headers["content-type"] == "font/woff2"
    assert "immutable" in r.headers.get("cache-control", "")
    src = (
        Path(__file__).resolve().parents[1]
        / "src" / "site" / "static" / "fonts" / "jetbrains-mono-latin.woff2"
    )
    assert r.content == src.read_bytes()  # тот же бинарь, без дублирования файла


def test_analytics_font_route_is_whitelisted_and_authed(client, db):
    # не файл-сервер: только известные woff2, и только за basic-auth (fail-closed)
    assert client.get("/analytics/fonts/evil.woff2", headers=AUTH).status_code == 404
    assert client.get("/analytics/fonts/jetbrains-mono-latin.woff2").status_code == 401


async def test_analytics_source_cells_allow_breaking_long_keys(client, db):
    # utm_source — публичный query-параметр: write-side санитайз даёт до 100 симв.
    # [A-Za-z0-9._-] БЕЗ точек мягкого переноса. Без разрешения на разрыв такая ячейка
    # распирает таблицу за hairline-границу paper-острова и даёт странице горизонтальный
    # скролл — любой визитёр с ?utm_source=<60+ симв.> ломает геометрию дашборда.
    long_key = "a" * 100
    await _seed_event(db, utm_source=long_key)
    r = client.get("/analytics", headers=AUTH)
    assert r.status_code == 200
    assert long_key in r.text  # ключ реально отрендерен в таблице источников
    assert "overflow-wrap:anywhere" in r.text


async def test_analytics_card_numbers_grouped_with_narrow_nbsp(client, db):
    # editorial-группировка разрядов: за 30д карточка легко уходит в 5 знаков — слитное
    # 12345 нечитаемо; группируем узким неразрывным пробелом (U+202F), обычный пробел
    # дал бы перенос строки внутри числа
    y1 = _today() - timedelta(days=1)
    await _seed_stat(db, y1, "views_path", "/", 12345)
    r = client.get("/analytics", headers=AUTH)
    assert r.status_code == 200
    assert "12 345" in r.text
    assert ">12345<" not in r.text


def test_analytics_focus_visible_and_today_dot_polish(client, db):
    r = client.get("/analytics", headers=AUTH)
    assert r.status_code == 200
    # закон «hover/focus-состояния»: кастомный видимый focus-ring
    assert ":focus-visible" in r.text
    # точка «сегодня»: circle в svg c preserveAspectRatio="none" эллипсится —
    # должен быть нуль-длинный path с non-scaling-stroke
    assert "<circle" not in r.text


# --------------------------------------------------- wave-5 review fixes (SRI / имена таблиц / контраст)


async def test_admin_cdn_scripts_pin_sri(client, db):
    # Компрометация CDN (unpkg/jsdelivr) не должна давать исполняемый JS в аутентифицированной
    # админ-сессии: CSRF-токен отрендерен прямо в DOM (hx-headers на <body>). Каждый внешний
    # <script> обязан пиннить integrity + crossorigin — как Leaflet на публичном сайте.
    # chart.js рендерится только в непустой ветке /metrics — сеем снапшот.
    async with db() as s:
        s.add(
            DailyMetrics(
                snapshot_date="2026-07-01",
                platform="nekretnine",
                active_count=1,
                no_photo_count=0,
            )
        )
        await s.commit()
    for path in ("/analytics", "/metrics"):
        r = client.get(path, headers=AUTH)
        assert r.status_code == 200
        ext = re.findall(r'<script\b[^>]*\bsrc="https?://[^>]*>', r.text)
        assert ext, f"{path}: внешних <script src> не найдено — тест не проверяет то, что должен"
        for tag in ext:
            assert re.search(r'integrity="sha384-[A-Za-z0-9+/]+=*"', tag), (path, tag)
            assert 'crossorigin="anonymous"' in tag, (path, tag)


async def test_analytics_tables_have_programmatic_names(client, db):
    # SR-имя таблиц: без caption/aria-labelledby скринридер объявляет «table, 6 columns»
    # без имени. Связываем таблицы с их существующими h2 через aria-labelledby.
    apt = await _seed_apt(db)
    await _seed_event(db, path="/apt", apt_id=apt)
    await _seed_event(db, visitor="z", utm_source="tg")
    r = client.get("/analytics", headers=AUTH)
    assert r.status_code == 200
    tables = re.findall(r"<table\b[^>]*>", r.text)
    assert len(tables) == 2  # «Топ-квартиры» и «Источники»
    for tag in tables:
        m = re.search(r'aria-labelledby="([^"]+)"', tag)
        assert m, f"таблица без aria-labelledby: {tag}"
        # ссылка обязана вести на реально существующий id (иначе имя молча теряется)
        assert f'id="{m.group(1)}"' in r.text


# ---- WCAG-контраст бейджей base.html — та же формула, что в tests/site/test_a11y.py


def _wcag_ratio(fg: str, bg: str) -> float:
    def lum(hexs: str) -> float:
        def lin(c: int) -> float:
            x = c / 255
            return x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4

        h = hexs.lstrip("#")
        r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
        return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)

    la, lb = lum(fg), lum(bg)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def test_admin_status_badges_meet_aa_contrast():
    # Бейджи 12px («на сайте», «пора убрать», платформа) — смысловой текст списков
    # /published и /queue: пары текст/фон обязаны давать AA 4.5:1. Пиним вычисленный
    # контраст, чтобы регрессия палитры ловилась тестом, а не глазами.
    css = (
        Path(__file__).resolve().parents[1] / "src" / "admin" / "templates" / "base.html"
    ).read_text(encoding="utf-8")
    tokens = dict(re.findall(r"--([a-z0-9-]+):\s*(#[0-9a-fA-F]{6})", css))

    def resolve(value: str) -> str:
        m = re.match(r"var\(--([a-z0-9-]+)\)", value.strip())
        return tokens[m.group(1)] if m else value.strip()

    for name in ("green", "yellow", "red"):
        rule = re.search(
            rf"\.badge-{name}\s*\{{\s*background:\s*([^;]+);\s*color:\s*([^;}}]+)", css
        )
        assert rule, f".badge-{name} не найден в base.html"
        bg, fg = resolve(rule.group(1)), resolve(rule.group(2))
        ratio = _wcag_ratio(fg, bg)
        assert ratio >= 4.5, f"badge-{name}: {fg} на {bg} = {ratio:.2f}:1 < 4.5 (AA, 12px)"
