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
