"""Hashtag generation for published posts (SPEC §6.4).

Three sources combined:
- **Price bucket**: #до600, #до800, ..., #свыше3000.
- **District**: looked up in data/districts.yaml — matches Cyrillic OR Latinic, with or
  without diacritics. Returns Latinic form (e.g. "Звездара" → "#Zvezdara").
- **Apartment type**: from rooms (+ area for studio detection):
    1 комн, ≤30 м² → #студия
    1.5            → #евродвушка
    2.5            → #евротрешка
    rest          → #<rooms>k (e.g. #2k for dvosoban)
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DISTRICTS_PATH = PROJECT_ROOT / "data" / "districts.yaml"

PRICE_CAPS = (600, 800, 1000, 1200, 1500, 2000, 3000)


def price_bucket(price_eur: int | None) -> str | None:
    if price_eur is None or price_eur <= 0:
        return None
    for cap in PRICE_CAPS:
        if price_eur <= cap:
            return f"#до{cap}"
    return "#свыше3000"


def rooms_tag(rooms: float | None, area_m2: float | None = None) -> str | None:
    if rooms is None:
        return None
    if rooms == 1.0 and area_m2 is not None and area_m2 <= 30:
        return "#студия"
    if rooms == 1.5:
        return "#евродвушка"
    if rooms == 2.5:
        return "#евротрешка"
    # Integer-valued rooms → #2k, #3k…
    if rooms == int(rooms):
        n = int(rooms)
        if n >= 1:
            return f"#{n}k"
    return None


def _normalise(s: str) -> str:
    """Lowercase, strip diacritics, collapse separators — for fuzzy district lookup."""
    nfkd = unicodedata.normalize("NFKD", s)
    no_diacritics = "".join(c for c in nfkd if not unicodedata.combining(c))
    lowered = no_diacritics.lower()
    return re.sub(r"[^a-z0-9а-я]+", " ", lowered).strip()


@lru_cache(maxsize=1)
def _districts_map() -> dict[str, str]:
    """Read districts.yaml once and key it by normalised form."""
    if not DISTRICTS_PATH.exists():
        return {}
    raw = yaml.safe_load(DISTRICTS_PATH.read_text(encoding="utf-8")) or {}
    result: dict[str, str] = {}
    for key, value in raw.items():
        result[_normalise(str(key))] = str(value)
    return result


def district_tag(district: str | None) -> str | None:
    """Find a Latinic-form district tag. Tries the whole string, then individual words."""
    if not district:
        return None
    m = _districts_map()
    norm = _normalise(district)
    if not norm:
        return None
    if norm in m:
        return f"#{m[norm]}"
    # Try progressively longer prefixes and individual words.
    tokens = norm.split()
    # Multi-word substrings, longest first
    for size in range(min(len(tokens), 3), 0, -1):
        for start in range(0, len(tokens) - size + 1):
            piece = " ".join(tokens[start:start + size])
            if piece in m:
                return f"#{m[piece]}"
    return None


def build_hashtags(
    *,
    price_eur: int | None,
    rooms: float | None,
    area_m2: float | None,
    district: str | None,
) -> list[str]:
    """Assemble the final hashtag list in display order (district, price, type)."""
    tags: list[str] = []
    for tag in (district_tag(district), price_bucket(price_eur), rooms_tag(rooms, area_m2)):
        if tag and tag not in tags:
            tags.append(tag)
    return tags
