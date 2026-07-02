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

Rollup/retention (nightly, §A.2) добавляет T2-4 — сюда же, отдельными функциями.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import UTC, datetime
from urllib.parse import urlsplit

from starlette.requests import Request

from src.clientip import client_ip
from src.config import get_settings
from src.db.models import SiteEvent
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
