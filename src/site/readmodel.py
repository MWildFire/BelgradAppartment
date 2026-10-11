"""Read-model for the public catalog (DATA_CONTRACT §1, §3, §5).

Joins site_listings → matches → listings, and → inbound_posts (via matches.inbound_post_id)
for LOCAL photos, and → published_posts for posted_at. Availability uses the full dup-group.
Returns flat, frozen SiteListingView / Facets — never ORM objects, never remote URLs.

District filtering happens in Python on the *display* (Cyrillic) value: many raw platform
districts collapse onto one human district, so post-filtering on the normalized label is both
correct and simpler than SQL on raw strings (the curated public set is small).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import load_only

from src.config import get_settings
from src.db.models import InboundPost, Listing, Match, MatchStatus, PublishedPost, SiteListing
from src.site import db as sitedb
from src.site.availability import (
    ApartmentKey,
    apartment_availability,
    apartment_key,
    source_presence,
)
from src.site.format import human_district, is_rented, public_photo_url, rooms_label
from src.site.maps import fuzz_coords


@dataclass(frozen=True)
class SiteListingView:
    id: int
    price_eur: int
    area_m2: float | None
    rooms: str
    floor: str | None
    district: str
    address: str | None
    lat: float | None
    lon: float | None
    for_vnj: bool
    for_white_card: bool
    is_rented: bool
    photos: list[str]
    note: str | None
    channel_url: str
    posted_at: str
    availability: str = "active"


@dataclass
class Filters:
    vnj: bool = False
    wc: bool = False
    district: list[str] = field(default_factory=list)
    price_min: int | None = None
    price_max: int | None = None
    rooms: list[str] = field(default_factory=list)
    show_rented: bool = False  # Legacy catalog query; recommendation predicate still excludes DEAD.


@dataclass
class Facets:
    districts: list[tuple[str, int]]
    price_bounds: tuple[int, int]
    rooms: list[tuple[str, int]]
    counts: dict[str, int]


# --- internal row container (carries raw fields needed for filtering) ---


@dataclass
class _Row:
    view: SiteListingView
    rooms_raw: float | None
    posted_dt: datetime
    apartment: ApartmentKey
    created_at: datetime
    is_featured: bool
    legacy_visible: bool
    original_active: bool
    legacy_primary: bool
    source_status: str


@dataclass(frozen=True)
class SubscriptionCandidate:
    """A public listing as the C2 subscription matcher sees it: the view (for the predicate +
    digest body), its raw numeric rooms (predicate needs it — the view only carries the label),
    and the SiteListing.created_at used for the since_hours «only NEW listings» window."""

    view: SiteListingView
    rooms_raw: float | None
    created_at: datetime


def _iso(dt: datetime | None) -> str:
    return dt.isoformat() if dt is not None else ""


def _build_view(
    sl: SiteListing, listing: Listing, post: InboundPost | None, published_at: datetime | None,
    *, availability: str,
) -> SiteListingView:
    settings = get_settings()
    photos = [public_photo_url(p) for p in (post.photo_paths if post else []) if p]
    posted = published_at or sl.created_at
    return SiteListingView(
        id=sl.id,
        price_eur=listing.price_eur or 0,
        area_m2=listing.area_m2,
        rooms=rooms_label(listing.rooms),
        floor=None,
        district=human_district(listing.district),
        address=None,  # district-only MVP (DECISIONS #1)
        lat=listing.lat,
        lon=listing.lon,
        for_vnj=sl.is_for_vnj,
        for_white_card=sl.is_for_white_card,
        is_rented=availability == "dead",
        availability=availability,
        photos=photos,
        note=sl.note,
        channel_url=sl.channel_post_url or settings.site_channel_url,
        posted_at=_iso(posted),
    )


def _representatives(rows: list[_Row]) -> list[_Row]:
    """Oldest authorized overlay wins; a scraper primary re-election cannot change its URL.

    Select before filtering, so catalog, subscriptions and sitemap agree on one identity.
    Secondary public overlays remain independently accessible through their existing URLs.
    """
    selected: dict[ApartmentKey, _Row] = {}
    for row in sorted(rows, key=lambda r: r.view.id):
        selected.setdefault(row.apartment, row)
    return list(selected.values())


async def _load_rows(
    database_url: str | None, *, representatives_only: bool = True
) -> list[_Row]:
    """Hydrate authorized overlays and resolve availability from all apartment members.

    Private siblings contribute only their activity bit. All display fields and media remain
    those of the selected public overlay and its original match; global primary is irrelevant.
    """
    stmt = (
        select(SiteListing, Listing, InboundPost, PublishedPost.published_at)
        .options(
            # Public display and the legacy comparison need only these scalars. Avoid
            # decoding scraper payloads and copying CLIP blobs for every public card.
            load_only(
                Listing.price_eur, Listing.area_m2, Listing.rooms, Listing.district,
                Listing.lat, Listing.lon, Listing.dup_group_id, Listing.is_active,
                Listing.link_status, Listing.deactivated_at, Listing.absence_streak,
                Listing.is_primary, Listing.title, Listing.description, raiseload=True,
            ),
            load_only(InboundPost.photo_paths, raiseload=True),
        )
        .join(Match, Match.id == SiteListing.match_id)
        .join(Listing, Listing.id == Match.listing_id)
        .join(InboundPost, InboundPost.id == Match.inbound_post_id, isouter=True)
        .join(PublishedPost, PublishedPost.match_id == Match.id, isouter=True)
        # A rejection is terminal: a rejected match is never public, on the site or to subscribers.
        .where(SiteListing.is_public.is_(True), Match.status != MatchStatus.REJECTED)
    )
    rows: list[_Row] = []
    async with sitedb.read_session(database_url=database_url) as s:
        public = (await s.execute(stmt)).all()
        group_ids = {listing.dup_group_id for _, listing, _, _ in public
                     if listing.dup_group_id is not None}
        statuses_by_group: dict[int, list[str]] = {}
        if group_ids:
            members = await s.execute(
                select(Listing.dup_group_id, Listing.is_active, Listing.link_status,
                       Listing.deactivated_at, Listing.absence_streak)
                .where(Listing.dup_group_id.in_(group_ids))
            )
            for group_id, active, link_status, deactivated_at, absence_streak in members.all():
                statuses_by_group.setdefault(group_id, []).append(source_presence(
                    is_active=active, link_status=link_status,
                    confirmed_absent=deactivated_at is not None and (absence_streak or 0) >= 2,
                ))
        for sl, listing, post, published_at in public:
            original_status = source_presence(
                is_active=listing.is_active, link_status=listing.link_status,
                confirmed_absent=listing.deactivated_at is not None
                and (listing.absence_streak or 0) >= 2,
            )
            statuses = statuses_by_group.get(listing.dup_group_id, [original_status])
            key = apartment_key(listing.id, listing.dup_group_id)
            view = _build_view(
                sl, listing, post, published_at,
                availability=apartment_availability(statuses),
            )
            rows.append(_Row(
                view=view,
                rooms_raw=listing.rooms,
                posted_dt=published_at or sl.created_at,
                apartment=key,
                created_at=sl.created_at,
                is_featured=sl.is_featured,
                legacy_visible=listing.is_primary and not is_rented(listing),
                original_active=listing.is_active,
                legacy_primary=listing.is_primary,
                source_status=original_status,
            ))
    return _representatives(rows) if representatives_only else rows


def _rooms_matches(rooms_raw: float | None, wanted: list[str]) -> bool:
    wanted = [w for w in wanted if w.strip()]  # blank entries = no filter (defensive)
    if not wanted:
        return True
    r = rooms_raw
    for w in wanted:
        if w == "studio" and (r is None or r == 0):
            return True
        if w == "3+" and r is not None and r >= 3:
            return True
        try:
            if r is not None and r == float(w):
                return True
        except ValueError:
            continue
    return False


def listing_matches_filters(view: SiteListingView, rooms_raw: float | None, f: Filters) -> bool:
    """ONE listing-matches-Filters predicate, shared by the catalog and C2 subscriptions.

    The catalog filter (`_passes`) and the subscription matcher both call this, so a listing is
    judged against criteria by exactly one implementation — refactoring the predicate (or fixing
    a matching bug) updates both call sites at once. `rooms_raw` is the listing's numeric rooms
    value (SiteListingView only carries the display label, so the raw value is passed alongside).
    """
    # C1: rented listings are hidden by default; «показать снятые» (show_rented) reveals them.
    # Subscriptions create Filters with show_rented=False, so a rented listing never notifies.
    if view.is_rented and not f.show_rented:
        return False
    if f.vnj and not view.for_vnj:
        return False
    if f.wc and not view.for_white_card:
        return False
    if f.district and view.district not in f.district:
        return False
    if f.price_min is not None and view.price_eur < f.price_min:
        return False
    if f.price_max is not None and view.price_eur > f.price_max:
        return False
    return _rooms_matches(rooms_raw, f.rooms)


def _passes(row: _Row, f: Filters) -> bool:
    # Only W1 publication policy can remove an authorized overlay. The legacy show_rented
    # query remains accepted, but viewing retained public cards is distinct from recommending
    # a dead apartment to a subscriber (which still uses the stricter shared predicate).
    return listing_matches_filters(row.view, row.rooms_raw, replace(f, show_rented=True))


def _sort_key(sort: str):  # type: ignore[no-untyped-def]
    """Composite sort key: rented ALWAYS sinks below active (C1), then the chosen order.

    Returned as a single ascending key (no `reverse` flag) so the rented-to-bottom rule can't
    be flipped by a descending price sort: rented rows (is_rented True -> 1) trail active ones
    (0), and within each group the secondary key applies. Descending orders negate the numeric
    value; "new" negates the posix timestamp so the newest sorts first.
    """
    if sort == "price_asc":
        return lambda r: (r.view.is_rented, r.view.price_eur)
    if sort == "price_desc":
        return lambda r: (r.view.is_rented, -r.view.price_eur)
    # default "new": newest posted first (negate timestamp for descending within ascending sort).
    return lambda r: (r.view.is_rented, -r.posted_dt.timestamp())


async def list_public(
    filters: Filters,
    sort: str = "new",
    page: int = 1,
    page_size: int | None = None,
    *,
    database_url: str | None = None,
) -> tuple[list[SiteListingView], int]:
    page_size = page_size or get_settings().site_page_size
    rows = [r for r in await _load_rows(database_url) if _passes(r, filters)]
    rows.sort(key=_sort_key(sort))
    total = len(rows)
    start = max(page - 1, 0) * page_size
    page_rows = rows[start:start + page_size]
    return [r.view for r in page_rows], total


async def get_public(
    site_listing_id: int, *, database_url: str | None = None
) -> SiteListingView | None:
    rows = await _load_rows(database_url, representatives_only=False)
    for r in rows:
        if r.view.id == site_listing_id:
            return r.view
    return None


async def featured_one(*, database_url: str | None = None) -> SiteListingView | None:
    """Newest featured authorized representative, with its truthful availability badge."""
    rows = await _load_rows(database_url)
    if not rows:
        return None
    rows.sort(key=lambda r: (r.is_featured, r.posted_dt), reverse=True)
    return rows[0].view


async def apartment_summary(
    site_listing_id: int, *, database_url: str | None = None
) -> dict[str, object] | None:
    """Compact apartment summary for the lead e-mail/notice — None if missing or not public.

    Privacy: _load_rows is already scoped to is_public, so a private/missing id yields None.
    """
    view = await get_public(site_listing_id, database_url=database_url)
    if view is None:
        return None
    return {
        "id": view.id,
        "price_eur": view.price_eur,
        "district": view.district,
        "area_m2": view.area_m2,
        "url": f"/apt/{view.id}",
    }


async def public_candidates(
    *, database_url: str | None = None
) -> list[SubscriptionCandidate]:
    """All public listings as C2 subscription candidates (view + raw rooms + created_at).

    Reuses _load_rows so the candidate set is EXACTLY the catalog's public set (same joins,
    same public representative selection). The matcher applies listing_matches_filters per
    subscription — the one shared predicate — so a subscription's «matches» is identical to
    what the catalog would show with the same filters.
    """
    return [
        SubscriptionCandidate(view=r.view, rooms_raw=r.rooms_raw, created_at=r.created_at)
        for r in await _load_rows(database_url)
        if not r.view.is_rented
    ]


async def facets(*, database_url: str | None = None) -> Facets:
    rows = await _load_rows(database_url)
    district_counts: dict[str, int] = {}
    rooms_counts: dict[str, int] = {}
    prices: list[int] = []
    vnj = wc = 0
    for r in rows:
        v = r.view
        if v.district:
            district_counts[v.district] = district_counts.get(v.district, 0) + 1
        rooms_counts[v.rooms] = rooms_counts.get(v.rooms, 0) + 1
        if v.price_eur:
            prices.append(v.price_eur)
        vnj += int(v.for_vnj)
        wc += int(v.for_white_card)
    districts = sorted(district_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    rooms = sorted(rooms_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    bounds = (min(prices), max(prices)) if prices else (0, 0)
    return Facets(
        districts=districts,
        price_bounds=bounds,
        rooms=rooms,
        counts={"vnj": vnj, "white_card": wc, "total": len(rows)},
    )


async def map_points(*, database_url: str | None = None) -> list[dict[str, object]]:
    rows = await _load_rows(database_url)
    points: list[dict[str, object]] = []
    for r in rows:
        v = r.view
        if v.lat is None or v.lon is None:
            continue
        # ФУЗЗИ/D1 (T1-1, §M2): snap to the ~100m grid BEFORE serialization — exact building
        # coords must never leave the DB through /api/map.json (Wave-0 диагностика caught them
        # bit-for-bit in prod). Client-side clustering groups the already-snapped points, so
        # this one seam covers pins, clusters and popups alike.
        flat, flon = fuzz_coords(v.lat, v.lon)
        points.append(
            {
                "id": v.id,
                "lat": flat,
                "lon": flon,
                "price_eur": v.price_eur,
                "rooms": v.rooms,
                "district": v.district,
                "for_vnj": v.for_vnj,
                "url": f"/apt/{v.id}",
                "availability": v.availability,
            }
        )
    return points


async def public_ids(*, database_url: str | None = None) -> list[int]:
    """Authorized public apartment representatives for the sitemap, matching the catalog."""
    return [r.view.id for r in await _load_rows(database_url)]


async def public_selection_comparison(*, database_url: str) -> dict[str, object]:
    """Sanitized legacy/current comparison on an explicitly supplied database copy.

    Uses the OS-enforced read-only engine. It never probes sources or sends notifications,
    and returns public overlay IDs and counts only, without source URLs, media or personal data.
    """
    rows = await _load_rows(database_url, representatives_only=False)
    before = [r for r in rows if r.legacy_visible]
    after = _representatives(rows)
    before_groups = {r.apartment for r in before}
    after_groups = {r.apartment for r in after}

    def reason(row: _Row) -> str:
        if row.view.availability == "dead":
            return "dead_retained_by_public_policy"
        if row.view.availability == "unknown":
            return "unknown_retained"
        if not row.original_active:
            return "active_sibling"
        return "public_secondary" if not row.legacy_primary else "source_activity"

    newly_visible = [
        {"site_listing_id": r.view.id, "reason": reason(r)}
        for r in after if r.apartment not in before_groups
    ]
    secondary_active = [r for r in rows if r.original_active and not r.legacy_primary
                        and r.apartment not in before_groups]
    return {
        "no_db_writes": True,
        "no_telegram_sends": True,
        "before": {"cards": len(before), "unique_apartments": len(before_groups)},
        "after": {"cards": len(after), "unique_apartments": len(after_groups)},
        "newly_visible": newly_visible,
        "newly_visible_reasons": dict(Counter(item["reason"] for item in newly_visible)),
        "no_longer_visible_apartments": len(before_groups - after_groups),
        "representative_ids": [r.view.id for r in after],
        "public_direct_ids": sorted({r.view.id for r in rows}),
        "active_secondary_cards": len(secondary_active),
        "active_secondary_apartments": len({r.apartment for r in secondary_active}),
        "available_via_sibling_cards": sum(
            not r.original_active and r.view.availability == "active" for r in rows
        ),
        "unknown_retained_cards": sum(r.view.availability == "unknown" for r in rows),
        "dead_retained_cards": sum(r.view.availability == "dead" for r in rows),
    }



async def similar(
    view: SiteListingView, limit: int = 4, *, database_url: str | None = None
) -> list[SiteListingView]:
    """Up to `limit` other listings in the same район, newest first.

    C1 invariant (rented hidden by default): unlike _load_rows, this drops rented listings so the
    «похожие» block never suggests an apartment that's already taken — matching every other public
    surface. Ordered by recency (newest posted first) instead of arbitrary DB row order.
    """
    all_rows = await _load_rows(database_url, representatives_only=False)
    apartment = next((r.apartment for r in all_rows if r.view.id == view.id), None)
    rows = _representatives(all_rows)
    candidates = [
        r
        for r in rows
        if r.view.district == view.district
        and r.view.id != view.id
        and r.apartment != apartment
        and not r.view.is_rented
    ]
    candidates.sort(key=lambda r: r.posted_dt, reverse=True)
    return [r.view for r in candidates[:limit]]
