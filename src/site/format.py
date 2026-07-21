"""Display normalization for the public catalog (DATA_CONTRACT §4–§5).

REUSE: src/post/hashtags._normalise + the districts.yaml map. The map keys (Cyrillic
or Latinic) → a Latinic hashtag value. For *display* we want Cyrillic, so we resolve
raw → canonical Latinic value (same matching as district_tag minus "#"), then reverse-map
that value back to a Cyrillic key. Unmapped raw passes through unchanged.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Protocol

import yaml

from src.config import PROJECT_ROOT, get_settings
from src.post.hashtags import DISTRICTS_PATH, _normalise, district_tag

# Markers that mean "already rented" appearing in the source post / listing text.
_RENTED_MARKERS = ("снято", "izdato", "rented", "издато")
_CYRILLIC_RE = re.compile(r"[а-яё]", re.IGNORECASE)

# Правка Даши №4 (2026-07-18): на витрине только 10 городских районов.
# Порядок — алфавитный, как выдаёт district_options().
CORE_DISTRICTS: tuple[str, ...] = (
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

# Латинское значение микрорайона (из districts.yaml) → значение района-родителя.
# Схлопывание ТОЛЬКО для витрины сайта; districts.yaml и хэштеги канала не меняются.
_PARENT_VALUE: dict[str, str] = {
    "Dorcol": "Starigrad",
    "Centar": "Starigrad",
    "BezanijskaKosa": "NoviBeograd",
    "BeogradNaVodi": "Savskivenac",
    "Dedinje": "Savskivenac",
    "Senjak": "Savskivenac",
    "TopciderskoBrdo": "Savskivenac",
    "Slavija": "Vracar",
    "Karaburma": "Palilula",
    "BanovoBrdo": "Cukarica",
    "Cerak": "Cukarica",
    "FilmskiGrad": "Cukarica",
    "PetlovoBrdo": "Rakovica",
    "Gardos": "Zemun",
    "ProfesorskaKolonija": "Zvezdara",
    "Mirijevo": "Zvezdara",
    "Konjarnik": "Zvezdara",
}


def rooms_label(rooms: float | None) -> str:
    """listings.rooms (float) → display label. None/0 → 'студия'; N → 'N+1'."""
    if rooms is None or rooms == 0:
        return "студия"
    # Drop a trailing .0 so 2.0 → "2+1" while 1.5 stays "1.5+1".
    n: float | int = int(rooms) if rooms == int(rooms) else rooms
    return f"{n}+1"


# Process cache for the district map. NOT @lru_cache: a successful (non-empty) load is memoized
# for the process lifetime, but a FAILED load (districts.yaml missing/empty at first call) is
# never cached — so if the file lands after the first request the next call re-reads it instead
# of permanently rendering every район in Latinic (BUG: lru_cache would pin the empty {}).
_VALUE_TO_CYRILLIC: dict[str, str] | None = None


def _value_to_cyrillic() -> dict[str, str]:
    """Latinic district value → first Cyrillic key that maps to it (YAML order).

    hashtags caches a *normalised* map; we re-read the raw YAML here to keep the original
    (un-normalised) Cyrillic keys for display.
    """
    global _VALUE_TO_CYRILLIC
    if _VALUE_TO_CYRILLIC:
        return _VALUE_TO_CYRILLIC
    if not DISTRICTS_PATH.exists():
        return {}  # not cached — retry on the next call once the file exists
    raw = yaml.safe_load(DISTRICTS_PATH.read_text(encoding="utf-8")) or {}
    result: dict[str, str] = {}
    for key, value in raw.items():
        val = str(value)
        if val in result:
            continue
        if _CYRILLIC_RE.search(str(key)):
            result[val] = str(key)
    if result:
        _VALUE_TO_CYRILLIC = result
    return result


def human_district(raw: str | None) -> str:
    """Raw platform district → Cyrillic display name. Unmapped passes through unchanged."""
    if not raw:
        return ""
    tag = district_tag(raw)
    if tag is None:
        return raw  # unmapped — show as-is (logged elsewhere via districts-report)
    value = tag[1:]  # strip leading "#"
    value = _PARENT_VALUE.get(value, value)
    cyrillic = _value_to_cyrillic().get(value)
    return cyrillic if cyrillic is not None else value


class _HasText(Protocol):
    is_active: bool
    description: str | None
    title: str | None


def is_rented(listing: _HasText) -> bool:
    """True when the listing is inactive OR its text carries a 'rented' marker."""
    if not getattr(listing, "is_active", True):
        return True
    parts = [getattr(listing, "title", None), getattr(listing, "description", None)]
    blob = " ".join(p for p in parts if p)
    norm = _normalise(blob)  # lowercased, diacritics stripped — matches izdato/Издато etc.
    return any(marker in norm for marker in (_normalise(m) for m in _RENTED_MARKERS))


def public_photo_url(local_path: str) -> str:
    """Local photo file → public '/photos/<relpath>'.

    Never emits a remote URL, an absolute path, or a '..' traversal: a stray remote URL or
    any path that escapes SITE_PHOTOS_ROOT collapses to the basename. (Defense-in-depth —
    photo_paths come from the pipeline today, but this is the single point that builds a
    public URL from a stored path.)
    """
    if not local_path:
        return ""
    # A stray remote URL must never reach a public template: collapse to basename.
    if "://" in local_path:
        return "/photos/" + Path(local_path).name
    root = Path(get_settings().site_photos_root).resolve()
    p = Path(local_path)
    if not p.is_absolute():
        # Pipeline stores paths relative to the working dir, e.g. "data/inbound/1/13038/00.jpg";
        # anchor at PROJECT_ROOT so the part under site_photos_root can be computed.
        p = PROJECT_ROOT / p
    try:
        rel = p.resolve().relative_to(root)
    except ValueError:
        # Stored outside the configured root — fall back to basename, never leak path.
        rel = Path(p.name)
    rel_posix = rel.as_posix()
    # Never let an absolute escape or a '..' segment reach the URL even if photo_paths
    # ever became relative/tainted; Caddy also jails /photos, but build a safe URL anyway.
    if rel_posix.startswith("/") or ".." in rel_posix.split("/"):
        rel_posix = Path(local_path).name
    return "/photos/" + rel_posix
