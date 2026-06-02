"""Parse digest posts from @serbia4rent — one Telegram message with N apartment shares.

A digest post has:
- A header "Доступны квартиры для просмотра:"
- Multiple section titles like "Однокомнатные и студии:", "Полуторакомнатные:", ...
- Per-section lines, each with an inline link to a YoNote share:
    "Звездара (Джерам)👖 #O4047 - 450€"

The line markup uses MessageEntityTextUrl whose `url` points at *.yonote.ru/share/<uuid>.
We extract one `DigestLine` per YoNote link with: the share id, the room-type category (carried
forward from the most recent bold heading), the visible line text, plus parsed district / price /
"rented" flag.

Telethon entity offsets are UTF-16-code-unit indices into the message text, NOT Python str
indices. We work on `add_surrogate(text)` so the offsets line up, and `del_surrogate` the slice
back to a normal Python string at the end.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from telethon.helpers import add_surrogate, del_surrogate
from telethon.tl.custom.message import Message
from telethon.tl.types import (
    MessageEntityBold,
    MessageEntityTextUrl,
)

from src.integrations.yonote import parse_share_url

DIGEST_HEADER_RE = re.compile(r"доступны\s+квартиры", re.IGNORECASE)

# Section header → rooms-bucket. Headings come from the source ("Однокомнатные и студии",
# "Полуторакомнатные", "Двухкомнатные", "Трёхкомнатные", "Четырёхкомнатные", "Пятикомнатные",
# and any future "N-комнатные"). The bucket label is what we feed downstream (parser/hashtags).
_SECTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"одноком|студи", re.IGNORECASE), "1k"),
    (re.compile(r"полуторакомнатн", re.IGNORECASE), "1.5k"),
    (re.compile(r"двухкомнатн", re.IGNORECASE), "2k"),
    (re.compile(r"трёхкомнатн|трехкомнатн", re.IGNORECASE), "3k"),
    (re.compile(r"четырёхкомнатн|четырехкомнатн", re.IGNORECASE), "4k"),
    (re.compile(r"пятикомнатн", re.IGNORECASE), "5k"),
)

# Per-line patterns. The agency line shape is:
#   <District>[ (<Sub>[, <Sub2>])][. <Feature>...][<emoji>] [#<InternalId>] [- <price>€]
# When a flat is already taken the price is replaced by "(СДАНА)".
PRICE_RE = re.compile(r"(\d{2,4})\s*€")
INTERNAL_ID_RE = re.compile(r"#([A-Z][A-Za-z0-9]+)")
RENTED_RE = re.compile(r"\(\s*СДАНА\s*\)", re.IGNORECASE)


@dataclass(slots=True)
class DigestLine:
    """One apartment row inside a digest post."""

    yonote_url: str
    share_id: str
    yonote_team: str
    raw_line: str
    room_type: str | None  # "1k" / "1.5k" / "2k" / "3k" / "4k" / "5k" / None
    internal_id: str | None  # e.g. "O4047", "F4046PG"
    district_ru: str | None  # russian-language district name as written ("Звездара")
    subdistrict_ru: str | None  # neighbourhood inside () — "Джерам"
    features: list[str] = field(default_factory=list)  # ["Паркинг", "Pet-friendly", ...]
    price_eur: int | None = None
    is_rented: bool = False


def is_digest_post(msg: Message) -> bool:
    """True if the message looks like a serbia4rent digest (≥2 YoNote share links)."""
    if not msg.entities:
        return False
    yonote_count = 0
    for e in msg.entities:
        if isinstance(e, MessageEntityTextUrl) and parse_share_url(e.url or "") is not None:
            yonote_count += 1
            if yonote_count >= 2:
                return True
    return False


def parse_digest(msg: Message) -> list[DigestLine]:
    """Extract one DigestLine per YoNote share link in the message.

    Carries the most recent bold-section heading forward as the room-type bucket.
    Skips links whose row contains "(СДАНА)" — those apartments are already rented and
    not worth scraping.
    """
    raw = msg.message or ""
    if not raw or not msg.entities:
        return []

    surrogate_text = add_surrogate(raw)
    # Sort entities by offset to walk in display order.
    ordered = sorted(msg.entities, key=lambda e: e.offset)

    current_bucket: str | None = None
    lines: list[DigestLine] = []

    for entity in ordered:
        if isinstance(entity, MessageEntityBold):
            # Section heading is a bold span; the line itself often spills past the entity
            # ("олуторакомнатные:" + bold cuts the first char off due to UTF-16 indexing
            # quirks from the source) so we read the whole containing line to classify.
            heading_line = _line_at(surrogate_text, entity.offset)
            bucket = _classify_section(heading_line)
            if bucket is not None:
                current_bucket = bucket
            continue

        if not isinstance(entity, MessageEntityTextUrl):
            continue
        parsed_url = parse_share_url(entity.url or "")
        if parsed_url is None:
            continue
        team, share_id = parsed_url

        # The visible line containing this hyperlink — gives us district, price, features.
        line_text = _line_at(surrogate_text, entity.offset)
        if RENTED_RE.search(line_text):
            continue  # already taken — skip

        district_ru, subdistrict_ru, features = _parse_line_structure(line_text)
        lines.append(
            DigestLine(
                yonote_url=entity.url or "",
                share_id=share_id,
                yonote_team=team,
                raw_line=line_text,
                room_type=current_bucket,
                internal_id=_extract(INTERNAL_ID_RE, line_text),
                district_ru=district_ru,
                subdistrict_ru=subdistrict_ru,
                features=features,
                price_eur=_extract_int(PRICE_RE, line_text),
                is_rented=False,
            )
        )

    return lines


# --------------------------------------------------------------------------- internals


def _line_at(surrogate_text: str, offset: int) -> str:
    """Return the full text line containing UTF-16 `offset`, decoded back to Python str."""
    start = surrogate_text.rfind("\n", 0, offset)
    end = surrogate_text.find("\n", offset)
    if start == -1:
        start = 0
    else:
        start += 1
    if end == -1:
        end = len(surrogate_text)
    return del_surrogate(surrogate_text[start:end]).strip()


def _classify_section(heading: str) -> str | None:
    for pat, bucket in _SECTIONS:
        if pat.search(heading):
            return bucket
    return None


def _parse_line_structure(line: str) -> tuple[str | None, str | None, list[str]]:
    """Pull district / subdistrict / features from a digest row.

    Strategy: cut everything from the first "#" onwards (that's the internal ID block), then
    split the remainder on ".". Token 0 is "<District> [(<Sub>[, <Sub2>])]"; subsequent
    tokens are features ("Паркинг", "Pet-friendly", "Полное оснащение", ...).
    """
    head = line.split("#", 1)[0]
    parts = [p for p in (s.strip() for s in _split_outside_parens(head, ".")) if p]
    if not parts:
        return None, None, []

    district_block = parts[0]
    district_ru: str | None
    subdistrict_ru: str | None
    if "(" in district_block and ")" in district_block:
        district_ru = district_block.split("(", 1)[0].strip() or None
        subdistrict_ru = district_block[district_block.find("(") + 1 : district_block.rfind(")")].strip() or None
    else:
        district_ru = district_block or None
        subdistrict_ru = None

    if district_ru:
        # Drop any trailing emoji glued onto the district name (e.g. "Звездара🧥" → "Звездара").
        district_ru = _strip_trailing_emoji(district_ru)
    if subdistrict_ru:
        subdistrict_ru = _strip_trailing_emoji(subdistrict_ru)

    features = [_strip_trailing_emoji(p) for p in parts[1:] if p]
    return district_ru, subdistrict_ru, features


def _split_outside_parens(text: str, sep: str) -> list[str]:
    """Split on `sep` only when the cursor is outside (...) — preserves things like 'Храм св. Савы'."""
    out: list[str] = []
    buf: list[str] = []
    depth = 0
    for ch in text:
        if ch == "(":
            depth += 1
            buf.append(ch)
        elif ch == ")":
            depth = max(0, depth - 1)
            buf.append(ch)
        elif ch == sep and depth == 0:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        out.append("".join(buf))
    return out


_EMOJI_OR_VS_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"  # most emoji blocks
    "\U00002600-\U000027BF"  # misc symbols + dingbats
    "\U0000FE00-\U0000FE0F"  # variation selectors
    "\U0001F1E6-\U0001F1FF"  # regional indicators
    "‍"                # ZWJ
    "]+",
    re.UNICODE,
)


def _strip_trailing_emoji(s: str) -> str:
    return _EMOJI_OR_VS_RE.sub("", s).strip()


def _extract(pat: re.Pattern[str], text: str) -> str | None:
    m = pat.search(text)
    return m.group(1) if m else None


def _extract_int(pat: re.Pattern[str], text: str) -> int | None:
    m = pat.search(text)
    if not m:
        return None
    try:
        return int(m.group(1))
    except (ValueError, IndexError):
        return None
