"""W3-G2: `otz districts-report` — missing-district detection against districts.yaml."""

from __future__ import annotations

import pytest

import src.db.session as db_session
import src.post.hashtags as hashtags
from src.cli import (
    _active_district_counts,
    _district_yaml_suggestion,
    _districts_report_lines,
    _missing_districts,
)
from src.db.models import Listing, Platform

YAML_BODY = '''\
"Vracar": Vracar
"Vračar": Vracar
"Novi Beograd": NoviBeograd
"Blok 45": NoviBeograd
'''


@pytest.fixture
def districts_yaml(tmp_path, monkeypatch):
    """Temp districts.yaml swapped under src.post.hashtags; lru_cache cleared both ways."""
    p = tmp_path / "districts.yaml"
    p.write_text(YAML_BODY, encoding="utf-8")
    monkeypatch.setattr(hashtags, "DISTRICTS_PATH", p)
    hashtags._districts_map.cache_clear()
    yield p
    hashtags._districts_map.cache_clear()


async def seed_listing(ext: str, *, district: str | None, active: bool = True) -> int:
    async with db_session.get_session() as s:
        listing = Listing(
            platform=Platform.NEKRETNINE,
            external_id=ext,
            url=f"https://example.com/{ext}",
            district=district,
            is_active=active,
        )
        s.add(listing)
        await s.flush()
        return listing.id


async def test_active_district_counts_only_active_non_empty(db):
    await seed_listing("a1", district="Vidikovac")
    await seed_listing("a2", district="Vidikovac")
    await seed_listing("a3", district="Vračar")
    await seed_listing("a4", district="Mirijevo", active=False)  # inactive — excluded
    await seed_listing("a5", district=None)
    await seed_listing("a6", district="")

    counts = dict(await _active_district_counts())

    assert counts == {"Vidikovac": 2, "Vračar": 1}


def test_missing_districts_excludes_known_variants(districts_yaml):
    counts = [
        ("Vračar", 50),          # known via diacritics-normalised "Vracar"
        ("Novi Beograd", 40),    # exact known
        ("Blok 45", 10),         # known micro-location
        ("Vračar Neimar", 5),    # token-substring resolves through district_tag → known
        ("Vidikovac", 7),        # missing
        ("Altina", 7),           # missing — same count, alphabetical after Vidikovac? no: a < v
        ("Rušanj", 1),           # missing, low count
    ]

    missing = _missing_districts(counts, min_count=1)

    assert missing == [("Altina", 7), ("Vidikovac", 7), ("Rušanj", 1)]
    # min_count hides the rare ones
    assert _missing_districts(counts, min_count=2) == [("Altina", 7), ("Vidikovac", 7)]


def test_yaml_suggestion_lines():
    assert _district_yaml_suggestion("Vidikovac") == '"Vidikovac": Vidikovac'
    assert _district_yaml_suggestion("Banovo brdo") == '"Banovo brdo": BanovoBrdo'
    # diacritics stripped via NFKD
    assert _district_yaml_suggestion("Bežanijska kosa") == '"Bežanijska kosa": BezanijskaKosa'
    # non-latinizable (Cyrillic survives NFKD) → explicit FIXME, not silent mojibake
    assert _district_yaml_suggestion("Звездара") == '"Звездара": FIXME'


def test_report_lines_contain_freqs_yaml_and_deploy_reminder():
    lines = _districts_report_lines([("Vidikovac", 7), ("Altina", 2)], top=30)
    text = "\n".join(lines)

    assert "    7  Vidikovac" in text
    assert '"Vidikovac": Vidikovac' in text
    assert '"Altina": Altina' in text
    # the one operational gotcha the command must always repeat:
    assert "deploy_sync.sh" in text
    assert "ЕДИНСТВЕННЫЙ" in text

    # top cap honoured
    capped = _districts_report_lines([("A", 3), ("B", 2), ("C", 1)], top=2)
    assert not any('"C"' in line for line in capped)


def test_report_lines_when_everything_covered():
    text = "\n".join(_districts_report_lines([], top=30))
    assert "уже покрыты" in text
    assert "deploy_sync.sh" in text  # reminder stays even when nothing to add


async def test_end_to_end_against_seeded_db(db, districts_yaml):
    await seed_listing("e1", district="Vračar")     # covered
    await seed_listing("e2", district="Vidikovac")  # missing
    await seed_listing("e3", district="Vidikovac")  # missing

    counts = await _active_district_counts()
    missing = _missing_districts(counts, min_count=1)

    assert missing == [("Vidikovac", 2)]
