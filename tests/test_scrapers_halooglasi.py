"""Unit tests for the halooglasi.com scraper.

Pure-parser tests: feed saved HTML, check that QuidditaEnvironment.serverListData /
.CurrentClassified are decoded correctly. No Playwright invocation here — that's covered by
the live smoke-test from CLI.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.db.models import Platform
from src.scrapers.halooglasi import HalooglasiScraper

FIXTURES = Path(__file__).parent / "fixtures" / "halooglasi"


@pytest.fixture
def halooglasi_list_html() -> str:
    return (FIXTURES / "list.html").read_text(encoding="utf-8")


@pytest.fixture
def halooglasi_detail_html() -> str:
    return (FIXTURES / "detail.html").read_text(encoding="utf-8")


@pytest.fixture
def halooglasi_detail_url() -> str:
    return (FIXTURES / "detail_url.txt").read_text(encoding="utf-8").strip()


def test_parse_list_extracts_20_ads(halooglasi_list_html: str) -> None:
    previews = HalooglasiScraper.parse_list(halooglasi_list_html)
    assert len(previews) == 20
    ids = {p.external_id for p in previews}
    assert len(ids) == 20, "external_ids must be unique"
    for p in previews:
        assert p.platform is Platform.HALOOGLASI
        assert p.url.startswith("https://www.halooglasi.com/nekretnine/izdavanje-stanova/")
        # halooglasi ad IDs are 13 digits
        assert p.external_id.isdigit() and len(p.external_id) >= 10


def test_parse_detail_core_fields(
    halooglasi_detail_html: str, halooglasi_detail_url: str
) -> None:
    detail = HalooglasiScraper.parse_detail(halooglasi_detail_url, halooglasi_detail_html)
    assert detail.platform is Platform.HALOOGLASI
    assert detail.external_id == "5425647097326"
    assert detail.price_eur == 400  # cena_d: 400.0 EUR
    assert detail.area_m2 == 50.0
    assert detail.rooms == 2.0  # broj_soba_s: "2.0"
    # coords from GeoLocationRPT: "44.848287,20.401391"
    assert detail.lat == pytest.approx(44.848287, abs=1e-4)
    assert detail.lon == pytest.approx(20.401391, abs=1e-4)
    assert detail.address_raw == "Dobrovoljačka"
    # district: prefers mikrolokacija_s "Dobanovačka", falls back to lokacija_s
    assert detail.district == "Dobanovačka"
    assert detail.title and "Zemun" in detail.title
    # photos: prefixed with img.halooglasi.com
    assert len(detail.photo_urls) >= 5
    for u in detail.photo_urls:
        assert u.startswith("https://img.halooglasi.com/slike/")


def test_parse_detail_returns_stub_on_missing_var() -> None:
    detail = HalooglasiScraper.parse_detail(
        "https://www.halooglasi.com/nekretnine/izdavanje-stanova/x/1234567890",
        "<html><body>no data here</body></html>",
    )
    assert detail.platform is Platform.HALOOGLASI
    assert detail.external_id == "1234567890"
    assert detail.price_eur is None
    assert detail.lat is None
    assert detail.photo_urls == []
