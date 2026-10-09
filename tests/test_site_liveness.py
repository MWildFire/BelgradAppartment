"""Tests for the liveness sweep + its config.

Liveness replaces the age-based 21-day retention notice (D5): a public card stays up while the
apartment is still listed on ≥1 platform (its A4 dup-group) and only comes down when confirmed gone
from ALL. This file is the successor to the old tests/test_retention.py.
"""

from __future__ import annotations

import itertools

import httpx
import pytest
from sqlalchemy import select, update

from src.config import Settings, get_settings
from src.db import Listing, Match, get_session
from src.db.models import (
    InboundPost,
    InboundStatus,
    MatchStatus,
    Platform,
    SiteListing,
    SourceChannel,
)
from src.site.liveness import (
    LivenessCandidate,
    LivenessReport,
    format_liveness_notice,
    send_liveness_notice,
    site_liveness_sweep,
)

_N = itertools.count(1)


# --------------------------------------------------------------------------- T3-1: config


def test_liveness_config_defaults() -> None:
    s = Settings()
    assert s.site_liveness_enabled is True
    assert s.site_liveness_auto_hide is False  # D1 — warn-first
    assert s.site_liveness_max_hides == 10  # D3
    assert s.nightly_dupes_write is True  # D2


def test_liveness_config_env_override(monkeypatch) -> None:
    monkeypatch.setenv("SITE_LIVENESS_ENABLED", "false")
    monkeypatch.setenv("SITE_LIVENESS_AUTO_HIDE", "true")
    monkeypatch.setenv("SITE_LIVENESS_MAX_HIDES", "3")
    monkeypatch.setenv("NIGHTLY_DUPES_WRITE", "false")
    s = Settings()
    assert s.site_liveness_enabled is False
    assert s.site_liveness_auto_hide is True
    assert s.site_liveness_max_hides == 3
    assert s.nightly_dupes_write is False


# ------------------------------------------------------------- T3-3: liveness sweep (matrix)
#
# DB — общий conftest-фикстур `db` (in-memory SQLite, подменяет глобальную session factory).
# HTTP — httpx.MockTransport, инжектируется в sweep через client=. Никакой сети.


async def _listing(
    *,
    is_active: bool,
    dup_group_id: int | None = None,
    url: str | None = None,
    price_eur: int | None = 300,
    district: str | None = "Vračar",
    address_raw: str | None = None,
    title: str | None = None,
    platform: Platform = Platform.NEKRETNINE,
) -> int:
    n = next(_N)
    async with get_session() as s:
        lst = Listing(
            platform=platform,
            external_id=f"ext{n}",
            url=url or f"https://nekretnine.rs/oglasi/{n}/",
            price_eur=price_eur,
            district=district,
            address_raw=address_raw,
            title=title,
            photo_urls=[],
            photo_phashes=[],
            is_active=is_active,
            dup_group_id=dup_group_id,
        )
        s.add(lst)
        await s.flush()
        return lst.id


async def _join_group(*listing_ids: int) -> int:
    """Tag every listing with the shared dup_group_id (= min member id), like dedup does."""
    gid = min(listing_ids)
    async with get_session() as s:
        await s.execute(
            update(Listing).where(Listing.id.in_(listing_ids)).values(dup_group_id=gid)
        )
    return gid


async def _card(listing_id: int, *, keep_on_site: bool = False, is_public: bool = True) -> int:
    """Attach a PUBLISHED match + (public) SiteListing to a listing; return the site_listing id."""
    n = next(_N)
    async with get_session() as s:
        ch = SourceChannel(tg_id=-n, username=f"c{n}", is_active=True)
        s.add(ch)
        await s.flush()
        ib = InboundPost(
            source_channel_id=ch.id, tg_message_id=n, photo_paths=[], photo_phashes=[],
            status=InboundStatus.MATCHED,
        )
        s.add(ib)
        await s.flush()
        m = Match(
            inbound_post_id=ib.id, listing_id=listing_id, confidence=0.9,
            matched_photo_count=1, method="phash", status=MatchStatus.PUBLISHED,
        )
        s.add(m)
        await s.flush()
        sl = SiteListing(match_id=m.id, is_public=is_public, keep_on_site=keep_on_site)
        s.add(sl)
        await s.flush()
        return sl.id


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)


