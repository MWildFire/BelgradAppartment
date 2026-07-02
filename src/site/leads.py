"""Lead-writer for the public catalog's request form (Stage W, W2-B1).

Pure write-path: validate (DATA_CONTRACT §6), drop honeypot bots silently, insert via the
narrow writer engine (busy_timeout + retry on "database is locked"), then notify Дарья.
The notice is non-fatal — a failed Telegram send must never 500 the visitor: the lead is
already committed by then. CSRF / rate-limit live in the Wave 3 route, not here.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from urllib.parse import urlsplit

from sqlalchemy.exc import OperationalError

from src.db.models import Lead
from src.site.db import writer_session
from src.site.email import send_lead_email
from src.site.readmodel import apartment_summary
from src.telegram.delivery import send_text_to_recipients

logger = logging.getLogger(__name__)

NAME_MAX = 120
CONTACT_MAX = 200
MESSAGE_MAX = 2000

# T2-3 (§A.1): границы полей атрибуции — те же, что у site_events (единый контракт этапа T).
UTM_MAX = 100
REFERRER_MAX = 200
_UTM_STRIP_RE = re.compile(r"[^A-Za-z0-9._-]+")

# busy_timeout (5000ms) already waits out the matcher's writes; these retries cover the rare
# case where the lock outlives the timeout. Small + bounded — this is a visitor-facing path.
_WRITE_RETRIES = 3
_WRITE_RETRY_DELAY = 0.2


class LeadValidationError(ValueError):
    """Required field missing/blank or an optional field out of range. Route maps it to 400."""


@dataclass(frozen=True)
class LeadPayload:
    """Form fields from POST /lead (DATA_CONTRACT §6). `company` is the anti-bot honeypot."""

    name: str
    contact: str
    budget_eur: int | None = None
    districts: str | None = None
    rooms: str | None = None
    message: str | None = None
    company: str | None = None  # honeypot: real users never see/fill it
    # Optional id of the apartment this lead is about. Public-only: a private/missing id is
    # validated away in create_lead (apartment_summary returns None) and never persisted.
    site_listing_id: int | None = None
    kind: str = "general"  # general | listing — set to 'listing' once the apartment resolves
    # T2-3 attribution (§A.4): first-touch снимок из hidden-полей utm.js (или no-JS фоллбэка
    # роута). Значения могут быть «сырыми» — писатель добивает их до §A.1 (мусор → NULL).
    utm_source: str | None = None
    utm_medium: str | None = None
    utm_campaign: str | None = None
    referrer: str | None = None


def clean_utm(value: str | None) -> str | None:
    """Санитайз utm_* по §A.1: только [A-Za-z0-9._-], обрезка ≤100; пусто/мусор → NULL.

    Best-effort по контракту: атрибуция никогда не превращается в 400 — грязное значение
    молча деградирует, заявка живёт.
    """
    if not value:
        return None
    cleaned = _UTM_STRIP_RE.sub("", value)[:UTM_MAX]
    return cleaned or None


def clean_referrer(value: str | None, *, own_hosts: Iterable[str] = ()) -> str | None:
    """Referrer по §A.1/§A.4: только внешний абсолютный URL, обрезка ≤200; иначе NULL.

    own_hosts передаёт роут (хост запроса + публичный base_url) — внутренние переходы это
    навигация по сайту, не источник трафика. Писатель зовёт без own_hosts: хост-политика —
    забота роута, здесь остаются длина и «URL вообще». Кривой URL глотаем (best-effort).
    """
    if not value:
        return None
    value = value.strip()
    try:
        host = (urlsplit(value).hostname or "").lower()
    except ValueError:  # напр. 'http://[broken' — мусор от рукописного POST
        return None
    if not host:
        return None
    if host in {h.lower() for h in own_hosts if h}:
        return None
    return value[:REFERRER_MAX]


def _validate(payload: LeadPayload) -> None:
    name = (payload.name or "").strip()
    if not (1 <= len(name) <= NAME_MAX):
        raise LeadValidationError("name must be 1..120 non-blank characters")

    contact = (payload.contact or "").strip()
    if not (1 <= len(contact) <= CONTACT_MAX):
        raise LeadValidationError("contact must be 1..200 non-blank characters")

    if payload.budget_eur is not None and payload.budget_eur < 0:
        raise LeadValidationError("budget_eur must be >= 0")

    if payload.message is not None and len(payload.message) > MESSAGE_MAX:
        raise LeadValidationError("message must be <= 2000 characters")


def _format_notice(payload: LeadPayload, apartment: dict[str, object] | None = None) -> str:
    budget = f"{payload.budget_eur}€" if payload.budget_eur is not None else "—"
    districts = (payload.districts or "").strip() or "—"
    text = (
        f"📩 Новая заявка с сайта: {payload.name.strip()}, "
        f"бюджет {budget}, районы {districts}, контакт {payload.contact.strip()}"
    )
    # Public apartment summary (from W3 onward) — append a compact «Квартира» line. The url is
    # the site's own /apt/{id}, never a площадка link, so the notice never leaks a source.
    if apartment:
        price = apartment.get("price_eur")
        a_district = apartment.get("district") or "—"
        url = apartment.get("url") or "—"
        price_str = f"{price}€" if price is not None else "—"
        text += f"\nКвартира: {price_str}, {a_district}, {url}"
    return text


async def _insert_lead(payload: LeadPayload, database_url: str | None) -> Lead:
    """Insert one Lead through the narrow writer, retrying on a transient sqlite lock."""
    last_err: OperationalError | None = None
    for attempt in range(_WRITE_RETRIES):
        try:
            async with writer_session(database_url=database_url) as s:
                lead = Lead(
                    name=payload.name.strip(),
                    contact=payload.contact.strip(),
                    budget_eur=payload.budget_eur,
                    districts=payload.districts,
                    rooms=payload.rooms,
                    message=payload.message,
                    site_listing_id=payload.site_listing_id,
                    kind=payload.kind,
                    source="site",
                    status="new",
                    # T2-3: последний рубеж §A.1 — длины/charset добиваются у писателя, чтобы
                    # НИКАКОЙ вызов create_lead не мог записать в БД сырое значение.
                    utm_source=clean_utm(payload.utm_source),
                    utm_medium=clean_utm(payload.utm_medium),
                    utm_campaign=clean_utm(payload.utm_campaign),
                    referrer=clean_referrer(payload.referrer),
                )
                s.add(lead)
                await s.flush()
                await s.refresh(lead)
                return lead
        except OperationalError as e:
            if "database is locked" not in str(e).lower():
                raise
            last_err = e
            logger.warning("lead insert hit a locked DB (attempt %d) — retrying", attempt + 1)
            await asyncio.sleep(_WRITE_RETRY_DELAY)
    assert last_err is not None  # loop only exits via return or a raised non-lock error
    raise last_err


async def create_lead(payload: LeadPayload, *, database_url: str | None = None) -> Lead | None:
    """Persist a lead and notify Дарья.

    Returns the saved Lead, or None when the honeypot is tripped (silent no-op — no DB write,
    no notice). Raises LeadValidationError on bad input. The notice is best-effort: if it
    raises, the already-committed lead is still returned.
    """
    # Honeypot first — a bot's spam should leave no trace and trigger no work.
    if payload.company and payload.company.strip():
        logger.info("lead honeypot tripped — dropping silently")
        return None

    _validate(payload)

    # Resolve the apartment, if one was named. apartment_summary is public-only, so a private
    # or missing id yields None → we DEGRADE to a general lead: drop the id and keep kind
    # 'general'. A private/non-existent apartment is never persisted, e-mailed, or notified.
    apartment: dict[str, object] | None = None
    if payload.site_listing_id is not None:
        apartment = await apartment_summary(payload.site_listing_id, database_url=database_url)
    if apartment is not None:
        payload = replace(payload, kind="listing")
    else:
        payload = replace(payload, site_listing_id=None, kind="general")

    lead = await _insert_lead(payload, database_url)

    # Deliver to both channels, e-mail FIRST then the bot notice. The lead is already committed,
    # so each channel is best-effort: a failure is logged (no secrets) and never propagates —
    # the visitor always gets success, and one channel failing never blocks the other.
    try:
        await send_lead_email(payload, apartment=apartment)
    except Exception:  # e-mail is non-fatal: lead is already persisted
        logger.exception("lead e-mail failed to send — lead %s still saved", lead.id)

    try:
        await send_text_to_recipients(None, _format_notice(payload, apartment))
    except Exception:  # notice is non-fatal: lead is already persisted
        logger.exception("lead notice failed to send — lead %s still saved", lead.id)

    return lead
