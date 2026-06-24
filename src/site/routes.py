"""Public catalog routes (DATA_CONTRACT §2 — exact template names + context keys).

Every catalog/facets/map read goes through the in-process TTL cache (src/site/cache.py),
keyed by the normalized query params. POST /lead and /healthz are never cached.

The DB url for reads is injected via Depends(catalog_db_url) — the test seam. Templates live
on the module-level `templates` object so tests can swap it for a DictLoader stub and assert
the selected template + context without depending on agent D's real markup.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from src.config import get_settings
from src.site import cache, og_image, readmodel
from src.site.deps import (
    SITE_CSRF_TOKEN,
    catalog_db_url,
    enforce_lead_ratelimit,
    require_site_csrf,
)
from src.site.leads import LeadPayload, LeadValidationError, create_lead
from src.site.maps import fuzz_coords, maps_links
from src.site.readmodel import Filters
from src.site.subscriptions import (
    SubscriptionPayload,
    SubscriptionValidationError,
    create_subscription,
    unsubscribe,
)

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["csrf_token"] = SITE_CSRF_TOKEN
templates.env.globals["site_base_url"] = get_settings().site_public_base_url

router = APIRouter()

# How many freshest listings the homepage features.
FEATURED_N = 6


# --------------------------------------------------------------------------- helpers


def _parse_filters(request: Request) -> tuple[Filters, str, int]:
    """Parse §3 query params → (Filters, sort, page). Multivalued: district, rooms."""
    qp = request.query_params
    price_min = _as_int(qp.get("price_min"))
    price_max = _as_int(qp.get("price_max"))
    page = _as_int(qp.get("page")) or 1
    sort = qp.get("sort") or "new"
    filters = Filters(
        vnj=qp.get("vnj") == "1",
        wc=qp.get("wc") == "1",
        # The hero/filters forms submit empty district=/rooms= for unfilled fields; an empty
        # string is "no filter", not a value that matches nothing — strip blanks here.
        district=[d for d in qp.getlist("district") if d.strip()],
        price_min=price_min,
        price_max=price_max,
        rooms=[r for r in qp.getlist("rooms") if r.strip()],
        show_rented=qp.get("show_rented") == "1",
    )
    return filters, sort, max(page, 1)


def _xml_escape(text: str) -> str:
    """Minimal XML-text escaping for <loc> values (defense-in-depth; site paths are clean)."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _as_int(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _static_page(request: Request, template: str) -> Response:
    """C3: render a genuinely-static content page WITH an ETag + conditional 304 short-circuit.

    Only the static content pages (/vnj, /how, /calculator) route through here — their bodies
    don't depend on DB state or query params, so a content-derived ETag is safe. The ETag is a
    weak validator over the rendered bytes (changes iff the rendered HTML changes — a template
    edit or an asset-version bump that alters /static URLs both move it). When the client sends
    a matching If-None-Match we return 304 with no body; otherwise the full 200 carries the ETag.

    Dynamic routes (/, /catalog, /lead, …) deliberately do NOT use this — a static ETag there
    would let a client cache one filter/DB state and miss later changes.
    """
    rendered = templates.TemplateResponse(request, template, {})
    etag = 'W/"' + hashlib.sha1(rendered.body).hexdigest()[:16] + '"'
    if _etag_matches(request.headers.get("if-none-match"), etag):
        # 304 carries no body; echo the validator so the client keeps caching.
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers={"ETag": etag})
    rendered.headers["ETag"] = etag
    return rendered


def _etag_matches(if_none_match: str | None, etag: str) -> bool:
    """True when an If-None-Match header matches our ETag (RFC 7232 weak comparison).

    Honors a comma-separated list and the "*" wildcard; ignores the weak "W/" prefix on each
    candidate so a weak/strong mismatch on the same opaque tag still counts as a match.
    """
    if not if_none_match:
        return False
    target = etag[2:] if etag.startswith("W/") else etag
    for candidate in if_none_match.split(","):
        c = candidate.strip()
        if c == "*":
            return True
        if (c[2:] if c.startswith("W/") else c) == target:
            return True
    return False


def _catalog_key(request: Request) -> tuple:  # type: ignore[type-arg]
    """Normalized cache key from the catalog query params (order/empty-insensitive)."""
    qp = request.query_params
    return cache.normalize_catalog_key(
        {
            "vnj": qp.get("vnj"),
            "wc": qp.get("wc"),
            "district": qp.getlist("district"),
            "price_min": qp.get("price_min"),
            "price_max": qp.get("price_max"),
            "rooms": qp.getlist("rooms"),
            "sort": qp.get("sort"),
            "page": qp.get("page"),
            "show_rented": qp.get("show_rented"),
        }
    )


