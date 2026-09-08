"""Shared apartment identity and source-presence facts.

Presence can keep an apartment available; absence alone does not authorize unpublishing.
Confirmed-dead evidence, pins, warn-only mode and removal caps belong to publication policy.
No publication rights are inferred from the membership or the global primary flag.
"""

from collections.abc import Iterable

ApartmentKey = tuple[str, int]


def apartment_key(listing_id: int, dup_group_id: int | None) -> ApartmentKey:
    """Keep singleton IDs distinct from duplicate-group IDs, even when numbers coincide."""
    return ("group", dup_group_id) if dup_group_id is not None else ("listing", listing_id)


def apartment_is_available(member_active: Iterable[bool]) -> bool:
    """A live source for any member establishes availability for the whole apartment."""
    return any(member_active)


def source_presence(
    *, is_active: bool, link_status: str | None = None,
    confirmed_absent: bool = False,
) -> str:
    """Inactive legacy flags are UNKNOWN; only explicit evidence establishes DEAD."""
    if is_active:
        return "ACTIVE"
    if link_status == "DEAD" or confirmed_absent:
        return "DEAD"
    return "INCONCLUSIVE"


def apartment_is_retained(member_statuses: Iterable[str], *, pinned: bool = False) -> bool:
    """Discovery retains pinned or uncertain apartments; only all-DEAD is rented.

    This is the availability half of publication policy. It grants no publishing rights;
    callers must independently scope overlays to is_public, and removal still uses W1 caps.
    """
    statuses = tuple(member_statuses)
    return pinned or not statuses or any(status != "DEAD" for status in statuses)
