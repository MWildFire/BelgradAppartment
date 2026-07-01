"""T1-2 — guard on `otz heal-urls` / `_heal_urls`.

The default sweep must (a) consider only is_active rows and (b) HEAD-check that the canonical
URL actually resolves before writing — a canonical that 404s or errors is skipped with a
warning, so a heal never repoints a row at a removed page. `--include-inactive` is the escape
hatch restoring the old unguarded sweep (all rows, no resolve check).

DB setup uses the shared `db` fixture (in-memory SQLite swapped into the global session
factory); HTTP is faked with httpx.MockTransport — no network.
"""

from __future__ import annotations

import logging

import httpx
from click.testing import CliRunner
from sqlalchemy import select

from src.cli import _heal_urls, cli
from src.db.models import Listing, Platform
from src.db.session import get_session


def _canon(ext: str) -> str:
    return f"https://www.nekretnine.rs/oglasi/{ext}/"


def _slug(ext: str) -> str:
    return f"https://www.nekretnine.rs/oglasi/{ext}/some-stale-slug/"


async def _seed(*, external_id: str, url: str, is_active: bool = True) -> int:
    async with get_session() as s:
        listing = Listing(
            platform=Platform.NEKRETNINE, external_id=external_id, url=url,
            photo_urls=[], photo_phashes=[], is_active=is_active,
        )
        s.add(listing)
        await s.flush()
        return listing.id


async def _urls_by_ext() -> dict[str, str]:
    async with get_session() as s:
        return dict((await s.execute(select(Listing.external_id, Listing.url))).all())


def _recording_client(responses: dict[str, int]) -> tuple[httpx.AsyncClient, list[str]]:
    """MockTransport client answering per-URL statuses; records every requested URL.

    A URL missing from `responses` raises ConnectError (models a dead host / no network).
    """
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        requested.append(url)
        if url not in responses:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(responses[url], request=request)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), requested


async def test_default_skips_inactive_rows(db):
    """Inactive rows are corpses awaiting purge — the default sweep must not touch (or even
    HEAD-check) them; only the active slug row is healed."""
    await _seed(external_id="1", url=_slug("1"), is_active=True)
    await _seed(external_id="2", url=_slug("2"), is_active=False)
    client, requested = _recording_client({_canon("1"): 200, _canon("2"): 200})

    healed = await _heal_urls(platform="nekretnine", dry_run=False, client=client)

    assert healed == 1
    urls = await _urls_by_ext()
    assert urls["1"] == _canon("1")
    assert urls["2"] == _slug("2"), "inactive row must stay untouched"
    assert requested == [_canon("1")], "inactive row must not even be HEAD-checked"


async def test_default_skips_unresolving_canonical(db, caplog):
    """A canonical that 404s or errors out is NOT written — skip + warn; the resolving row
    is still healed in the same run."""
    ok_id = await _seed(external_id="10", url=_slug("10"))
    dead_id = await _seed(external_id="20", url=_slug("20"))
    err_id = await _seed(external_id="30", url=_slug("30"))
    # 30 is absent from the map → ConnectError (network-level failure is also "не резолвится").
    client, _ = _recording_client({_canon("10"): 200, _canon("20"): 404})

    with caplog.at_level(logging.WARNING, logger="src.cli"):
        healed = await _heal_urls(platform="nekretnine", dry_run=False, client=client)

    assert healed == 1
    urls = await _urls_by_ext()
    assert urls["10"] == _canon("10")
    assert urls["20"] == _slug("20"), "404 canonical must be skipped"
    assert urls["30"] == _slug("30"), "network error must be skipped"
    assert caplog.text.count("does not resolve") == 2
    assert _canon("20") in caplog.text and _canon("30") in caplog.text
    assert ok_id and dead_id and err_id  # seeded ids exist; silences unused warnings


async def test_include_inactive_restores_old_sweep(db):
    """Escape hatch: include_inactive=True is the pre-guard behaviour — all rows healed,
    no resolve check at all."""
    await _seed(external_id="1", url=_slug("1"), is_active=True)
    await _seed(external_id="2", url=_slug("2"), is_active=False)
    client, requested = _recording_client({})  # any HEAD would raise ConnectError

    healed = await _heal_urls(
        platform="nekretnine", dry_run=False, include_inactive=True, client=client
    )

    assert healed == 2
    urls = await _urls_by_ext()
    assert urls["1"] == _canon("1")
    assert urls["2"] == _canon("2")
    assert requested == [], "old behaviour must not resolve-check anything"


async def test_dry_run_scopes_but_stays_offline(db):
    """Dry-run applies the is_active scope (the count previews the guarded run) but never
    touches the network and never writes."""
    await _seed(external_id="1", url=_slug("1"), is_active=True)
    await _seed(external_id="2", url=_slug("2"), is_active=False)
    client, requested = _recording_client({})

    healed = await _heal_urls(platform="nekretnine", dry_run=True, client=client)

    assert healed == 1, "inactive row is out of scope even in dry-run"
    urls = await _urls_by_ext()
    assert urls["1"] == _slug("1") and urls["2"] == _slug("2"), "dry-run must not write"
    assert requested == [], "dry-run must stay offline"


def test_cli_include_inactive_flag_wiring(monkeypatch):
    """`--include-inactive` reaches _heal_urls; the default leaves the guard on."""
    captured: list[dict] = []

    async def fake_heal(**kwargs):
        captured.append(kwargs)
        return 0

    monkeypatch.setattr("src.cli._heal_urls", fake_heal)
    runner = CliRunner()

    result = runner.invoke(cli, ["heal-urls", "--no-dry-run", "--include-inactive"])
    assert result.exit_code == 0
    assert captured[-1]["include_inactive"] is True
    assert captured[-1]["dry_run"] is False

    result = runner.invoke(cli, ["heal-urls"])
    assert result.exit_code == 0
    assert captured[-1]["include_inactive"] is False
