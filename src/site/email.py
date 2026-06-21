"""Lead e-mail delivery for the public catalog (Stage W2, F4).

Sends each site lead to Дарья (site_lead_email_to) over an SMTP relay, in addition to the
Telegram bot notice. Relay-agnostic: host/port/creds come ONLY from Settings (server .env) —
never hardcoded, never logged. When site_smtp_host is empty (dev/test, or simply not yet
configured) this is a no-op, so the lead path stays green without a live relay.
"""

from __future__ import annotations

import logging
from email.message import EmailMessage
from typing import TYPE_CHECKING, Any

import aiosmtplib

from src.config import get_settings

if TYPE_CHECKING:
    from src.site.leads import LeadPayload

logger = logging.getLogger(__name__)


def _build_message(payload: LeadPayload, apartment: dict[str, Any] | None) -> EmailMessage:
    """Compose the lead e-mail: plain-text primary + a simple HTML alternative, UTF-8."""
    settings = get_settings()
    name = payload.name.strip()

    budget = f"{payload.budget_eur}€" if payload.budget_eur is not None else "—"
    districts = (payload.districts or "").strip() or "—"
    rooms = (payload.rooms or "").strip() or "—"
    contact = payload.contact.strip()
    message = (payload.message or "").strip() or "—"

    lines = [
        "Новая заявка с сайта RentBelgrad:",
        "",
        f"Имя: {name}",
        f"Контакт: {contact}",
        f"Бюджет: {budget}",
        f"Районы: {districts}",
        f"Комнаты: {rooms}",
        f"Сообщение: {message}",
    ]
    # apartment is a dict summary (used from W3 onward) — append a compact listing block.
    if apartment:
        price = apartment.get("price")
        a_district = apartment.get("district") or "—"
        url = apartment.get("url") or "—"
        price_str = f"{price}€" if price is not None else "—"
        lines += ["", f"Квартира: {price_str}, {a_district}, {url}"]

    text = "\n".join(lines)
    html_rows = "".join(f"<p>{line}</p>" for line in lines if line)

    msg = EmailMessage()
    msg["From"] = settings.site_smtp_from
    msg["To"] = settings.site_lead_email_to
    msg["Subject"] = f"Новая заявка с сайта RentBelgrad — {name}"
    msg.set_content(text, charset="utf-8")
    msg.add_alternative(f"<html><body>{html_rows}</body></html>", subtype="html", charset="utf-8")
    return msg


async def send_lead_email(payload: LeadPayload, *, apartment: dict[str, Any] | None = None) -> None:
    """E-mail a lead to Дарья via SMTP. No-op when SMTP is unconfigured.

    Creds are read from Settings only and are never logged. Raises on a real send failure so the
    caller (create_lead) can log it non-fatally — the lead is already persisted by then.
    """
    settings = get_settings()
    if not settings.site_smtp_host:
        # Not configured (dev/test or pre-rollout) — skip silently rather than fail the lead.
        logger.info("SMTP not configured, skipping lead e-mail")
        return

    msg = _build_message(payload, apartment)
    # Bound the visitor-facing path: the lead is already committed, so a hung relay must not
    # block the POST /lead response for aiosmtplib's 60s default — fail fast, caller logs it.
    await aiosmtplib.send(
        msg,
        hostname=settings.site_smtp_host,
        port=settings.site_smtp_port,
        username=settings.site_smtp_user or None,
        password=settings.site_smtp_password or None,
        start_tls=settings.site_smtp_starttls,
        timeout=15,
    )