async def _cached_catalog(
    request: Request, db_url: str
) -> tuple[list[readmodel.SiteListingView], int]:
    key = _catalog_key(request)
    cached = cache.catalog_cache.get(key)
    if cached is not cache.MISS:
        return cached
    filters, sort, page = _parse_filters(request)
    result = await readmodel.list_public(filters, sort=sort, page=page, database_url=db_url)
    cache.catalog_cache.set(key, result)
    return result


async def _cached_facets(db_url: str) -> readmodel.Facets:
    key = ("facets",)
    cached = cache.facets_cache.get(key)
    if cached is not cache.MISS:
        return cached
    result = await readmodel.facets(database_url=db_url)
    cache.facets_cache.set(key, result)
    return result


async def _cached_map(db_url: str) -> list[dict[str, object]]:
    key = ("map",)
    cached = cache.map_cache.get(key)
    if cached is not cache.MISS:
        return cached
    result = await readmodel.map_points(database_url=db_url)
    cache.map_cache.set(key, result)
    return result


def _applied(request: Request) -> dict[str, object]:
    """Echo back the applied filters so templates can render active state / deep-links."""
    qp = request.query_params
    return {
        "vnj": qp.get("vnj") == "1",
        "wc": qp.get("wc") == "1",
        "district": qp.getlist("district"),
        "price_min": _as_int(qp.get("price_min")),
        "price_max": _as_int(qp.get("price_max")),
        "rooms": qp.getlist("rooms"),
        "sort": qp.get("sort") or "new",
        "page": _as_int(qp.get("page")) or 1,
        "show_rented": qp.get("show_rented") == "1",
    }


# Filter params that must survive a page jump (everything except `page` itself).
_PAGER_PRESERVE = ("vnj", "wc", "district", "price_min", "price_max", "rooms", "sort", "show_rented")


def _qs_for_page(request: Request, page: int) -> str:
    """Query-string for ?page=N that re-emits the current filters (multivalued-safe)."""
    qp = request.query_params
    pairs: list[tuple[str, str]] = []
    for key in _PAGER_PRESERVE:
        for val in qp.getlist(key):
            if val != "":
                pairs.append((key, val))
    pairs.append(("page", str(page)))
    return urlencode(pairs)


def _pager(request: Request, total: int, page: int) -> dict[str, object]:
    """Pagination context: bounds + prev/next query-strings preserving active filters.

    qs_prev/qs_next are built independent of the bounds (has_prev/has_next gate rendering),
    so deep-links + HTMX hx-get can reuse them without recomputing the filter set.
    """
    page_size = get_settings().site_page_size
    total_pages = max((total + page_size - 1) // page_size, 1)
    return {
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
        "has_prev": page > 1,
        "has_next": page < total_pages,
        "qs_prev": _qs_for_page(request, page - 1),
        "qs_next": _qs_for_page(request, page + 1),
    }


# --------------------------------------------------------------------------- routes


@router.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"status": "ok"})


@router.get("/", response_class=HTMLResponse)
async def index(request: Request, db_url: str = Depends(catalog_db_url)) -> HTMLResponse:
    items, _total = await readmodel.list_public(
        Filters(), sort="new", page=1, page_size=FEATURED_N, database_url=db_url
    )
    # «Квартира недели»: a single curated apartment for the hero side-card (W3-2).
    week = await readmodel.featured_one(database_url=db_url)
    facets = await _cached_facets(db_url)
    stats = {
        "active": facets.counts.get("total", 0),
        "districts": len(facets.districts),
        "vnj": facets.counts.get("vnj", 0),
        "white_card": facets.counts.get("white_card", 0),
    }
    return templates.TemplateResponse(
        request, "index.html", {"featured": items, "week": week, "stats": stats}
    )


@router.get("/catalog", response_class=HTMLResponse)
async def catalog(request: Request, db_url: str = Depends(catalog_db_url)) -> HTMLResponse:
    items, total = await _cached_catalog(request, db_url)
    facets = await _cached_facets(db_url)
    applied = _applied(request)
    context = {
        "items": items,
        "facets": facets,
        "applied": applied,
        "total": total,
        "pager": _pager(request, total, applied["page"]),
    }
    # HTMX swaps only the results grid + pagination; full-page request gets the shell.
    template = "partials/_results.html" if "hx-request" in request.headers else "catalog.html"
    return templates.TemplateResponse(request, template, context)


