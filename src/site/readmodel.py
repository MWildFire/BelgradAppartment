"""Read-model for the public catalog (DATA_CONTRACT §1, §3, §5).

Joins site_listings → matches → listings, and → inbound_posts (via matches.inbound_post_id)
for LOCAL photos + rented-marker text, and → published_posts for posted_at. Returns flat,
frozen SiteListingView / Facets — never ORM objects, never remote URLs.

District filtering happens in Python on the *display* (Cyrillic) value: many raw platform
districts collapse onto one human district, so post-filtering on the normalized label is both
correct and simpler than SQL on raw strings (the curated public set is small).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select

from src.config import get_settings
from src.db.models import InboundPost, Listing, Match, PublishedPost, SiteListing
from src.site import db as sitedb
from src.site.format import human_district, is_rented, public_photo_url, rooms_label


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


@dataclass
class Filters:
    vnj: bool = False
    wc: bool = False
    district: list[str] = field(default_factory=list)
    price_min: int | None = None
    price_max: int | None = None
    rooms: list[str] = field(default_factory=list)
    show_rented: bool = False  # C1: default view HIDES rented; «показать снятые» includes them


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
    sl: SiteListing, listing: Listing, post: InboundPost | None, published_at: datetime | None
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
        is_rented=is_rented(listing),
        photos=photos,
        note=sl.note,
        channel_url=sl.channel_post_url or settings.site_channel_url,
        posted_at=_iso(posted),
    )


async def _load_rows(database_url: str | None) -> list[_Row]:
    """All public rows, hydrated, with raw fields needed for in-Python filtering/sorting."""
    stmt = (
        select(SiteListing, Listing, InboundPost, PublishedPost.published_at)
        .join(Match, Match.id == SiteListing.match_id)
        .join(Listing, Listing.id == Match.listing_id)
        .join(InboundPost, InboundPost.id == Match.inbound_post_id, isouter=True)
        .join(PublishedPost, PublishedPost.match_id == Match.id, isouter=True)
        .where(SiteListing.is_public.is_(True))
        # A4 — one card per apartment: hide non-primary members of a cross-platform dup group.
        # Ungrouped listings (dup_group_id NULL) are always is_primary=True, so this only drops
        # the demoted duplicates and leaves the curated public set otherwise unchanged.
        .where(Listing.is_primary.is_(True))
    )
    rows: list[_Row] = []
    async with sitedb.read_session(database_url=database_url) as s:
        for sl, listing, post, published_at in (await s.execute(stmt)).all():
            view = _build_view(sl, listing, post, published_at)
            rows.append(
                _Row(view=view, rooms_raw=listing.rooms, posted_dt=published_at or sl.created_at)
            )
    return rows


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
    return listing_matches_filters(row.view, row.rooms_raw, f)


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
    rows = await _load_rows(database_url)
    for r in rows:
        if r.view.id == site_listing_id:
            return r.view
    return None


async def featured_one(*, database_url: str | None = None) -> SiteListingView | None:
    """The «квартира недели»: newest public+featured; else newest public; else None.

    Reuses the same hydration as the catalog (_load_rows builds full SiteListingViews),
    then picks within Python — the public set is small and we need the is_featured flag,
    which _Row doesn't carry, so we re-derive it from the source rows here.
    """
    stmt = (
        select(SiteListing.id, SiteListing.is_featured)
        .where(SiteListing.is_public.is_(True))
    )
    async with sitedb.read_session(database_url=database_url) as s:
        featured_ids = {
            sl_id for sl_id, is_featured in (await s.execute(stmt)).all() if is_featured
        }

    rows = await _load_rows(database_url)
    if not rows:
        return None
    # Newest first; a featured row outranks any non-featured one regardless of recency.
    rows.sort(key=lambda r: (r.view.id in featured_ids, r.posted_dt), reverse=True)
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
    same is_public/is_primary scoping). The matcher applies listing_matches_filters per
    subscription — the one shared predicate — so a subscription's «matches» is identical to
    what the catalog would show with the same filters.
    """
    rows = await _load_rows(database_url)
    # _Row.posted_dt is published_at or created_at; the SiteListing.created_at (when Дарья
    # exposed it) is the «new listing» signal we want for since_hours. Re-read it cheaply.
    stmt = select(SiteListing.id, SiteListing.created_at).where(SiteListing.is_public.is_(True))
    async with sitedb.read_session(database_url=database_url) as s:
        created_by_id = {sl_id: created for sl_id, created in (await s.execute(stmt)).all()}
    out: list[SubscriptionCandidate] = []
    for r in rows:
        created = created_by_id.get(r.view.id, r.posted_dt)
        out.append(
            SubscriptionCandidate(view=r.view, rooms_raw=r.rooms_raw, created_at=created)
        )
    return out


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
        points.append(
            {
                "id": v.id,
                "lat": v.lat,
                "lon": v.lon,
                "price_eur": v.price_eur,
                "rooms": v.rooms,
                "district": v.district,
                "for_vnj": v.for_vnj,
                "url": f"/apt/{v.id}",
            }
        )
    return points


async def public_ids(*, database_url: str | None = None) -> list[int]:
    """Ids of every public, primary listing — for the sitemap (no view hydration).

    The sitemap only needs ids, so this skips the full _load_rows join+hydration (price/photos/
    район normalization) that list_public(page_size=10000) used to do per request. Same is_public
    + is_primary scoping as the catalog, so private listings never surface.
    """
    stmt = (
        select(SiteListing.id)
        .join(Match, Match.id == SiteListing.match_id)
        .join(Listing, Listing.id == Match.listing_id)
        .where(SiteListing.is_public.is_(True))
        .where(Listing.is_primary.is_(True))
    )
    async with sitedb.read_session(database_url=database_url) as s:
        return [row_id for (row_id,) in (await s.execute(stmt)).all()]


async def similar(
    view: SiteListingView, limit: int = 4, *, database_url: str | None = None
) -> list[SiteListingView]:
    """Up to `limit` other listings in the same район, newest first.

    C1 invariant (rented hidden by default): unlike _load_rows, this drops rented listings so the
    «похожие» block never suggests an apartment that's already taken — matching every other public
    surface. Ordered by recency (newest posted first) instead of arbitrary DB row order.
    """
    rows = await _load_rows(database_url)
    candidates = [
        r
        for r in rows
        if r.view.district == view.district
        and r.view.id != view.id
        and not r.view.is_rented
    ]
    candidates.sort(key=lambda r: r.posted_dt, reverse=True)
    return [r.view for r in candidates[:limit]]
