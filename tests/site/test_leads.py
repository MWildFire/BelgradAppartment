"""Lead-writer (Stage W, W2-B1): validation, honeypot, narrow-writer insert, non-fatal notice.

Contract: DATA_CONTRACT.md §6. The notifier is mocked everywhere — a real Telegram send
must never run in tests, and a failing send must never propagate to the visitor.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from src.db.models import Lead
from src.site import db as sitedb
from src.site.leads import LeadPayload, LeadValidationError, create_lead


async def _fetch_leads(url: str) -> list[Lead]:
    async with sitedb.read_session(database_url=url) as s:
        return list((await s.execute(select(Lead).order_by(Lead.id))).scalars().all())


@pytest.mark.asyncio
async def test_valid_payload_writes_row_and_notifies(site_db: str) -> None:
    payload = LeadPayload(
        name="Иван",
        contact="@ivan",
        budget_eur=700,
        districts="Врачар",
        rooms="2",
        message="Ищу с балконом",
    )
    with patch("src.site.leads.send_text_to_recipients", new=AsyncMock()) as mock_send:
        lead = await create_lead(payload, database_url=site_db)

    assert lead is not None
    assert lead.id is not None

    rows = await _fetch_leads(site_db)
    assert len(rows) == 1
    saved = rows[0]
    assert saved.name == "Иван"
    assert saved.contact == "@ivan"
    assert saved.budget_eur == 700
    assert saved.districts == "Врачар"
    assert saved.rooms == "2"
    assert saved.message == "Ищу с балконом"
    assert saved.source == "site"
    assert saved.status == "new"

    mock_send.assert_awaited_once()
    # First positional arg is the client (None in bot mode); the text is the second.
    call_args = mock_send.await_args
    text = call_args.args[1] if len(call_args.args) > 1 else call_args.kwargs["text"]
    assert "Иван" in text
    assert "@ivan" in text
    assert "700" in text
    assert "Врачар" in text


@pytest.mark.asyncio
async def test_honeypot_filled_is_silent_noop(site_db: str) -> None:
    payload = LeadPayload(name="Bot", contact="spam@x", company="ACME Corp")
    with patch("src.site.leads.send_text_to_recipients", new=AsyncMock()) as mock_send:
        result = await create_lead(payload, database_url=site_db)

    assert result is None
    assert await _fetch_leads(site_db) == []
    mock_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_name_raises(site_db: str) -> None:
    payload = LeadPayload(name="", contact="@ivan")
    with patch("src.site.leads.send_text_to_recipients", new=AsyncMock()) as mock_send:
        with pytest.raises(LeadValidationError):
            await create_lead(payload, database_url=site_db)
    assert await _fetch_leads(site_db) == []
    mock_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_blank_name_raises(site_db: str) -> None:
    payload = LeadPayload(name="   ", contact="@ivan")
    with patch("src.site.leads.send_text_to_recipients", new=AsyncMock()):
        with pytest.raises(LeadValidationError):
            await create_lead(payload, database_url=site_db)
    assert await _fetch_leads(site_db) == []


@pytest.mark.asyncio
async def test_missing_contact_raises(site_db: str) -> None:
    payload = LeadPayload(name="Иван", contact="")
    with patch("src.site.leads.send_text_to_recipients", new=AsyncMock()):
        with pytest.raises(LeadValidationError):
            await create_lead(payload, database_url=site_db)
    assert await _fetch_leads(site_db) == []


@pytest.mark.asyncio
async def test_negative_budget_raises(site_db: str) -> None:
    payload = LeadPayload(name="Иван", contact="@ivan", budget_eur=-1)
    with patch("src.site.leads.send_text_to_recipients", new=AsyncMock()):
        with pytest.raises(LeadValidationError):
            await create_lead(payload, database_url=site_db)
    assert await _fetch_leads(site_db) == []


@pytest.mark.asyncio
async def test_overlong_message_raises(site_db: str) -> None:
    payload = LeadPayload(name="Иван", contact="@ivan", message="x" * 2001)
    with patch("src.site.leads.send_text_to_recipients", new=AsyncMock()):
        with pytest.raises(LeadValidationError):
            await create_lead(payload, database_url=site_db)
    assert await _fetch_leads(site_db) == []


@pytest.mark.asyncio
async def test_overlong_districts_raises(site_db: str) -> None:
    # districts/rooms — свободный текст формы; без лимита рукописный POST пишет в leads
    # блоб произвольного размера (Wave-5 #5). Границы у писателя, как у message (≤2000 → 400).
    payload = LeadPayload(name="Иван", contact="@ivan", districts="Врачар, " * 100)
    with patch("src.site.leads.send_text_to_recipients", new=AsyncMock()):
        with pytest.raises(LeadValidationError):
            await create_lead(payload, database_url=site_db)
    assert await _fetch_leads(site_db) == []


@pytest.mark.asyncio
async def test_overlong_rooms_raises(site_db: str) -> None:
    payload = LeadPayload(name="Иван", contact="@ivan", rooms="x" * 101)
    with patch("src.site.leads.send_text_to_recipients", new=AsyncMock()):
        with pytest.raises(LeadValidationError):
            await create_lead(payload, database_url=site_db)
    assert await _fetch_leads(site_db) == []


@pytest.mark.asyncio
async def test_districts_and_rooms_at_limit_are_saved(site_db: str) -> None:
    # Границы включительные: ровно 500/100 символов — валидная заявка (без off-by-one).
    payload = LeadPayload(name="Иван", contact="@ivan", districts="д" * 500, rooms="к" * 100)
    with patch("src.site.leads.send_text_to_recipients", new=AsyncMock()):
        lead = await create_lead(payload, database_url=site_db)
    assert lead is not None
    rows = await _fetch_leads(site_db)
    assert len(rows) == 1
    assert rows[0].districts == "д" * 500
    assert rows[0].rooms == "к" * 100


@pytest.mark.asyncio
async def test_notifier_failure_does_not_propagate_lead_still_saved(site_db: str) -> None:
    payload = LeadPayload(name="Иван", contact="@ivan", budget_eur=700)
    boom = AsyncMock(side_effect=RuntimeError("telegram down"))
    with patch("src.site.leads.send_text_to_recipients", new=boom):
        lead = await create_lead(payload, database_url=site_db)

    # Notifier raised, but the lead must still be returned and persisted.
    assert lead is not None
    boom.assert_awaited_once()
    rows = await _fetch_leads(site_db)
    assert len(rows) == 1
    assert rows[0].name == "Иван"


@pytest.mark.asyncio
async def test_missing_budget_and_districts_format_gracefully(site_db: str) -> None:
    payload = LeadPayload(name="Иван", contact="@ivan")
    with patch("src.site.leads.send_text_to_recipients", new=AsyncMock()) as mock_send:
        lead = await create_lead(payload, database_url=site_db)

    assert lead is not None
    mock_send.assert_awaited_once()
    call_args = mock_send.await_args
    text = call_args.args[1] if len(call_args.args) > 1 else call_args.kwargs["text"]
    # No crash on None budget/districts; the name+contact must still be present.
    assert "Иван" in text
    assert "@ivan" in text
