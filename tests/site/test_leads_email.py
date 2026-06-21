"""Lead e-mail delivery (Stage W2, F4): create_lead e-mails Дарья in addition to the bot notice.

The SMTP send is always mocked — no real e-mail must ever leave a test. The bot notifier is
mocked too (as in test_leads.py). Both channels are best-effort: a failing channel must never
propagate to the visitor, and the lead must always be saved first.
"""

from __future__ import annotations

from email.message import EmailMessage
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from src.config import get_settings
from src.db.models import Lead
from src.site import db as sitedb
from src.site.leads import LeadPayload, create_lead


async def _fetch_leads(url: str) -> list[Lead]:
    async with sitedb.read_session(database_url=url) as s:
        return list((await s.execute(select(Lead).order_by(Lead.id))).scalars().all())


@pytest.fixture
def smtp_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bypass the no-op branch: give send_lead_email a non-empty SMTP host."""
    settings = get_settings()
    monkeypatch.setattr(settings, "site_smtp_host", "smtp.example.com")
    monkeypatch.setattr(settings, "site_smtp_port", 587)
    monkeypatch.setattr(settings, "site_smtp_user", "relay-user")
    monkeypatch.setattr(settings, "site_smtp_password", "relay-secret")
    monkeypatch.setattr(settings, "site_smtp_from", "no-reply@rentbelgrad.com")
    monkeypatch.setattr(settings, "site_lead_email_to", "d.mashukova@bk.ru")
    monkeypatch.setattr(settings, "site_smtp_starttls", True)


@pytest.mark.asyncio
async def test_valid_lead_emails_and_notifies(site_db: str, smtp_configured: None) -> None:
    payload = LeadPayload(
        name="Иван",
        contact="@ivan",
        budget_eur=700,
        districts="Врачар",
        rooms="2",
        message="Ищу с балконом",
    )
    smtp_send = AsyncMock()
    with (
        patch("src.site.email.aiosmtplib.send", new=smtp_send),
        patch("src.site.leads.send_text_to_recipients", new=AsyncMock()) as mock_bot,
    ):
        lead = await create_lead(payload, database_url=site_db)

    assert lead is not None

    # E-mail was sent exactly once with the right recipient/subject/body.
    smtp_send.assert_awaited_once()
    msg = smtp_send.await_args.args[0]
    assert isinstance(msg, EmailMessage)
    assert msg["To"] == "d.mashukova@bk.ru"
    assert "Иван" in msg["Subject"]
    body = msg.get_content() if not msg.is_multipart() else msg.get_body(("plain",)).get_content()
    for field in ("Иван", "@ivan", "700", "Врачар", "2", "Ищу с балконом"):
        assert field in body, f"missing {field!r} in e-mail body"

    # The bot notice fired too, and a Lead row exists.
    mock_bot.assert_awaited_once()
    rows = await _fetch_leads(site_db)
    assert len(rows) == 1
    assert rows[0].name == "Иван"


@pytest.mark.asyncio
async def test_smtp_failure_does_not_propagate_lead_still_saved(
    site_db: str, smtp_configured: None
) -> None:
    payload = LeadPayload(name="Иван", contact="@ivan", budget_eur=700)
    boom = AsyncMock(side_effect=RuntimeError("smtp down"))
    with (
        patch("src.site.email.aiosmtplib.send", new=boom),
        patch("src.site.leads.send_text_to_recipients", new=AsyncMock()) as mock_bot,
    ):
        lead = await create_lead(payload, database_url=site_db)

    # SMTP raised, but the lead is still returned + persisted, and the bot notice still fires.
    assert lead is not None
    boom.assert_awaited()
    mock_bot.assert_awaited_once()
    rows = await _fetch_leads(site_db)
    assert len(rows) == 1
    assert rows[0].name == "Иван"


@pytest.mark.asyncio
async def test_honeypot_no_email_no_notice(site_db: str, smtp_configured: None) -> None:
    payload = LeadPayload(name="Bot", contact="spam@x", company="ACME Corp")
    smtp_send = AsyncMock()
    with (
        patch("src.site.email.aiosmtplib.send", new=smtp_send),
        patch("src.site.leads.send_text_to_recipients", new=AsyncMock()) as mock_bot,
    ):
        result = await create_lead(payload, database_url=site_db)

    assert result is None
    assert await _fetch_leads(site_db) == []
    smtp_send.assert_not_awaited()
    mock_bot.assert_not_awaited()


@pytest.mark.asyncio
async def test_smtp_not_configured_is_noop_lead_and_notice_still_happen(
    site_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Empty host -> send_lead_email must short-circuit and never call aiosmtplib.send.
    monkeypatch.setattr(get_settings(), "site_smtp_host", "")
    payload = LeadPayload(name="Иван", contact="@ivan", budget_eur=700)
    smtp_send = AsyncMock()
    with (
        patch("src.site.email.aiosmtplib.send", new=smtp_send),
        patch("src.site.leads.send_text_to_recipients", new=AsyncMock()) as mock_bot,
    ):
        lead = await create_lead(payload, database_url=site_db)

    assert lead is not None
    smtp_send.assert_not_awaited()  # no-op branch taken
    mock_bot.assert_awaited_once()  # existing behavior preserved
    rows = await _fetch_leads(site_db)
    assert len(rows) == 1
