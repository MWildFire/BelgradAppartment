"""C2 — subscription digest e-mail: no-op when SMTP empty, right payload when configured."""

from __future__ import annotations

import pytest

from src.site.email import send_subscription_digest
from src.site.readmodel import SiteListingView


def _view(vid: int, price: int, district: str) -> SiteListingView:
    return SiteListingView(
        id=vid, price_eur=price, area_m2=40.0, rooms="2-комн", floor=None, district=district,
        address=None, lat=None, lon=None, for_vnj=False, for_white_card=False, is_rented=False,
        photos=[], note=None, channel_url="https://t.me/RentBelgrad",
        posted_at="2026-06-20T12:00:00+00:00",
    )


@pytest.mark.asyncio
async def test_noop_when_smtp_unconfigured(monkeypatch) -> None:
    from src.config import get_settings

    monkeypatch.setattr(get_settings(), "site_smtp_host", "")
    called = {"n": 0}

    async def _fail(*a, **k):
        called["n"] += 1

    monkeypatch.setattr("aiosmtplib.send", _fail)
    # Should return without ever touching aiosmtplib.
    await send_subscription_digest("a@b.com", [_view(1, 600, "Врачар")], "https://x/unsub?token=t")
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_sends_with_right_payload_when_configured(monkeypatch) -> None:
    from src.config import get_settings

    monkeypatch.setattr(get_settings(), "site_smtp_host", "smtp.example.com")
    captured = {}

    async def _send(msg, **kwargs):
        captured["to"] = msg["To"]
        captured["subject"] = str(msg["Subject"])
        # multipart: read the text/plain part's content.
        captured["body"] = msg.get_body(preferencelist=("plain",)).get_content()
        captured["kwargs"] = kwargs

    monkeypatch.setattr("aiosmtplib.send", _send)
    listings = [_view(1, 600, "Врачар"), _view(2, 800, "Земун")]
    await send_subscription_digest("a@b.com", listings, "https://x/unsub?token=tok")

    assert captured["to"] == "a@b.com"
    body = captured["body"]
    # Both listings + the unsubscribe link appear in the digest body.
    assert "Врачар" in body
    assert "Земун" in body
    assert "https://x/unsub?token=tok" in body
    assert captured["kwargs"]["hostname"] == "smtp.example.com"


@pytest.mark.asyncio
async def test_empty_listings_is_noop(monkeypatch) -> None:
    from src.config import get_settings

    monkeypatch.setattr(get_settings(), "site_smtp_host", "smtp.example.com")
    called = {"n": 0}

    async def _send(*a, **k):
        called["n"] += 1

    monkeypatch.setattr("aiosmtplib.send", _send)
    await send_subscription_digest("a@b.com", [], "https://x/unsub")
    assert called["n"] == 0  # nothing to digest → no send
