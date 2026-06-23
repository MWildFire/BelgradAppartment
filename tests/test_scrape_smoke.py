"""Tests for the live-smoke probe + structure fingerprint + drift detection (feature A2).

Covers four layers:
  1. Per-scraper `fingerprint(list_html)` — structure-only (keys/types/source), never values.
  2. `compute_drift(old_fp, new_fp)` — human-readable removed/added keys & changed source.
  3. `run_smoke(scraper)` — list+detail probe, fingerprint vs last-known-good state.
  4. Contract guard — each real scraper's `parse_detail(fixture)` yields all REQUIRED_DETAIL_FIELDS.

The probe is driven offline with a fake scraper returning canned fixture data — no network, no
Playwright. The state file is redirected to tmp_path so tests never touch real `data/`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.db.models import Platform
from src.scrapers import SCRAPERS
from src.scrapers.base import ListingDetail, ListingPreview, Scraper
from src.scrapers.cetiri_zida import CetiriZidaScraper
from src.scrapers.halooglasi import HalooglasiScraper
from src.scrapers.nekretnine import NekretnineScraper
from src.scrapers.smoke import SmokeResult, compute_drift, run_smoke

FIXTURES = Path(__file__).parent / "fixtures"


def _read(rel: str) -> str:
    return (FIXTURES / rel).read_text(encoding="utf-8")


@pytest.fixture
def halooglasi_list_html() -> str:
    return _read("halooglasi/list.html")


# --------------------------------------------------------------------------- fingerprint stability


def test_nekretnine_fingerprint_stable(nekretnine_list_html: str) -> None:
    fp = NekretnineScraper.fingerprint(nekretnine_list_html)
    assert fp["source"] == "__NEXT_DATA__"
    assert "realEstate" in fp["result_keys"] and "seo" in fp["result_keys"]
    assert fp["n"] >= 1
    # Structure-only: result_keys is a sorted list of the FIRST result's keys (no values leaked).
    assert fp["result_keys"] == sorted(fp["result_keys"])


def test_cetiri_zida_fingerprint_stable(cetiri_zida_list_html: str) -> None:
    fp = CetiriZidaScraper.fingerprint(cetiri_zida_list_html)
    assert fp["source"] == "json-ld"
    assert "ItemList" in fp["types"]
    assert fp["types"] == sorted(fp["types"])
    # item_keys come from the first ItemList element.
    assert "@type" in fp["item_keys"] and "url" in fp["item_keys"]


def test_halooglasi_fingerprint_stable(halooglasi_list_html: str) -> None:
    fp = HalooglasiScraper.fingerprint(halooglasi_list_html)
    assert fp["source"] == "serverListData"
    assert "Id" in fp["ad_keys"] and "OtherFields" in fp["ad_keys"]
    assert fp["ad_keys"] == sorted(fp["ad_keys"])
    assert fp["n"] >= 1


def test_fingerprint_empty_on_garbage() -> None:
    # No payload → empty dict, not a crash. Probe treats {} as "can't fingerprint".
    assert NekretnineScraper.fingerprint("<html>nope</html>") == {}
    assert CetiriZidaScraper.fingerprint("<html>nope</html>") == {}
    assert HalooglasiScraper.fingerprint("<html>nope</html>") == {}


def test_base_fingerprint_default_empty() -> None:
    assert Scraper.fingerprint("<html></html>") == {}


# --------------------------------------------------------------------------- drift detection


def test_compute_drift_detects_removed_key() -> None:
    old = {"source": "__NEXT_DATA__", "result_keys": ["realEstate", "seo"], "n": 25}
    new = {"source": "__NEXT_DATA__", "result_keys": ["realEstate"], "n": 25}
    drift = compute_drift(old, new)
    assert drift, "removing a result key must be reported as drift"
    assert any("seo" in line for line in drift)


def test_compute_drift_detects_added_key() -> None:
    old = {"source": "json-ld", "types": ["ItemList"], "item_keys": ["url"]}
    new = {"source": "json-ld", "types": ["ItemList"], "item_keys": ["url", "price"]}
    drift = compute_drift(old, new)
    assert any("price" in line for line in drift)


def test_compute_drift_detects_changed_source() -> None:
    old = {"source": "__NEXT_DATA__", "result_keys": ["a"]}
    new = {"source": "json-ld", "result_keys": ["a"]}
    drift = compute_drift(old, new)
    assert any("source" in line.lower() for line in drift)


def test_compute_drift_identical_is_empty() -> None:
    fp = {"source": "__NEXT_DATA__", "result_keys": ["realEstate", "seo"], "n": 25}
    assert compute_drift(fp, dict(fp)) == []


def test_compute_drift_first_run_is_empty() -> None:
    # Missing/empty old fingerprint = baseline, NOT drift.
    new = {"source": "__NEXT_DATA__", "result_keys": ["realEstate", "seo"]}
    assert compute_drift({}, new) == []
    assert compute_drift(None, new) == []  # type: ignore[arg-type]


def test_compute_drift_ignores_count_only_change() -> None:
    # `n` is a count, not structure — its change alone must not raise drift.
    old = {"source": "serverListData", "ad_keys": ["Id"], "n": 20}
    new = {"source": "serverListData", "ad_keys": ["Id"], "n": 19}
    assert compute_drift(old, new) == []


# --------------------------------------------------------------------------- run_smoke (fake scraper)


class _FakeScraper(Scraper):
    """Offline stand-in: returns canned previews/detail/list-html without network or Playwright."""

    platform = Platform.NEKRETNINE
    list_url = "https://example.test/list"
    MIN_PREVIEWS = 5

    def __init__(
        self,
        *,
        previews: list[ListingPreview],
        details: dict[str, ListingDetail],
        list_html: str = "<html>list</html>",
        fingerprint_fp: dict | None = None,
    ) -> None:
        # Skip Scraper.__init__ — no httpx client needed for the fake.
        self._previews = previews
        self._details = details
        self._list_html = list_html
        self._fp = fingerprint_fp if fingerprint_fp is not None else {}

    async def list_recent(self, max_pages: int = 5, **kwargs: object) -> list[ListingPreview]:
        return self._previews

    async def fetch_detail(self, url: str) -> ListingDetail:
        return self._details[url]

    async def _fetch_page(self, url: str) -> str | None:
        return self._list_html

    def fingerprint(self, list_html: str) -> dict:  # type: ignore[override]
        return self._fp

    async def aclose(self) -> None:
        return None


def _preview(i: int) -> ListingPreview:
    return ListingPreview(
        platform=Platform.NEKRETNINE,
        external_id=str(i),
        url=f"https://example.test/oglasi/{i}/",
        title=f"Stan {i}",
    )


def _full_detail(url: str) -> ListingDetail:
    return ListingDetail(
        platform=Platform.NEKRETNINE,
        external_id=url.rstrip("/").split("/")[-1],
        url=url,
        price_eur=350,
        area_m2=50.0,
        photo_urls=["https://pic.example/1.jpg"],
    )


def _state_redirect(monkeypatch, tmp_path: Path) -> Path:
    """Point the smoke state dir at tmp_path; return the fingerprints file path."""
    import src.scrapers.smoke as smoke_mod

    state_dir = tmp_path / "state"
    monkeypatch.setattr(smoke_mod, "_state_path", lambda: state_dir / "parser_fingerprints.json")
    return state_dir / "parser_fingerprints.json"


@pytest.mark.asyncio
async def test_run_smoke_ok_case(monkeypatch, tmp_path: Path) -> None:
    _state_redirect(monkeypatch, tmp_path)
    previews = [_preview(i) for i in range(1, 7)]
    details = {p.url: _full_detail(p.url) for p in previews}
    sc = _FakeScraper(previews=previews, details=details, fingerprint_fp={"source": "x", "result_keys": ["a"]})

    res = await run_smoke(sc)
    assert isinstance(res, SmokeResult)
    assert res.ok is True
    assert res.n_previews == 6
    assert res.detail_ok is True
    assert res.missing_fields == []
    assert res.drift == []
    assert res.error is None


@pytest.mark.asyncio
async def test_run_smoke_too_few_previews(monkeypatch, tmp_path: Path) -> None:
    _state_redirect(monkeypatch, tmp_path)
    previews = [_preview(i) for i in range(1, 4)]  # only 3 < MIN_PREVIEWS(5)
    details = {p.url: _full_detail(p.url) for p in previews}
    sc = _FakeScraper(previews=previews, details=details)

    res = await run_smoke(sc)
    assert res.ok is False
    assert res.n_previews == 3


@pytest.mark.asyncio
async def test_run_smoke_missing_detail_field(monkeypatch, tmp_path: Path) -> None:
    _state_redirect(monkeypatch, tmp_path)
    previews = [_preview(i) for i in range(1, 7)]
    # EVERY preview's detail lacks photo_urls → detail_ok must be False.
    details = {}
    for p in previews:
        d = _full_detail(p.url)
        d.photo_urls = []
        details[p.url] = d
    sc = _FakeScraper(previews=previews, details=details)

    res = await run_smoke(sc)
    assert res.ok is False
    assert res.detail_ok is False
    assert "photo_urls" in res.missing_fields


@pytest.mark.asyncio
async def test_run_smoke_tolerates_single_no_price_listing(monkeypatch, tmp_path: Path) -> None:
    """A lone price-less first listing must NOT trip the probe — we probe a few previews and
    accept if at least one yields all required fields."""
    _state_redirect(monkeypatch, tmp_path)
    previews = [_preview(i) for i in range(1, 7)]
    details = {}
    # First listing has no price/area (a legitimate no-price ad); the rest are complete.
    d0 = _full_detail(previews[0].url)
    d0.price_eur = None
    d0.area_m2 = None
    details[previews[0].url] = d0
    for p in previews[1:]:
        details[p.url] = _full_detail(p.url)
    sc = _FakeScraper(previews=previews, details=details)

    res = await run_smoke(sc)
    assert res.ok is True
    assert res.detail_ok is True


@pytest.mark.asyncio
async def test_run_smoke_drift_warns_but_stays_ok(monkeypatch, tmp_path: Path) -> None:
    """A stale good-fingerprint differing from the live one → drift reported, but ok stays True
    (drift is WARN, not hard-fail)."""
    state_file = _state_redirect(monkeypatch, tmp_path)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    # Seed a stale baseline with an extra key the live structure no longer has.
    state_file.write_text(
        json.dumps({Platform.NEKRETNINE.value: {"source": "x", "result_keys": ["a", "b"]}}),
        encoding="utf-8",
    )
    previews = [_preview(i) for i in range(1, 7)]
    details = {p.url: _full_detail(p.url) for p in previews}
    sc = _FakeScraper(
        previews=previews, details=details, fingerprint_fp={"source": "x", "result_keys": ["a"]}
    )

    res = await run_smoke(sc)
    assert res.ok is True, "drift must NOT set ok=False"
    assert res.drift, "drift must be reported"
    assert any("b" in line for line in res.drift)


@pytest.mark.asyncio
async def test_run_smoke_persists_good_fingerprint_only_when_ok(monkeypatch, tmp_path: Path) -> None:
    state_file = _state_redirect(monkeypatch, tmp_path)
    good_fp = {"source": "x", "result_keys": ["a"]}

    # OK run → fingerprint persisted.
    previews = [_preview(i) for i in range(1, 7)]
    details = {p.url: _full_detail(p.url) for p in previews}
    ok_sc = _FakeScraper(previews=previews, details=details, fingerprint_fp=good_fp)
    res = await run_smoke(ok_sc)
    assert res.ok is True
    saved = json.loads(state_file.read_text())
    assert saved[Platform.NEKRETNINE.value] == good_fp

    # Broken run (too few previews) with a DIFFERENT fingerprint → must NOT overwrite the good one.
    broken_sc = _FakeScraper(
        previews=[_preview(1)], details={_preview(1).url: _full_detail(_preview(1).url)},
        fingerprint_fp={"source": "x", "result_keys": ["a", "b", "c"]},
    )
    res2 = await run_smoke(broken_sc)
    assert res2.ok is False
    saved2 = json.loads(state_file.read_text())
    assert saved2[Platform.NEKRETNINE.value] == good_fp, "broken run must not poison the baseline"


class _NoFetchPageScraper(Scraper):
    """Playwright-like stand-in (e.g. halooglasi): no cheap `_fetch_page`, so it stashes page-1
    HTML on `_last_list_html` during list_recent for the smoke fingerprint to pick up."""

    platform = Platform.HALOOGLASI
    list_url = "https://example.test/list"
    MIN_PREVIEWS = 5

    def __init__(
        self,
        *,
        previews: list[ListingPreview],
        details: dict[str, ListingDetail],
        last_list_html: str | None,
        fingerprint_fp: dict,
    ) -> None:
        self._previews = previews
        self._details = details
        self._last_list_html = last_list_html
        self._fp = fingerprint_fp

    async def list_recent(self, max_pages: int = 5, **kwargs: object) -> list[ListingPreview]:
        return self._previews

    async def fetch_detail(self, url: str) -> ListingDetail:
        return self._details[url]

    def fingerprint(self, list_html: str) -> dict:  # type: ignore[override]
        # Proves the stash reached the fingerprint step (not the default empty HTML).
        assert list_html, "fingerprint should receive the stashed list HTML"
        return self._fp

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_run_smoke_uses_stashed_html_when_no_fetch_page(monkeypatch, tmp_path: Path) -> None:
    """A scraper without `_fetch_page` (halooglasi) still gets layer-3 drift coverage from the
    page-1 HTML it stashed on `_last_list_html` during list_recent."""
    state_file = _state_redirect(monkeypatch, tmp_path)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(
        json.dumps({Platform.HALOOGLASI.value: {"source": "serverListData", "ad_keys": ["Id", "Gone"]}}),
        encoding="utf-8",
    )
    previews = [
        ListingPreview(platform=Platform.HALOOGLASI, external_id=str(i), url=f"https://example.test/{i}/")
        for i in range(1, 7)
    ]
    details = {
        p.url: ListingDetail(
            platform=Platform.HALOOGLASI, external_id=p.external_id, url=p.url,
            price_eur=400, area_m2=55.0, photo_urls=["https://pic/1.jpg"],
        )
        for p in previews
    }
    sc = _NoFetchPageScraper(
        previews=previews, details=details, last_list_html="<html>stashed</html>",
        fingerprint_fp={"source": "serverListData", "ad_keys": ["Id"]},
    )
    assert not hasattr(sc, "_fetch_page"), "this fake must exercise the stash fallback path"

    res = await run_smoke(sc)
    assert res.ok is True
    assert any("Gone" in line for line in res.drift), "drift from the stashed-HTML fingerprint"


@pytest.mark.asyncio
async def test_run_smoke_fingerprint_step_is_non_fatal(monkeypatch, tmp_path: Path) -> None:
    """A failure acquiring/parsing the list HTML for the fingerprint must NOT manufacture a
    not-ok alert from an otherwise-healthy list+detail probe (layer 3 is WARN-only)."""
    _state_redirect(monkeypatch, tmp_path)
    previews = [_preview(i) for i in range(1, 7)]
    details = {p.url: _full_detail(p.url) for p in previews}

    class _BadFingerprintFetch(_FakeScraper):
        async def _fetch_page(self, url: str) -> str | None:
            raise RuntimeError("fingerprint fetch boom")

    sc = _BadFingerprintFetch(previews=previews, details=details)
    res = await run_smoke(sc)
    assert res.ok is True, "fingerprint hiccup must not fail the probe"
    assert res.drift == []
    assert res.error is None


@pytest.mark.asyncio
async def test_run_smoke_exception_sets_error(monkeypatch, tmp_path: Path) -> None:
    _state_redirect(monkeypatch, tmp_path)

    class _Boom(_FakeScraper):
        async def list_recent(self, max_pages: int = 5, **kwargs: object) -> list[ListingPreview]:
            raise RuntimeError("kaboom")

    sc = _Boom(previews=[], details={})
    res = await run_smoke(sc)
    assert res.ok is False
    assert res.error is not None and "kaboom" in res.error


# --------------------------------------------------------------------------- contract guard (CI)


@pytest.mark.parametrize(
    ("platform", "detail_rel", "detail_url"),
    [
        (
            Platform.NEKRETNINE,
            "nekretnine/detail.html",
            "https://www.nekretnine.rs/oglasi/468187/",
        ),
        (
            Platform.CETIRI_ZIDA,
            "cetiri_zida/detail.html",
            "https://www.4zida.rs/izdavanje-stanova/blok-61-novi-beograd-beograd/"
            "trosoban-stan/650ae06563090cbb0c0c11e7",
        ),
        (
            Platform.CITYEXPERT,
            "cityexpert/detail.json",
            "https://cityexpert.rs/izdavanje-nekretnina/beograd/28075/"
            "troiposoban-stan-krunska-vracar",
        ),
    ],
)
def test_parse_detail_yields_all_required_fields(
    platform: Platform, detail_rel: str, detail_url: str
) -> None:
    """CI guard: a parser regression that drops price/area/photos is caught before prod."""
    scraper_cls = SCRAPERS[platform]
    detail = scraper_cls.parse_detail(detail_url, _read(detail_rel))
    for field in scraper_cls.REQUIRED_DETAIL_FIELDS:
        value = getattr(detail, field)
        assert value, f"{platform.value}.parse_detail missing required field {field!r}: {value!r}"


def test_halooglasi_parse_detail_yields_all_required_fields() -> None:
    detail_url = _read("halooglasi/detail_url.txt").strip()
    detail = HalooglasiScraper.parse_detail(detail_url, _read("halooglasi/detail.html"))
    for field in HalooglasiScraper.REQUIRED_DETAIL_FIELDS:
        assert getattr(detail, field), f"halooglasi missing {field!r}"