def _by_path(mapping: dict[str, int], default: int = 200):
    """MockTransport handler: first path-substring match wins, else `default`."""
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        for needle, code in mapping.items():
            if needle in url:
                return httpx.Response(code)
        return httpx.Response(default)
    return handler


async def _is_public(sl_id: int) -> bool:
    async with get_session() as s:
        row = (await s.execute(select(SiteListing).where(SiteListing.id == sl_id))).scalar_one()
        return row.is_public


@pytest.mark.asyncio
async def test_active_group_member_keeps_card(db):
    """A neighbour still ACTIVE on another platform keeps the card up, even though the matched
    listing itself went inactive (and would HEAD 404)."""
    dead = await _listing(is_active=False, url="https://nekretnine.rs/gone/")
    alive = await _listing(is_active=True, url="https://4zida.rs/live/", platform=Platform.CETIRI_ZIDA)
    await _join_group(dead, alive)
    sl = await _card(dead)

    report = await site_liveness_sweep(client=_client(_by_path({"/gone/": 404})))

    assert report.kept_active == 1
    assert report.total_flagged == 0
    assert await _is_public(sl) is True


@pytest.mark.asyncio
async def test_all_dead_hides_when_autohide_on(db, monkeypatch):
    monkeypatch.setattr(get_settings(), "site_liveness_auto_hide", True)
    lst = await _listing(is_active=False, url="https://nekretnine.rs/gone/")
    sl = await _card(lst)

    report = await site_liveness_sweep(client=_client(_by_path({"/gone/": 404})))

    assert len(report.hidden) == 1
    assert report.hidden[0].site_listing_id == sl
    assert await _is_public(sl) is False


@pytest.mark.asyncio
async def test_all_dead_is_candidate_when_autohide_off(db):
    """Default warn-first (D1): all-dead → confirmed candidate, card NOT hidden."""
    lst = await _listing(is_active=False, url="https://nekretnine.rs/gone/")
    sl = await _card(lst)

    report = await site_liveness_sweep(client=_client(_by_path({"/gone/": 404})))

    assert len(report.candidates_confirmed) == 1
    assert report.candidates_confirmed[0].site_listing_id == sl
    assert len(report.hidden) == 0
    assert await _is_public(sl) is True


@pytest.mark.asyncio
async def test_inconclusive_keeps_card_even_with_autohide_on(db, monkeypatch):
    """A 403/timeout on an inactive member = INCONCLUSIVE → KEEP (fail-safe), even with auto-hide on."""
    monkeypatch.setattr(get_settings(), "site_liveness_auto_hide", True)
    lst = await _listing(is_active=False, url="https://nekretnine.rs/blocked/")
    sl = await _card(lst)

    report = await site_liveness_sweep(client=_client(_by_path({"/blocked/": 403})))

    assert len(report.candidates_unconfirmed) == 1
    assert len(report.hidden) == 0
    assert await _is_public(sl) is True


@pytest.mark.asyncio
async def test_head_transport_error_is_inconclusive_and_counted(db, monkeypatch):
    monkeypatch.setattr(get_settings(), "site_liveness_auto_hide", True)
    lst = await _listing(is_active=False, url="https://nekretnine.rs/slow/")
    sl = await _card(lst)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out", request=request)

    report = await site_liveness_sweep(client=_client(handler))

    assert report.errors == 1
    assert len(report.candidates_unconfirmed) == 1
    assert await _is_public(sl) is True


