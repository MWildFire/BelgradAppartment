"""Shared normalized parity contract for Nekretnine snapshot/detail comparisons."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from src.scrapers.base import ListingDetail

COMPARE_FIELDS = (
    "external_id",
    "url",
    "price_eur",
    "area_m2",
    "rooms",
    "district",
    "address_raw",
    "title",
    "lat",
    "lon",
    "photo_ids",
)


def photo_ids(urls: list[str]) -> list[str]:
    """Return stable photo IDs while ignoring URL presentation details."""
    result: list[str] = []
    for url in urls:
        path_parts = [part for part in urlparse(url).path.split("/") if part]
        if len(path_parts) >= 2 and path_parts[-2] == "image":
            result.append(path_parts[-1].split(".", 1)[0])
        else:
            result.append(path_parts[-1] if path_parts else "unknown")
    return result


def normalized(detail: ListingDetail) -> dict[str, Any]:
    return {
        "external_id": detail.external_id,
        "url": detail.url,
        "price_eur": detail.price_eur,
        "area_m2": detail.area_m2,
        "rooms": detail.rooms,
        "district": detail.district,
        "address_raw": detail.address_raw,
        "title": detail.title,
        "lat": detail.lat,
        "lon": detail.lon,
        "photo_ids": photo_ids(detail.photo_urls),
    }


def mismatch_fields(left: ListingDetail, right: ListingDetail) -> list[str]:
    left_values = normalized(left)
    right_values = normalized(right)
    return [field for field in COMPARE_FIELDS if left_values[field] != right_values[field]]
