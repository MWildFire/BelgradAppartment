"""Extract price and area from the free-form inbound caption.

Inbound captions in @OtzRentBelgrad follow loose patterns:
- "600е в месяц", "Аренда 700€", "Цена: 550 €", "1100 евро"
- "66 м²", "45м2", "(54 м²)", "80m²"
Hashtag bands like "#до1500" give us a price bucket as a fallback when no exact figure appears.

Returns whatever can be confidently parsed — caller decides if both fields are required.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from src.post.hashtags import _districts_map, _normalise

PRICE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # "550 €", "700€", "1.200€"
    re.compile(r"(?<![\d.])(\d{2,3}(?:[.,]\d{3})?|\d{3,5})\s*€", re.IGNORECASE),
    # "600 евро", "1100 евро", "800евро"
    re.compile(r"(?<![\d.])(\d{2,3}(?:[.,]\d{3})?|\d{3,5})\s*евр[оa]", re.IGNORECASE),
    # "Аренда 700", "цена 550"
    re.compile(r"(?:аренда|цена|стоимост[ьи]|rent|price)[^\d\n]{0,15}(\d{3,5})", re.IGNORECASE),
    # Trailing "е" abbreviation: "600е в месяц", "550e", "1100е"
    re.compile(r"(?<![a-zA-Zа-яёА-ЯЁ])(\d{3,5})\s*[еe](?:\s*[в.\s/])", re.IGNORECASE),
)

# Price bucket from hashtag like #до1500 — last-resort upper bound.
PRICE_BUCKET = re.compile(r"#до\s*(\d{3,5})", re.IGNORECASE)

AREA_PATTERN = re.compile(
    r"(?<![\d.])(\d{1,3}(?:[.,]\d{1,2})?)\s*(?:м²|м2|m²|m2|кв\.?\s*м)",
    re.IGNORECASE,
)


@dataclass
class ParsedInbound:
    price_eur: int | None = None
    area_m2: float | None = None
    price_bucket_max: int | None = None  # e.g. 1500 from "#до1500"
    # Set of Latinic district-tag values mentioned anywhere in the caption.
    # Example: {"Vracar", "NoviBeograd"}. Empty if no recognisable district was found.
    districts: set[str] = field(default_factory=set)


def parse_inbound_text(text: str | None) -> ParsedInbound:
    if not text:
        return ParsedInbound()

    price: int | None = None
    for pat in PRICE_PATTERNS:
        for m in pat.finditer(text):
            raw = m.group(1).replace(",", "").replace(".", "").replace(" ", "")
            try:
                val = int(raw)
            except ValueError:
                continue
            # Sanity: realistic Belgrade monthly rents are 200–6000 EUR.
            if 200 <= val <= 8000:
                price = val
                break
        if price is not None:
            break

    bucket: int | None = None
    bm = PRICE_BUCKET.search(text)
    if bm:
        try:
            bucket = int(bm.group(1))
        except ValueError:
            pass

    area: float | None = None
    for m in AREA_PATTERN.finditer(text):
        raw = m.group(1).replace(",", ".")
        try:
            val = float(raw)
        except ValueError:
            continue
        # Realistic Belgrade apartments: 15–400 m².
        if 15 <= val <= 400:
            area = val
            break

    return ParsedInbound(
        price_eur=price,
        area_m2=area,
        price_bucket_max=bucket,
        districts=_extract_districts(text),
    )


def _extract_districts(text: str) -> set[str]:
    """Find Latinic district tags (e.g. 'Vracar', 'NoviBeograd') mentioned in inbound text.

    Algorithm:
    1. Split text into normalised tokens (lowercased, diacritics stripped).
    2. Slide a window of 1–3 tokens, look each window up in districts.yaml's reverse index.
    3. Collect every unique Latinic value found.
    """
    norm_text = _normalise(text)
    if not norm_text:
        return set()
    tokens = norm_text.split()
    m = _districts_map()
    hits: set[str] = set()
    for size in range(1, 4):
        for start in range(0, len(tokens) - size + 1):
            piece = " ".join(tokens[start:start + size])
            if piece in m:
                hits.add(m[piece])
    return hits
