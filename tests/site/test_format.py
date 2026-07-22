"""Display normalization helpers (DATA_CONTRACT §4–§5)."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from src.config import get_settings
from src.site.format import human_district, is_rented, public_photo_url, rooms_label


@pytest.mark.parametrize(
    "rooms,expected",
    [
        (None, "студия"),
        (0.0, "студия"),
        (1.0, "1+1"),
        (1.5, "1.5+1"),
        (2.0, "2+1"),
        (3.0, "3+1"),
        (4.0, "4+1"),
        (2.5, "2.5+1"),
    ],
)
def test_rooms_label(rooms: float | None, expected: str) -> None:
    assert rooms_label(rooms) == expected


def test_human_district_cyrillic_raw_passthrough_to_cyrillic() -> None:
    # Cyrillic raw that maps to a Latinic value -> Cyrillic display key.
    assert human_district("Врачар") == "Врачар"


def test_human_district_latin_raw_resolves_to_cyrillic() -> None:
    # Latin raw -> value "Vracar" -> reverse to Cyrillic key.
    assert human_district("Vracar") == "Врачар"


def test_human_district_micro_location_resolves_to_parent_cyrillic() -> None:
    # "Dorcol" maps to value "Dorcol"; reverse-map finds the Cyrillic key if any,
    # else falls back to the canonical Latinic value. "Dušanovac" -> Vozdovac -> Вождовац.
    assert human_district("Dušanovac") == "Вождовац"


def test_human_district_multiword_resolves() -> None:
    # "Vracar Centar" -> value via word match -> Cyrillic.
    assert human_district("Vracar Centar") == "Врачар"


def test_human_district_halooglasi_parent_municipality_is_a_core_district() -> None:
    """Halooglasi' parent of Dobanovačka must render as Zemun, never the micro-location."""
    from src.site.format import CORE_DISTRICTS

    district = human_district("Opština Zemun")
    assert district == "Земун"
    assert district in CORE_DISTRICTS


def test_human_district_unmapped_passthrough() -> None:
    assert human_district("Completely Unknown Place") == "Completely Unknown Place"


def test_human_district_none() -> None:
    assert human_district(None) == ""


def test_human_district_empty_string() -> None:
    assert human_district("") == ""


# --- S1-3: no Latin district may leak onto the public catalog ---
# Each of these raw values used to resolve to a Latin/CamelCase form (orphan yaml value
# with no Cyrillic key) and leaked latinica onto the showcase. They must now show Cyrillic.
# Правка Даши №4 (2026-07-18): все они — микрорайоны, поэтому ожидаемое имя теперь
# родительский район из CORE_DISTRICTS (было: собственное имя микрорайона).
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Dorcol", "Стари град"),
        ("Dorćol", "Стари град"),
        ("Banovo brdo", "Чукарица"),
        ("Banovo Brdo", "Чукарица"),
        ("Senjak", "Савски венац"),
        ("Karaburma", "Палилула"),
        ("Centar", "Стари град"),
        ("Cerak", "Чукарица"),
        ("Dedinje", "Савски венац"),
        ("Slavija", "Врачар"),
        ("Gardoš", "Земун"),
        ("Gardos", "Земун"),
        ("Bežanijska kosa", "Нови Београд"),
        ("Beograd na vodi", "Савски венац"),
        ("Belgrade Waterfront", "Савски венац"),
        ("Filmski grad", "Чукарица"),
        ("Petlovo brdo", "Раковица"),
        ("Topčidersko brdo", "Савски венац"),
        ("Profesorska kolonija", "Звездара"),
        ("Profesorska kolonija - Hala Pionir", "Звездара"),
    ],
)
def test_human_district_latin_no_longer_leaks(raw: str, expected: str) -> None:
    out = human_district(raw)
    assert out == expected
    # Defense: the result must contain at least one Cyrillic char (no bare latinica).
    assert any("Ѐ" <= ch <= "ӿ" for ch in out)


def test_microdistricts_collapse_to_parent_district():
    # Правка 4: на витрине только 10 районов — микрорайоны схлопываются в родителя.
    from src.site.format import human_district

    assert human_district("Dorcol") == "Стари град"
    assert human_district("Dorćol") == "Стари град"
    assert human_district("Bežanijska kosa") == "Нови Београд"
    assert human_district("Belgrade Waterfront") == "Савски венац"
    assert human_district("Banovo brdo") == "Чукарица"
    assert human_district("Karaburma") == "Палилула"
    assert human_district("Slavija") == "Врачар"
    assert human_district("Gardoš") == "Земун"
    assert human_district("Petlovo brdo") == "Раковица"
    assert human_district("Profesorska kolonija") == "Звездара"


def test_core_districts_is_the_ten_approved():
    from src.site.format import CORE_DISTRICTS

    assert CORE_DISTRICTS == (
        "Вождовац",
        "Врачар",
        "Звездара",
        "Земун",
        "Нови Београд",
        "Палилула",
        "Раковица",
        "Савски венац",
        "Стари град",
        "Чукарица",
    )


def test_human_district_already_cyrillic_passthrough() -> None:
    # Already-Cyrillic raw maps to its own Cyrillic display key, unchanged.
    assert human_district("Звездара") == "Звездара"
    assert human_district("Стари град") == "Стари град"


def test_human_district_unmapped_latin_passthrough_unchanged() -> None:
    # Genuinely unmapped latinica is returned verbatim (no crash); reportable via
    # the districts-report CLI. Passthrough behavior must not change.
    raw = "Neki Sasvim Nepoznat Kraj"
    assert human_district(raw) == raw


def test_human_district_no_cyrillic_key_falls_back_to_value() -> None:
    # "Blok 21" -> value "NoviBeograd"; there is a Cyrillic key for that value
    # ("Нови Београд"), so it should resolve to Cyrillic.
    assert human_district("Blok 21") == "Нови Београд"


@dataclass
class _FakeListing:
    is_active: bool = True
    description: str | None = None
    title: str | None = None


def test_is_rented_inactive() -> None:
    assert is_rented(_FakeListing(is_active=False)) is True


def test_is_rented_active_no_marker() -> None:
    assert is_rented(_FakeListing(is_active=True, description="lovely flat")) is False


@pytest.mark.parametrize("marker", ["снято", "СНЯТО", "izdato", "Izdato", "rented", "RENTED"])
def test_is_rented_marker_in_text(marker: str) -> None:
    assert is_rented(_FakeListing(is_active=True, description=f"квартира {marker} уже")) is True


def test_public_photo_url_absolute_under_root() -> None:
    root = get_settings().site_photos_root
    local = str(root / "5" / "100" / "00.jpg")
    assert public_photo_url(local) == "/photos/5/100/00.jpg"


def test_public_photo_url_already_relative() -> None:
    # Pipeline stores paths relative to the working dir under data/inbound; the data/inbound
    # prefix is stripped so the URL tail matches Caddy's /photos root (= data/inbound).
    assert public_photo_url("data/inbound/5/100/00.jpg") == "/photos/5/100/00.jpg"


def test_public_photo_url_never_remote() -> None:
    # A stray remote URL must never be passed through as-is to a public template.
    out = public_photo_url("https://cdn.telegram.org/file/abc.jpg")
    assert not out.startswith("http")
    assert out.startswith("/photos/")


def test_public_photo_url_rejects_relative_traversal() -> None:
    # A '..' traversal (even if photo_paths ever became relative/tainted) collapses to basename.
    out = public_photo_url("../../../../etc/passwd")
    assert ".." not in out
    assert out == "/photos/passwd"


def test_public_photo_url_empty_is_empty() -> None:
    assert public_photo_url("") == ""