@router.get("/apt/{site_listing_id}", response_class=HTMLResponse)
async def apt(
    request: Request, site_listing_id: int, db_url: str = Depends(catalog_db_url)
) -> HTMLResponse:
    item = await readmodel.get_public(site_listing_id, database_url=db_url)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    similar = await readmodel.similar(item, limit=4, database_url=db_url)
    # Stage S Wave 2 (§M, FUZZY/D1): snap coords to the ~100m privacy grid BEFORE they reach the
    # page so the exact building never appears in the HTML source. The template renders the map
    # block only when coords exist; maps carries the fuzzy Yandex/Google deep-links (None if none).
    flat, flon = fuzz_coords(item.lat, item.lon)
    coords = {"lat": flat, "lon": flon} if flat is not None and flon is not None else None
    maps = maps_links(flat, flon)
    return templates.TemplateResponse(
        request,
        "apt.html",
        {"item": item, "similar": similar, "coords": coords, "maps": maps},
    )


# How long CF edge / browsers may cache a generated OG image. The image only changes when the
# listing's price/район/ВНЖ/first-photo change; the ETag (derived from exactly those inputs)
# revalidates on change, and HTML is never cached, so a fresh share always resolves the live URL.
_OG_MAX_AGE = 86_400


@router.get("/apt/{site_listing_id}/og.png")
async def apt_og_image(
    request: Request, site_listing_id: int, db_url: str = Depends(catalog_db_url)
) -> Response:
    """S5-2: dynamic 1200×630 OpenGraph share-image for /apt/{id} (Telegram/WhatsApp previews).

    Same public-visibility gate as the apt page (get_public → 404 for private/missing). The image
    is composed from privacy-safe fields only (price/район/ВНЖ/first local photo — never an
    address, per D1). Served with a content-derived ETag (+ If-None-Match → 304) and a CF-friendly
    Cache-Control so the edge can hold it.
    """
    item = await readmodel.get_public(site_listing_id, database_url=db_url)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")

    # ETag over the inputs that actually change the rendered image (cheaper + stable vs hashing the
    # PNG bytes, which vary with Pillow/zlib): id, price, район, ВНЖ flag, first photo path.
    first_photo = item.photos[0] if item.photos else ""
    sig = f"{item.id}|{item.price_eur}|{item.district}|{int(item.for_vnj)}|{first_photo}"
    etag = 'W/"og-' + hashlib.sha1(sig.encode("utf-8")).hexdigest()[:16] + '"'
    cache_control = f"public, max-age={_OG_MAX_AGE}"

    if _etag_matches(request.headers.get("if-none-match"), etag):
        return Response(
            status_code=status.HTTP_304_NOT_MODIFIED,
            headers={"ETag": etag, "Cache-Control": cache_control},
        )

    png = og_image.render_og_png(item)
    return Response(
        content=png,
        media_type="image/png",
        headers={"ETag": etag, "Cache-Control": cache_control},
    )


@router.get("/map", response_class=HTMLResponse)
async def map_view(request: Request) -> HTMLResponse:
    # Data is fetched client-side from /api/map.json; the page itself carries no listings.
    return templates.TemplateResponse(request, "map.html", {})


@router.get("/api/map.json")
async def map_json(db_url: str = Depends(catalog_db_url)) -> JSONResponse:
    points = await _cached_map(db_url)
    return JSONResponse({"points": points})


@router.get("/calculator", response_class=HTMLResponse)
async def calculator(request: Request) -> Response:
    # C3: static content page — served with an ETag + conditional 304.
    return _static_page(request, "calculator.html")


# Static pages that always belong in the sitemap (absolute via site_public_base_url).
_SITEMAP_STATIC = ("/", "/catalog", "/map", "/calculator", "/vnj", "/how")


@router.get("/sitemap.xml")
async def sitemap(db_url: str = Depends(catalog_db_url)) -> Response:
    """sitemaps.org urlset — static pages + every is_public /apt/{id}, all <loc> ABSOLUTE.

    Public ids are enumerated via list_public with a large page_size (the public set is small,
    a single page covers it). Private listings never surface — list_public is scoped to
    is_public. No remote/площадка urls: every <loc> is site_public_base_url + a site path.
    """
    base = get_settings().site_public_base_url.rstrip("/")
    items, _total = await readmodel.list_public(
        Filters(show_rented=True), sort="new", page=1, page_size=10000, database_url=db_url
    )
    locs = [f"{base}{path}" for path in _SITEMAP_STATIC]
    locs.extend(f"{base}/apt/{item.id}" for item in items)
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "".join(f"  <url><loc>{_xml_escape(loc)}</loc></url>\n" for loc in locs)
        + "</urlset>\n"
    )
    return Response(content=body, media_type="application/xml")


@router.get("/robots.txt")
async def robots() -> Response:
    """text/plain robots — allow all crawling, point at the absolute sitemap url."""
    base = get_settings().site_public_base_url.rstrip("/")
    body = f"User-agent: *\nAllow: /\nSitemap: {base}/sitemap.xml\n"
    return Response(content=body, media_type="text/plain")


