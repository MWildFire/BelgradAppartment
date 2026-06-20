"""Publish-to-site helpers (Stage W · W2-A1).

Pure logic for turning a post's text into default ВНЖ / белый-картон flags.
Keyword lists are locked in docs/web/DECISIONS.md #2; the admin toggle always
overrides the parse, so this is only a first-guess default at publish time.
"""

from __future__ import annotations

import re
import unicodedata

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import SiteListing

# Raw keyword lists exactly as DECISIONS.md #2. Both the text and these keywords go
# through _normalise before substring-matching, so diacritics/case/script line up
# (e.g. Serbian "č/ž" → bare Latin, "белый" → "белыи", "регистрациј" → "регистраци").
_VNJ_RAW = (
    "ВНЖ",
    "боравак",
    "boravak",
    "привремени боравак",
    "privremeni boravak",
    "регистрациј",
    "registracij",
    "prijava boravka",
    "пријава боравка",
)

_WHITE_CARD_RAW = (
    "бели картон",
    "beli karton",
    "белый картон",
    "white card",
    "пријава адресе",
    "prijava adrese",
)


def _normalise(s: str) -> str:
    """Lowercase, strip diacritics, collapse separators.

    Mirrors src.post.hashtags._normalise but keeps both Latin and Cyrillic letters
    so keywords can be written in either script. Diacritic stripping decomposes
    Serbian "č/ć/š/ž" to bare Latin, making "boravak"/"beli karton" robust.
    """
    nfkd = unicodedata.normalize("NFKD", s)
    no_diacritics = "".join(c for c in nfkd if not unicodedata.combining(c))
    lowered = no_diacritics.lower()
    return re.sub(r"[^a-z0-9а-яё]+", " ", lowered).strip()


# Pre-normalise once; drop any that collapse to empty after normalisation.
_VNJ_KEYWORDS = tuple(filter(None, (_normalise(k) for k in _VNJ_RAW)))
_WHITE_CARD_KEYWORDS = tuple(filter(None, (_normalise(k) for k in _WHITE_CARD_RAW)))


def parse_relocation_flags(text: str | None) -> tuple[bool, bool]:
    """Return (is_for_vnj, is_for_white_card) from a post's raw text.

    Case-insensitive and diacritics-tolerant. Empty/None text → (False, False).
    The admin toggle overrides this default, so over-matching is the safer bias.
    """
    if not text:
        return (False, False)
    norm = _normalise(text)
    if not norm:
        return (False, False)
    is_vnj = any(kw in norm for kw in _VNJ_KEYWORDS)
    is_white_card = any(kw in norm for kw in _WHITE_CARD_KEYWORDS)
    return (is_vnj, is_white_card)


async def get_site_listing(session: AsyncSession, match_id: int) -> SiteListing | None:
    """Fetch the SiteListing overlay for a match (None if Дарья never exposed it)."""
    return (
        await session.execute(select(SiteListing).where(SiteListing.match_id == match_id))
    ).scalar_one_or_none()


async def get_or_create_site_listing(
    session: AsyncSession, match_id: int
) -> tuple[SiteListing, bool]:
    """Return (row, created). On create the row carries the DB defaults
    (is_public=True, flags False); never clobbers an existing row's fields.
    Caller must have validated that the match exists."""
    row = await get_site_listing(session, match_id)
    if row is not None:
        return row, False
    row = SiteListing(match_id=match_id)
    session.add(row)
    await session.flush()
    return row, True