@pytest.mark.asyncio
async def test_pin_keeps_card_even_when_all_dead(db, monkeypatch):
    monkeypatch.setattr(get_settings(), "site_liveness_auto_hide", True)
    lst = await _listing(is_active=False, url="https://nekretnine.rs/gone/")
    sl = await _card(lst, keep_on_site=True)

    report = await site_liveness_sweep(client=_client(_by_path({"/gone/": 404})))

    assert report.kept_pinned == 1
    assert len(report.hidden) == 0
    assert report.total_flagged == 0
    assert await _is_public(sl) is True


@pytest.mark.asyncio
async def test_cap_limits_hides_overflow_to_candidates(db, monkeypatch):
    monkeypatch.setattr(get_settings(), "site_liveness_auto_hide", True)
    monkeypatch.setattr(get_settings(), "site_liveness_max_hides", 10)
    for _ in range(11):
        lst = await _listing(is_active=False, url=f"https://nekretnine.rs/gone/{next(_N)}/")
        await _card(lst)

    report = await site_liveness_sweep(client=_client(_by_path({"/gone/": 404})))

    assert len(report.hidden) == 10, "cap D3 bounds hides per night"
    assert len(report.candidates_confirmed) == 1, "the 11th all-dead card spills to candidates"


@pytest.mark.asyncio
async def test_dry_run_computes_but_does_not_mutate(db, monkeypatch):
    monkeypatch.setattr(get_settings(), "site_liveness_auto_hide", True)
    lst = await _listing(is_active=False, url="https://nekretnine.rs/gone/")
    sl = await _card(lst)

    report = await site_liveness_sweep(client=_client(_by_path({"/gone/": 404})), dry_run=True)

    assert len(report.hidden) == 1, "dry-run still reports what WOULD be hidden"
    assert await _is_public(sl) is True, "dry-run mutates nothing"


@pytest.mark.asyncio
async def test_singleton_group_dead_is_confirmed_candidate(db):
    """A listing that never joined a dup-group (dup_group_id NULL) is a group of one."""
    lst = await _listing(is_active=False, dup_group_id=None, url="https://nekretnine.rs/gone/")
    sl = await _card(lst)

    report = await site_liveness_sweep(client=_client(_by_path({"/gone/": 404})))

    assert len(report.candidates_confirmed) == 1
    assert report.candidates_confirmed[0].group_size == 1
    assert report.candidates_confirmed[0].site_listing_id == sl


@pytest.mark.asyncio
async def test_empty_run_is_all_zero(db):
    report = await site_liveness_sweep(client=_client(_by_path({})))
    assert isinstance(report, LivenessReport)
    assert report.kept_active == 0 and report.kept_pinned == 0
    assert report.total_flagged == 0 and report.errors == 0


@pytest.mark.asyncio
async def test_district_falls_back_to_address_then_title(db):
    """D8 fix for the retention screenshot's «—»: district → address_raw → title."""
    lst = await _listing(
        is_active=False, district=None, address_raw="Кнеза Милоша 10", title="Стан",
        url="https://nekretnine.rs/gone/",
    )
    await _card(lst)

    report = await site_liveness_sweep(client=_client(_by_path({"/gone/": 404})))

    assert report.candidates_confirmed[0].district == "Кнеза Милоша 10"


# ------------------------------------------------------------- T3-4: notice (D8) + delivery


def _cand(i: int, *, price: int | None = 300, district: str | None = "Vračar",
          group_size: int = 1, dead: int = 1, inconclusive: int = 0) -> LivenessCandidate:
    return LivenessCandidate(
        site_listing_id=i, listing_id=i, price_eur=price, district=district,
        group_size=group_size, dead=dead, inconclusive=inconclusive, url=f"https://x/{i}/",
    )


def test_notice_is_silent_when_nothing_flagged():
    assert format_liveness_notice(LivenessReport(kept_active=5, kept_pinned=2)) is None


