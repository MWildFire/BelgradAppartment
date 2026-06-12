"""W3-G3: price-drop alerts off the W2-C2 PriceChange contract (nightly-refresh hook)."""

from __future__ import annotations

import src.db.session as db_session
from src.cli import PRICE_DROP_MAX_ALERTS, _build_price_drop_alerts, _select_price_drops
from src.config import get_settings
from src.db.models import InboundPost, Listing, Match, MatchStatus, Platform, SourceChannel
from src.scrapers.orchestrator import PriceChange

# ------------------------------------------------------------------ pure drop filter


def test_select_price_drops_filters_and_threshold_boundary():
    changes = [
        PriceChange(1, None, 400),   # old unknown → skip
        PriceChange(2, 500, None),   # new unknown → skip
        PriceChange(3, 400, 450),    # price rise → skip
        PriceChange(4, 400, 400),    # unchanged → skip
        PriceChange(5, 1000, 950),   # exactly 5.0% → alert (boundary inclusive)
        PriceChange(6, 1000, 951),   # 4.9% → below threshold
        PriceChange(7, 0, -10),      # garbage old=0 → skip, no ZeroDivisionError
        PriceChange(8, 500, 400),    # 20% → alert
        PriceChange(9, 500, 0),      # new=0 scrape glitch → skip, no «→ 0€ (−100%)»
    ]

    drops = _select_price_drops(changes, min_pct=5.0)

    assert [(c.listing_id, round(pct, 1)) for c, pct in drops] == [(5, 5.0), (8, 20.0)]


# ------------------------------------------------------------------ DB-backed alert builder


async def _seed(
    ext: str,
    *,
    price: int,
    match_status: MatchStatus | None,
    channel_username: str | None = "srcchan",
    tg_message_id: int = 777,
) -> int:
    """Listing (+ optional inbound/match chain) → listing id."""
    async with db_session.get_session() as s:
        listing = Listing(
            platform=Platform.NEKRETNINE,
            external_id=ext,
            url=f"https://example.com/NEKRETNINE/{ext}",
            price_eur=price,
        )
        s.add(listing)
        await s.flush()
        if match_status is not None:
            chan = SourceChannel(tg_id=-100_000 - listing.id, username=channel_username)
            s.add(chan)
            await s.flush()
            inbound = InboundPost(source_channel_id=chan.id, tg_message_id=tg_message_id)
            s.add(inbound)
            await s.flush()
            s.add(
                Match(
                    inbound_post_id=inbound.id,
                    listing_id=listing.id,
                    status=match_status,
                )
            )
        return listing.id


async def test_alerts_only_for_pending_or_published_matches(db, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "price_drop_alerts", True)
    monkeypatch.setattr(settings, "price_drop_min_pct", 5.0)

    pending = await _seed("pend", price=450, match_status=MatchStatus.PENDING)
    published = await _seed(
        "publ", price=500, match_status=MatchStatus.PUBLISHED,
        channel_username=None, tg_message_id=888,
    )
    rejected = await _seed("rej", price=400, match_status=MatchStatus.REJECTED)
    unmatched = await _seed("none", price=400, match_status=None)

    changes = [
        PriceChange(pending, 500, 450),    # -10% + PENDING → alert
        PriceChange(published, 600, 500),  # -17% + PUBLISHED → alert
        PriceChange(rejected, 500, 400),   # -20% but REJECTED-only → silent
        PriceChange(unmatched, 500, 400),  # -20% but no match at all → silent
    ]

    alerts, summary = await _build_price_drop_alerts(changes)

    assert len(alerts) == 2
    assert summary == "📉 Алертов о снижении цены: 2"

    # biggest drop first (−17% published before −10% pending)
    assert alerts[0].startswith("📉 Цена снизилась: 600€ → 500€ (−17%)")
    # channel without username → inbound id reference instead of a t.me link
    assert "📥 источник: inbound #" in alerts[0]
    assert "t.me" not in alerts[0]

    assert alerts[1].startswith("📉 Цена снизилась: 500€ → 450€ (−10%)")
    assert "https://example.com/NEKRETNINE/pend" in alerts[1]
    assert "📥 источник: https://t.me/srcchan/777" in alerts[1]  # public channel → t.me link


async def test_flag_off_means_no_sends_and_no_summary_line(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "price_drop_alerts", False)

    alerts, summary = await _build_price_drop_alerts([PriceChange(1, 500, 100)])

    assert alerts == []
    assert summary is None


async def test_enabled_but_no_qualifying_drops_reports_zero(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "price_drop_alerts", True)
    monkeypatch.setattr(settings, "price_drop_min_pct", 5.0)

    # No DB needed: nothing passes the pure filter, so no session is opened.
    alerts, summary = await _build_price_drop_alerts(
        [PriceChange(1, 500, 510), PriceChange(2, None, 300)]
    )

    assert alerts == []
    assert summary == "📉 Алертов о снижении цены: 0"


async def test_drop_below_threshold_is_silent(db, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "price_drop_alerts", True)
    monkeypatch.setattr(settings, "price_drop_min_pct", 5.0)
    lid = await _seed("tiny", price=990, match_status=MatchStatus.PENDING)

    alerts, summary = await _build_price_drop_alerts([PriceChange(lid, 1000, 990)])  # -1%

    assert alerts == []
    assert summary == "📉 Алертов о снижении цены: 0"


# ------------------------------------------------------------------ standalone-send cap


async def test_cap_orders_by_pct_and_appends_overflow_line(db, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "price_drop_alerts", True)
    monkeypatch.setattr(settings, "price_drop_min_pct", 5.0)

    # 12 eligible drops, seeded in ASCENDING pct order (6%..17%) to prove the sort
    changes = []
    for i in range(12):
        pct = 6 + i
        new_price = 1000 - pct * 10
        lid = await _seed(f"cap{i}", price=new_price, match_status=MatchStatus.PENDING)
        changes.append(PriceChange(lid, 1000, new_price))

    alerts, summary = await _build_price_drop_alerts(changes)

    # top-10 standalone messages + 1 overflow line — never a 12-message burst
    assert len(alerts) == PRICE_DROP_MAX_ALERTS + 1
    assert alerts[-1] == "…и ещё 2 снижений (см. ночную сводку)"
    # biggest drops first; the smallest eligible ones (6%, 7%) are squeezed out
    assert alerts[0].startswith("📉 Цена снизилась: 1000€ → 830€ (−17%)")
    assert alerts[PRICE_DROP_MAX_ALERTS - 1].startswith("📉 Цена снизилась: 1000€ → 920€ (−8%)")
    # nightly summary line counts ALL drops, not just the ones sent standalone
    assert summary == "📉 Алертов о снижении цены: 12"


async def test_exactly_at_cap_sends_all_without_overflow_line(db, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "price_drop_alerts", True)
    monkeypatch.setattr(settings, "price_drop_min_pct", 5.0)

    changes = []
    for i in range(PRICE_DROP_MAX_ALERTS):
        pct = 6 + i
        new_price = 1000 - pct * 10
        lid = await _seed(f"atcap{i}", price=new_price, match_status=MatchStatus.PENDING)
        changes.append(PriceChange(lid, 1000, new_price))

    alerts, summary = await _build_price_drop_alerts(changes)

    assert len(alerts) == PRICE_DROP_MAX_ALERTS
    assert all(a.startswith("📉 Цена снизилась") for a in alerts)
    assert summary == f"📉 Алертов о снижении цены: {PRICE_DROP_MAX_ALERTS}"
