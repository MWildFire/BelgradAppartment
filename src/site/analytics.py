"""Stage T / T2-2 — first-party логгер событий публичного сайта (DATA_CONTRACT-T §A.1/§A.3).

Единственная точка записи site_events: роуты аллоулиста вешают `record_event` через FastAPI
BackgroundTasks — событие пишется ПОСЛЕ отправки ответа и никогда не участвует в request-цикле.
Приватность заложена конструктивно: visitor = sha256(daily_salt‖ip‖ua)[:16], где daily_salt =
sha256(SITE_ANALYTICS_SALT‖YYYY-MM-DD) — сырые IP/UA не пишутся никогда, а суточная соль делает
уников несвязываемыми между днями. Ботов помечаем is_bot=1 (строка дешёвая, отсев — на агрегатах),
поэтому эвристику можно менять задним числом без потери данных.

Гарантия отказоустойчивости (§A.3, тест обязателен): ЛЮБАЯ ошибка внутри record_event глотается
с warn-логом — страница не может 5xx-нуть из-за аналитики. Kill-switch SITE_ANALYTICS_ENABLED
проверяется здесь же, так что все хуки становятся no-op одной настройкой (откат без деплоя).

Rollup/retention (nightly, §A.2/§N) — здесь же: rollup(day) пересчитывает агрегаты UTC-дня
в site_stats_daily целиком (идемпотентно), retention() ротирует raw старше
SITE_ANALYTICS_RAW_RETENTION_DAYS, но ТОЛЬКО для дней с маркером _rolled — несагрегированное
не теряется никогда. Агрегаты живут вечно.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, cast
from urllib.parse import urlsplit

from sqlalchemy import CursorResult, delete, func, select
from starlette.requests import Request

from src.clientip import client_ip
from src.config import get_settings
from src.db.models import SiteEvent, SiteStatsDaily
from src.site.db import writer_session

logger = logging.getLogger(__name__)

# §A.3: UA-подстроки бот-эвристики (case-insensitive). Ловит и заголовочные "python-requests",
# "curl/8", и мониторинги — консервативно широкий список, т.к. false-positive лишь помечает
# строку is_bot=1, а не теряет её.
_BOT_UA_MARKERS = (
    "bot",
    "spider",
    "crawl",
    "slurp",
    "headless",
    "python",
    "curl",
    "wget",
    "monitor",
    "uptime",
    "lighthouse",
    "pingdom",
)

# §A.1: границы полей атрибуции — обрезка при записи, мусор → NULL (best-effort, никогда не 400).
_REFERRER_MAX = 200
_UTM_MAX = 100
_UTM_STRIP_RE = re.compile(r"[^A-Za-z0-9._-]+")


def visitor_hash(ip: str, ua: str, *, secret: str, day: str) -> str:
    """§A.1: visitor = sha256(daily_salt‖ip‖ua)[:16] (hex), daily_salt = sha256(secret‖day).

    day = 'YYYY-MM-DD' (UTC). Соль меняется ежесуточно, поэтому один и тот же посетитель даёт
    РАЗНЫЕ хэши в разные дни — деанонимизация/связывание между днями невозможны по построению.
    """
    daily_salt = hashlib.sha256((secret + day).encode("utf-8")).hexdigest()
    return hashlib.sha256((daily_salt + ip + ua).encode("utf-8")).hexdigest()[:16]


def is_bot_ua(ua: str) -> bool:
    """§A.3: UA содержит любой бот-маркер (case-insensitive) → is_bot=1."""
    low = ua.lower()
    return any(marker in low for marker in _BOT_UA_MARKERS)


def _clean_utm(value: str | None) -> str | None:
    """Санитайз utm_* (§A.1): только [A-Za-z0-9._-], обрезка ≤100; пусто/мусор → NULL."""
    if not value:
        return None
    cleaned = _UTM_STRIP_RE.sub("", value)[:_UTM_MAX]
    return cleaned or None


def _external_referrer(request: Request) -> str | None:
    """Только ВНЕШНИЙ referrer (≤200): свои хосты (текущий request + публичный base_url) → NULL.

    Внутренние переходы — навигация по сайту, не источник трафика; относительные/мусорные
    значения без хоста тоже отбрасываются.
    """
    raw = (request.headers.get("referer") or "").strip()
    if not raw:
        return None
    host = (urlsplit(raw).hostname or "").lower()
    if not host:
        return None
    own_hosts = {
        (request.url.hostname or "").lower(),
        (urlsplit(get_settings().site_public_base_url).hostname or "").lower(),
    }
    if host in own_hosts:
        return None
    return raw[:_REFERRER_MAX]


async def record_event(
    request: Request,
    *,
    path: str,
    event_type: str = "pageview",
    apt_id: int | None = None,
    database_url: str | None = None,
) -> None:
    """Записать одно событие в site_events (тело BackgroundTask; §A.3).

    `path` — уже нормализованный путь аллоулиста ('/apt/{id}' → path='/apt', apt_id =
    site_listings.id): нормализация по построению в точке вызова, а не парсингом URL,
    поэтому кардинальность путей в агрегатах — O(1). Скипы здесь (kill-switch,
    HX-Request), «не-2xx» — размещением хука в success-пути хэндлера. Реюз writer_session
    (busy_timeout=5000) — отдельный от read-engine писатель, как для Lead.

    НИКОГДА не бросает: любая ошибка (хэш, коннект, insert) → warn-лог и выход — страница,
    к которой прицеплен таск, уже отправлена и не должна знать о судьбе телеметрии.
    """
    try:
        settings = get_settings()
        if not settings.site_analytics_enabled:  # kill-switch: все хуки no-op
            return
        if "hx-request" in request.headers:  # HTMX-пагинация/фильтры ≠ pageview
            return
        ua = request.headers.get("user-agent", "")
        now = datetime.now(UTC)
        event = SiteEvent(
            ts=now,
            type=event_type,
            path=path,
            apt_id=apt_id,
            visitor=visitor_hash(
                client_ip(request),
                ua,
                secret=settings.site_analytics_salt,
                day=now.strftime("%Y-%m-%d"),
            ),
            referrer=_external_referrer(request),
            utm_source=_clean_utm(request.query_params.get("utm_source")),
            utm_medium=_clean_utm(request.query_params.get("utm_medium")),
            utm_campaign=_clean_utm(request.query_params.get("utm_campaign")),
            is_bot=is_bot_ua(ua),
        )
        async with writer_session(database_url=database_url) as session:
            session.add(event)
    except Exception:
        # §A.3: телеметрия некритична — глотаем всё, но оставляем след для оператора.
        logger.warning(
            "site analytics: %s event for %s dropped (writer failure)",
            event_type,
            path,
            exc_info=True,
        )


# ============================================================== T2-4: rollup + retention (§A.2/§N)

# Маркер «день сагрегирован» — единственное, что разрешает retention трогать raw этого дня.
_ROLLED = "_rolled"


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    """Полуоткрытая UTC-граница дня [00:00, +24h) — день события = дата его ts в UTC."""
    start = datetime.combine(day, time.min, tzinfo=UTC)
    return start, start + timedelta(days=1)


def _ref_domain(referrer: str) -> str | None:
    """Ключ src_ref = хост referrer-а lower-case; без хоста (не должен встречаться) — пропуск."""
    return (urlsplit(referrer).hostname or "").lower() or None


async def rollup(day: date, *, database_url: str | None = None) -> int:
    """Пересчитать агрегаты §A.2 одного UTC-дня в site_stats_daily. Возвращает число строк.

    Идемпотентность — upsert дня ЦЕЛИКОМ: в одной транзакции старые строки дня удаляются и
    пишется свежий пересчёт. Это сильнее построчного ON CONFLICT: ключ, исчезнувший из raw
    (например, оператор вручную вычистил спам-события), не остаётся висеть в агрегатах.
    Маркер _rolled пишется той же транзакцией — упавший на середине rollup не «закроет» день
    для retention. Пустой день закрывается одним маркером, иначе retention ждал бы его вечно.

    Боты (is_bot=1) исключаются из views/uniques/src_* прямо по контракту §A.2; leads/subs
    считаются как есть — конверсия дороже ложного бота, а эвристика UA меняется задним числом.
    В отличие от record_event НЕ глотает ошибки: батч-джоб, изоляцией владеет nightly-обвязка.
    """
    start, end = _day_bounds(day)
    in_day = (SiteEvent.ts >= start, SiteEvent.ts < end)
    human = SiteEvent.is_bot.is_(False)
    pv = SiteEvent.type == "pageview"

    async with writer_session(database_url=database_url) as s:
        rows: list[tuple[str, str, int]] = []

        res = await s.execute(
            select(SiteEvent.path, func.count()).where(*in_day, pv, human).group_by(SiteEvent.path)
        )
        rows += [("views_path", path, n) for path, n in res]

        # key хранится строкой (Text в составном PK) — смешение типов ломало бы точечный lookup.
        res = await s.execute(
            select(SiteEvent.apt_id, func.count())
            .where(*in_day, pv, human, SiteEvent.apt_id.is_not(None))
            .group_by(SiteEvent.apt_id)
        )
        rows += [("views_apt", str(apt), n) for apt, n in res]

        n_uniq = (
            await s.execute(
                select(func.count(func.distinct(SiteEvent.visitor))).where(*in_day, human)
            )
        ).scalar_one()
        if n_uniq:
            rows.append(("uniques", "", n_uniq))

        for metric, etype in (("leads", "lead_submit"), ("subs", "subscribe_submit")):
            n = (
                await s.execute(select(func.count()).where(*in_day, SiteEvent.type == etype))
            ).scalar_one()
            if n:
                rows.append((metric, "", n))

        res = await s.execute(
            select(SiteEvent.apt_id, func.count())
            .where(*in_day, SiteEvent.type == "lead_submit", SiteEvent.apt_id.is_not(None))
            .group_by(SiteEvent.apt_id)
        )
        rows += [("leads_apt", str(apt), n) for apt, n in res]

        res = await s.execute(
            select(SiteEvent.utm_source, func.count())
            .where(*in_day, pv, human, SiteEvent.utm_source.is_not(None))
            .group_by(SiteEvent.utm_source)
        )
        rows += [("src_utm", src, n) for src, n in res]

        # Домен referrer-а SQLite не распарсит — доагрегируем в Python; кардинальность за
        # день мала (referrer уже отфильтрован до «внешних с хостом» при записи).
        res = await s.execute(
            select(SiteEvent.referrer, func.count())
            .where(*in_day, pv, human, SiteEvent.referrer.is_not(None))
            .group_by(SiteEvent.referrer)
        )
        by_domain: dict[str, int] = {}
        for ref, n in res:
            dom = _ref_domain(ref)
            if dom:
                by_domain[dom] = by_domain.get(dom, 0) + n
        rows += [("src_ref", dom, n) for dom, n in sorted(by_domain.items())]

        rows.append((_ROLLED, "", 1))

        await s.execute(delete(SiteStatsDaily).where(SiteStatsDaily.day == day))
        s.add_all(SiteStatsDaily(day=day, metric=m, key=k, value=v) for m, k, v in rows)
        return len(rows)


async def retention(*, now: datetime | None = None, database_url: str | None = None) -> int:
    """Удалить raw site_events старше SITE_ANALYTICS_RAW_RETENTION_DAYS. Возвращает число строк.

    Гард от потери данных: удаляются ТОЛЬКО дни с маркером _rolled — день, который nightly
    так и не сагрегировал (rollup падал, деплой стоял), лежит в raw сколько угодно и будет
    подобран будущим rollup-ом. Граница строгая: день ровно N-дневной давности ещё живёт.
    `now` инжектится тестами; прод берёт текущий момент UTC.
    """
    keep_days = get_settings().site_analytics_raw_retention_days
    cutoff = (now or datetime.now(UTC)).date() - timedelta(days=keep_days)

    async with writer_session(database_url=database_url) as s:
        rolled_days = (
            await s.execute(
                select(SiteStatsDaily.day)
                .where(SiteStatsDaily.metric == _ROLLED, SiteStatsDaily.day < cutoff)
                .order_by(SiteStatsDaily.day)
            )
        ).scalars().all()
        deleted = 0
        for day in rolled_days:
            start, end = _day_bounds(day)
            # cast: execute() типизирован как Result, но DML всегда возвращает CursorResult.
            res = cast(
                "CursorResult[Any]",
                await s.execute(delete(SiteEvent).where(SiteEvent.ts >= start, SiteEvent.ts < end)),
            )
            deleted += res.rowcount or 0
        return deleted