@router.get("/vnj", response_class=HTMLResponse)
async def vnj(request: Request) -> Response:
    # Static content page «ВНЖ и договор» — no DB; base.html globals (telegram_url,
    # district_options, csrf_token) are supplied via the shared Jinja env.
    # C3: served with an ETag + conditional 304 (content-static page).
    return _static_page(request, "vnj.html")


@router.get("/how", response_class=HTMLResponse)
async def how(request: Request) -> Response:
    # Static content page «Как работаем» (incl. the 50% commission breakdown) — no DB.
    # C3: served with an ETag + conditional 304.
    return _static_page(request, "how.html")


@router.post("/lead")
async def lead(
    request: Request,
    name: str = Form(""),
    contact: str = Form(""),
    budget_eur: str = Form(""),
    districts: str = Form(""),
    rooms: str = Form(""),
    message: str = Form(""),
    company: str = Form(""),  # honeypot — real users never see/fill it
    site_listing_id: str = Form(""),  # set when the form is opened for a specific apartment
    _csrf: None = Depends(require_site_csrf),
    _rl: None = Depends(enforce_lead_ratelimit),
    db_url: str = Depends(catalog_db_url),
) -> Response:
    # The id is just a hint here — public/existence validation (and degradation to a general
    # lead for a private/missing id) happens in create_lead via apartment_summary.
    payload = LeadPayload(
        name=name,
        contact=contact,
        budget_eur=_as_int(budget_eur),
        districts=districts or None,
        rooms=rooms or None,
        message=message or None,
        company=company or None,
        site_listing_id=_as_int(site_listing_id),
    )
    try:
        await create_lead(payload, database_url=db_url)
    except LeadValidationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e

    # honeypot trip returns None from create_lead → still respond OK (silent to the bot).
    if "hx-request" in request.headers:
        return templates.TemplateResponse(request, "partials/_lead_ok.html", {})
    return RedirectResponse(url="/?lead=ok", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/subscribe")
async def subscribe(
    request: Request,
    channel: str = Form("email"),
    email: str = Form(""),
    district: list[str] = Form(default=[]),  # multivalued, like the catalog filter
    price_min: str = Form(""),
    price_max: str = Form(""),
    rooms: list[str] = Form(default=[]),
    vnj: str = Form(""),
    wc: str = Form(""),
    company: str = Form(""),  # honeypot — real users never see/fill it
    _csrf: None = Depends(require_site_csrf),
    _rl: None = Depends(enforce_lead_ratelimit),  # C2 reuses the lead per-IP sliding window
    db_url: str = Depends(catalog_db_url),
) -> Response:
    """C2 — create a saved-search subscription (same anti-spam as POST /lead).

    Honeypot trip → silent OK (no row, no link). E-mail → active row. Telegram → an inactive row
    plus a deep-link the visitor opens to link their chat. Validation errors map to 400.
    """
    payload = SubscriptionPayload(
        channel=channel.strip() or "email",
        email=email or None,
        districts=[d for d in district if d.strip()],
        price_min=_as_int(price_min),
        price_max=_as_int(price_max),
        rooms=[r for r in rooms if r.strip()],
        vnj=vnj == "1",
        white_card=wc == "1",
        company=company or None,
    )
    try:
        result = await create_subscription(payload, database_url=db_url)
    except SubscriptionValidationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e

    # honeypot trip returns None → still respond OK (silent), with no deep-link.
    deep_link = result.telegram_deep_link if result is not None else None
    if "hx-request" in request.headers:
        return templates.TemplateResponse(
            request, "partials/_subscribe_ok.html", {"telegram_deep_link": deep_link}
        )
    # Non-HTMX: telegram needs the deep-link visible, so render the same partial inline; e-mail
    # redirects with a success marker (no link to show).
    if deep_link:
        return templates.TemplateResponse(
            request, "partials/_subscribe_ok.html", {"telegram_deep_link": deep_link}
        )
    return RedirectResponse(url="/catalog?sub=ok", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/unsubscribe", response_class=HTMLResponse)
async def unsubscribe_route(
    request: Request,
    token: str = "",
    db_url: str = Depends(catalog_db_url),
    _rl: None = Depends(enforce_lead_ratelimit),  # defense-in-depth vs token-guessing / DoS
) -> Response:
    """C2 — token-based unsubscribe (no auth). Flips is_active=False, renders a confirmation."""
    ok = await unsubscribe(token, database_url=db_url)
    return templates.TemplateResponse(request, "unsubscribe.html", {"ok": ok})