def test_notice_has_sections_top10_and_overflow():
    rep = LivenessReport(
        hidden=[_cand(i) for i in range(3)],
        candidates_confirmed=[_cand(100 + i) for i in range(12)],  # >10 → «…и ещё 2»
        candidates_unconfirmed=[_cand(200, dead=0, inconclusive=1)],
    )
    body = format_liveness_notice(rep)
    assert body is not None
    assert "🪦 сняты автоматически (3)" in body
    assert "подтверждённо мертвы" in body and "(12)" in body
    assert "…и ещё 2" in body, "confirmed section collapses past the top-10"
    assert "не подтверждено" in body


def test_notice_price_and_district_and_group_in_row():
    body = format_liveness_notice(
        LivenessReport(candidates_confirmed=[_cand(1, price=1234, district="Врачар",
                                                   group_size=2, dead=2)])
    )
    assert body is not None
    assert "1234€" in body and "Врачар" in body
    assert "мертво 2/2 площадок" in body


@pytest.mark.asyncio
async def test_send_liveness_notice_sends_when_flagged_and_token(monkeypatch):
    import src.site.liveness as liveness_mod

    monkeypatch.setattr(get_settings(), "tg_bot_token", "test-token")
    sent = {}

    async def fake_send(text):  # send_text_via_bot(text) — bot-only, no client arg
        sent["text"] = text

    monkeypatch.setattr(liveness_mod, "send_text_via_bot", fake_send)
    ok = await send_liveness_notice(LivenessReport(candidates_confirmed=[_cand(1, price=1234)]))
    assert ok is True
    assert "1234€" in sent["text"]


@pytest.mark.asyncio
async def test_send_liveness_notice_noop_when_empty(monkeypatch):
    import src.site.liveness as liveness_mod

    calls = []

    async def fake_send(text):
        calls.append(text)

    monkeypatch.setattr(liveness_mod, "send_text_via_bot", fake_send)
    assert await send_liveness_notice(LivenessReport(kept_active=3)) is False
    assert calls == []


@pytest.mark.asyncio
async def test_send_liveness_notice_noop_without_bot_token(monkeypatch):
    import src.site.liveness as liveness_mod

    monkeypatch.setattr(get_settings(), "tg_bot_token", "")
    calls = []

    async def fake_send(text):
        calls.append(text)

    monkeypatch.setattr(liveness_mod, "send_text_via_bot", fake_send)
    assert await send_liveness_notice(LivenessReport(candidates_confirmed=[_cand(1)])) is False
    assert calls == []


# ------------------------------------------------------------- T3-4: nightly step (fault-isolated)


@pytest.mark.asyncio
async def test_liveness_nightly_line_killswitch_off(monkeypatch):
    """SITE_LIVENESS_ENABLED=0 → step is skipped with an explicit summary line, no sweep runs."""
    import src.cli as cli_mod

    monkeypatch.setattr(get_settings(), "site_liveness_enabled", False)
    # If the sweep were called it would need a client; make it explode to prove it's NOT called.
    async def boom(**_kw):
        raise AssertionError("sweep must not run when the kill switch is off")

    monkeypatch.setattr("src.site.liveness.site_liveness_sweep", boom)
    line = await cli_mod._liveness_nightly_line()
    assert "выкл" in line


@pytest.mark.asyncio
async def test_liveness_nightly_line_fault_isolated(monkeypatch):
    """A sweep failure becomes a 🚨 summary line — the nightly run never dies on it."""
    import src.cli as cli_mod

    monkeypatch.setattr(get_settings(), "site_liveness_enabled", True)

    async def boom(**_kw):
        raise RuntimeError("sweep exploded")

    monkeypatch.setattr("src.site.liveness.site_liveness_sweep", boom)
    line = await cli_mod._liveness_nightly_line()
    assert line.startswith("🚨")
    assert "liveness" in line


@pytest.mark.asyncio
async def test_liveness_one_listing_with_pinned_and_unpinned_cards_hides_only_eligible(db, monkeypatch):
    """A listing-level UPDATE used to remove a pinned second card as a side effect."""
    monkeypatch.setattr(get_settings(), "site_liveness_auto_hide", True)
    lid = await _listing(is_active=False, url="https://example.test/gone/")
    removable = await _card(lid)
    pinned = await _card(lid, keep_on_site=True)
    report = await site_liveness_sweep(client=_client(_by_path({"/gone/": 404})))
    assert len(report.hidden) == 1
    assert not await _is_public(removable)
    assert await _is_public(pinned)


