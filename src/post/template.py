"""Build the published post text from a Listing + InboundPost (SPEC §6.4).

The template is fixed (Darya's wording); only the fillers vary. `availability_line` is best-effort:
we regex the inbound caption for "свободна с / free from / available from / ..." phrases.

Price / area precedence: the inbound caption is fresher than the listing snapshot (Дарья reposts
*after* the platform price may have shifted, and sometimes the platform listing is stale by days).
We trust caption numbers when they parse cleanly, and fall back to the listing's own values.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from src.db.models import InboundPost, Listing
from src.post.hashtags import build_hashtags
from src.post.location import yandex_maps_url

TEMPLATE = (
    "Цена: {price} евро + залог + комиссия риэлтора 50%\n"
    "Площадь: {area} м²"
    "{availability_block}"
    "{location_block}"
    "{hashtags_block}\n"
    "\n"
    "Если квартира, которая вам приглянулась, уже сдана или вы не нашли подходящий "
    "вариант в нашем канале, напишите @Darya_kralj, и мы обязательно подберем квартиру "
    "под ваши персональные предпочтения."
)


_AVAILABLE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # "Свободна с 1 июня", "свободна с 01.06.2026" — keep dots (date separators), stop at comma.
    re.compile(r"свободн[аоы]?\s*с\s+([^\n,;]{2,40})", re.IGNORECASE),
    re.compile(r"available\s+from\s+([^\n,;]{2,40})", re.IGNORECASE),
    re.compile(r"free\s+from\s+([^\n,;]{2,40})", re.IGNORECASE),
    re.compile(r"slobodan[a]?\s+od\s+([^\n,;]{2,40})", re.IGNORECASE),
)


@dataclass
class RenderedPost:
    text: str
    hashtags: list[str]
    availability: str | None


def extract_availability(text: str | None) -> str | None:
    if not text:
        return None
    for pat in _AVAILABLE_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(1).strip().rstrip(".").rstrip()
    return None


def render_post(
    listing: Listing,
    inbound: InboundPost | None = None,
    *,
    target_location_address: str | None = None,
) -> RenderedPost:
    # Lazy import — src.matching.inbound_parser depends on src.post.hashtags, and importing
    # parse_inbound_text at module load time creates a cycle through src/post/__init__.py.
    from src.matching.inbound_parser import parse_inbound_text

    parsed = parse_inbound_text(inbound.raw_text if inbound else None)

    # Caption beats listing for price/area — see module docstring.
    price_value = parsed.price_eur if parsed.price_eur is not None else listing.price_eur
    area_value = parsed.area_m2 if parsed.area_m2 is not None else listing.area_m2

    price = price_value if price_value is not None else "—"
    if isinstance(area_value, (int, float)):
        area = f"{float(area_value):g}"
    else:
        area = "—"

    availability = extract_availability(inbound.raw_text if inbound else None)
    availability_block = (
        f"\nДоступна с: {availability}" if availability else ""
    )

    addr_for_map = target_location_address or listing.address_raw
    map_url = yandex_maps_url(
        lat=listing.lat,
        lon=listing.lon,
        address=addr_for_map,
    )
    location_block = f"\nЛокация: {map_url}" if map_url else ""

    hashtags = build_hashtags(
        price_eur=price_value,
        rooms=listing.rooms,
        area_m2=area_value,
        district=listing.district,
    )
    # Brand tag for posts published via this pipeline — always last so it's the visible "channel"
    # marker if Telegram clips display.
    hashtags = [*hashtags, "#квартирыOtz"]
    hashtags_block = f"\n\n{' '.join(hashtags)}" if hashtags else ""

    text = TEMPLATE.format(
        price=price,
        area=area,
        availability_block=availability_block,
        location_block=location_block,
        hashtags_block=hashtags_block,
    )
    return RenderedPost(text=text, hashtags=hashtags, availability=availability)
