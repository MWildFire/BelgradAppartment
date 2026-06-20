"""Lead-writer for the public catalog's request form (Stage W, W2-B1).

Pure write-path: validate (DATA_CONTRACT §6), drop honeypot bots silently, insert via the
narrow writer engine (busy_timeout + retry on "database is locked"), then notify Дарья.
The notice is non-fatal — a failed Telegram send must never 500 the visitor: the lead is
already committed by then. CSRF / rate-limit live in the Wave 3 route, not here.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from sqlalchemy.exc import OperationalError

from src.db.models import Lead
from src.site.db import writer_session
from src.telegram.delivery import send_text_to_recipients

logger = logging.getLogger(__name__)

NAME_MAX = 120
CONTACT_MAX = 200
MESSAGE_MAX = 2000

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


def _format_notice(payload: LeadPayload) -> str:
    budget = f"{payload.budget_eur}€" if payload.budget_eur is not None else "—"
    districts = (payload.districts or "").strip() or "—"
    return (
        f"📩 Новая заявка с сайта: {payload.name.strip()}, "
        f"бюджет {budget}, районы {districts}, контакт {payload.contact.strip()}"
    )


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
                    source="site",
                    status="new",
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
    lead = await _insert_lead(payload, database_url)

    try:
        await send_text_to_recipients(None, _format_notice(payload))
    except Exception:  # notice is non-fatal: lead is already persisted
        logger.exception("lead notice failed to send — lead %s still saved", lead.id)

    return lead