@pytest.mark.asyncio
async def test_liveness_respects_remaining_combined_nightly_cap(db, monkeypatch):
    monkeypatch.setattr(get_settings(), "site_liveness_auto_hide", True)
    lid = await _listing(is_active=False, url="https://example.test/gone/")
    sl = await _card(lid)
    report = await site_liveness_sweep(client=_client(_by_path({"/gone/": 404})), max_hides=0)
    assert not report.hidden and len(report.candidates_confirmed) == 1
    assert await _is_public(sl)


async def test_liveness_hung_source_preserves_time_for_later_source(db, monkeypatch):
    import asyncio
    import time

    import src.site.liveness as live
    from src.scrapers.link_probe import LinkObservation

    first = await _listing(is_active=False, platform=Platform.NEKRETNINE)
    second = await _listing(is_active=False, platform=Platform.CITYEXPERT)
    await _card(first)
    await _card(second)
    calls = []

    class Probe:
        def __init__(self, platform):
            self.platform = platform
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            return None
        async def probe(self, url):
            calls.append(self.platform)
            if self.platform == Platform.NEKRETNINE:
                await asyncio.sleep(20)
            return LinkObservation('ACTIVE')

    monkeypatch.setattr(live, 'SourceProbeSession', Probe)
    monkeypatch.setattr(get_settings(), 'site_liveness_budget_sec', 0.2)
    started = time.monotonic()
    report = await site_liveness_sweep()
    assert time.monotonic() - started < 1
    assert Platform.CITYEXPERT in calls
    assert not report.hidden
    async with get_session() as session:
        recovered = await session.get(Listing, second)
        assert recovered.is_active
        assert recovered.link_succeeded_at is not None


async def test_liveness_unknown_backoff_allows_later_rows_next_run(db, monkeypatch):
    import src.site.liveness as live
    from src.scrapers.link_probe import LinkObservation

    ids = [await _listing(is_active=False) for _ in range(3)]
    for listing_id in ids:
        await _card(listing_id)
    calls = []

    class Probe:
        def __init__(self, _platform): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return None
        async def probe(self, url):
            calls.append(url)
            return LinkObservation('INCONCLUSIVE', True, 'challenge')

    monkeypatch.setattr(live, 'SourceProbeSession', Probe)
    first = await site_liveness_sweep()
    second = await site_liveness_sweep()
    assert first.checked == 3
    assert second.checked == 0
    assert len(calls) == 3
    assert len(second.candidates_unconfirmed) == 3
    assert not second.hidden


async def test_hung_oldest_link_does_not_starve_next_same_source_run(db, monkeypatch):
    import asyncio

    import src.site.liveness as live
    from src.scrapers.link_probe import LinkObservation

    ids = [await _listing(is_active=False) for _ in range(3)]
    for listing_id in ids:
        await _card(listing_id)
    calls = []

    class Probe:
        def __init__(self, _platform): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return None
        async def probe(self, url):
            calls.append(url)
            if len(calls) == 1:
                await asyncio.sleep(20)
            return LinkObservation('ACTIVE')

    monkeypatch.setattr(live, 'SourceProbeSession', Probe)
    monkeypatch.setattr(get_settings(), 'site_liveness_budget_sec', 0.15)
    first = await site_liveness_sweep()
    assert first.checked == 1
    second = await site_liveness_sweep()
    assert second.checked == 2
    assert len(set(calls)) == 3
    async with get_session() as session:
        oldest = await session.get(Listing, ids[0])
        assert oldest.link_checked_at is not None
        assert oldest.link_next_retry_at is not None
        assert oldest.link_status == 'INCONCLUSIVE'
        assert not oldest.is_active
