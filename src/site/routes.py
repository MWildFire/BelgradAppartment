"""Public catalog routes (DATA_CONTRACT §2 — exact template names + context keys).

Every catalog/facets/map read goes through the in-process TTL cache (src/site/cache.py),
keyed by the normalized query params. POST /lead and /healthz are never cached.

The DB url for reads is injected via Depends(catalog_db_url) — the test seam. Templates live
on the module-level `templates` object so tests can swap it for a DictLoader stub and assert
the selected template + context without depending on agent D's real markup.
"""

from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from src.config import get_settings
from src.site import cache, readmodel
from src.site.deps import (
    SITE_CSRF_TOKEN,
    catalog_db_url,
    enforce_lead_ratelimit,
    require_site_csrf,
)
from src.site.leads import LeadPayload, LeadValidationError, create_lead
from src.site.readmodel import Filters

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
    return templates.TemplateResponse(
        request, "apt.html", {"item": item, "similar": similar}
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
async def calculator(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "calculator.html", {})


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
async def vnj(request: Request) -> HTMLResponse:
    # Static content page «ВНЖ и договор» — no DB; base.html globals (telegram_url,
    # district_options, csrf_token) are supplied via the shared Jinja env.
    return templates.TemplateResponse(request, "vnj.html", {})


@router.get("/how", response_class=HTMLResponse)
async def how(request: Request) -> HTMLResponse:
    # Static content page «Как работаем» (incl. the 50% commission breakdown) — no DB.
    return templates.TemplateResponse(request, "how.html", {})


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
