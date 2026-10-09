"""Public catalog routes (DATA_CONTRACT §2 — exact template names + context keys).

Every catalog/facets/map read — плюс главная («index») и карточка /apt/{id} (Wave-5 #13) —
goes through the in-process TTL cache (src/site/cache.py), keyed by the normalized query
params (или фиксированным ключом без пользовательских данных). POST /lead and /healthz are
never cached; /apt/{id}/og.png держит собственный ETag-механизм и через TTL-кэш не ходит.

The DB url for reads is injected via Depends(catalog_db_url) — the test seam. Templates live
on the module-level `templates` object so tests can swap it for a DictLoader stub and assert
the selected template + context without depending on agent D's real markup.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import anyio
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Form,
    HTTPException,
    Request,
    status,
)
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from src.config import get_settings
from src.site import analytics, cache, og_image, readmodel
from src.site.deps import (
    SITE_CSRF_TOKEN,
    catalog_db_url,
    enforce_lead_ratelimit,
    require_site_csrf,
)
from src.site.format import CORE_DISTRICTS
from src.site.leads import (
    LeadPayload,
    LeadValidationError,
    clean_referrer,
    create_lead,
)
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

# S5-1 (PWA): the service-worker source, read once. It ships a __CACHE_VERSION__ placeholder
# that GET /sw.js substitutes with the live asset version (app.py sets templates.env.globals
# ["asset_v"]) — so every deploy that bumps asset_v yields a fresh SW cache name and the old
# caches are evicted on activate. Served at ROOT scope (/sw.js, not /static/...) so the worker
# can control the whole origin.
_SW_SOURCE_PATH = Path(__file__).parent / "static" / "js" / "sw.js"
_SW_CACHE_TOKEN = "__CACHE_VERSION__"
# Rendered SW body memoized per asset-version: the substitution result is fixed for a given
# version, so we read+rewrite the file once instead of on every navigation's SW update check
# (browsers re-fetch /sw.js on each navigation). Keyed by version so a deploy still busts it.
_SW_RENDERED: dict[str, str] = {}


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


# --------------------------------------------------- T2-3: атрибуция сабмитов (§A.3/§A.4)

_UTM_KEYS = ("utm_source", "utm_medium", "utm_campaign")


def _referer_query_utm(request: Request) -> dict[str, str]:
    """No-JS фоллбэк (§A.4): utm_* из query страницы-источника (заголовок Referer).

    Без JS hidden-поля пусты, но браузер шлёт Referer = страницу формы; если посетитель
    пришёл на неё по ссылке с ?utm_source=…, метки ещё живут в её query. Санитайз не здесь:
    значения всё равно проходят clean_utm у писателя (мусор → NULL, никогда не 400).
    """
    try:
        query = urlsplit(request.headers.get("referer", "")).query
    except ValueError:  # кривой Referer от рукописного клиента — фоллбэка просто нет
        return {}
    if not query:
        return {}
    params = parse_qs(query)
    return {k: params[k][0] for k in _UTM_KEYS if k in params and params[k][0]}


def _form_attribution(
    request: Request, utm_source: str, utm_medium: str, utm_campaign: str, referrer: str
) -> dict[str, str | None]:
    """Атрибуция сабмита: hidden-поля utm.js (first-touch) с per-полевым no-JS фоллбэком.

    referrer фильтруется от «своих» хостов здесь — только роут знает хост запроса (за
    прокси он может отличаться от публичного base_url, проверяем оба). Длины/charset
    добивает писатель (§A.1).
    """
    fallback = _referer_query_utm(request)
    own_hosts = (
        request.url.hostname or "",
        urlsplit(get_settings().site_public_base_url).hostname or "",
    )
    form_utm = {"utm_source": utm_source, "utm_medium": utm_medium, "utm_campaign": utm_campaign}
    return {
        **{k: (form_utm[k].strip() or fallback.get(k)) for k in _UTM_KEYS},
        "referrer": clean_referrer(referrer, own_hosts=own_hosts),
    }


def _submit_event_request(request: Request, attribution: dict[str, str | None]) -> Request:
    """Синтетический Request для record_event('lead_submit'/'subscribe_submit').

    record_event — единственный писатель site_events, и он читает атрибуцию из запроса
    (query + Referer), а скип HX-Request у него — про pageview-пагинацию. У POST-сабмита же
    utm в query нет, Referer — своя страница, а реальная форма шлёт hx-post. Поэтому событию
    отдаём копию scope, где: (а) HX-заголовок снят — submit пишется и для HTMX; (б) query =
    first-touch utm ИЗ ФОРМЫ; (в) Referer = внешний referrer формы. ip/ua/host исходного
    запроса сохраняются — visitor-хэш и бот-эвристика считаются как обычно.
    """
    scope = dict(request.scope)
    headers = [(k, v) for (k, v) in scope["headers"] if k not in (b"hx-request", b"referer")]
    ref = attribution.get("referrer")
    if ref:
        # заголовок должен быть ASCII: не-ASCII части URL процентуем (IRI→URI), ASCII не трогаем
        headers.append((b"referer", quote(ref, safe=":/?#[]@!$&'()*+,;=%~").encode("ascii")))
    scope["headers"] = headers
    scope["query_string"] = urlencode(
        {k: v for k in _UTM_KEYS if (v := attribution.get(k))}
    ).encode("ascii")
    return Request(scope)


def _static_page(request: Request, template: str) -> Response:
    """C3: render a genuinely-static content page WITH an ETag + conditional 304 short-circuit.

    Only the static content pages (/vnj, /how) route through here — their bodies
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
        return cast("tuple[list[readmodel.SiteListingView], int]", cached)
    filters, sort, page = _parse_filters(request)
    result = await readmodel.list_public(filters, sort=sort, page=page, database_url=db_url)
    cache.catalog_cache.set(key, result)
    return result


async def _cached_index(
    db_url: str,
) -> tuple[list[readmodel.SiteListingView], readmodel.SiteListingView | None]:
    """Wave-5 #13: данные главной (featured-подборка + «квартира недели») через TTL-кэш.

    Раньше каждый GET / делал 2 полных _load_rows-гидрации (list_public + featured_one) —
    стоимость линейна по каталогу и умножается краулерами. Ключ фиксированный (без
    пользовательских данных); живёт в catalog_cache, чтобы invalidate_catalog_cache()
    при publish/hide сбрасывал и его; кросс-процессно свежесть ограничивает тот же TTL.
    """
    key = ("index",)
    cached = cache.catalog_cache.get(key)
    if cached is not cache.MISS:
        return cast(
            "tuple[list[readmodel.SiteListingView], readmodel.SiteListingView | None]", cached
        )
    items, _total = await readmodel.list_public(
        Filters(), sort="new", page=1, page_size=FEATURED_N, database_url=db_url
    )
    week = await readmodel.featured_one(database_url=db_url)
    result = (items, week)
    cache.catalog_cache.set(key, result)
    return result


async def _cached_apt(
    site_listing_id: int, db_url: str
) -> tuple[readmodel.SiteListingView | None, list[readmodel.SiteListingView]]:
    """Wave-5 #13: карточка /apt/{id} + «похожие» через TTL-кэш (2 полных _load_rows на MISS).

    Ключ — только id из пути (без пользовательских данных). None кэшируется тоже (404 для
    приватного/несуществующего id): id-перебор краулером не гидрирует каталог на каждый
    запрос, а maxsize кэша ограничивает память. og.png этот кэш сознательно НЕ использует.
    """
    key = ("apt", site_listing_id)
    cached = cache.catalog_cache.get(key)
    if cached is not cache.MISS:
        return cast(
            "tuple[readmodel.SiteListingView | None, list[readmodel.SiteListingView]]", cached
        )
    item = await readmodel.get_public(site_listing_id, database_url=db_url)
    similar = (
        await readmodel.similar(item, limit=4, database_url=db_url) if item is not None else []
    )
    result = (item, similar)
    cache.catalog_cache.set(key, result)
    return result


async def _cached_facets(db_url: str) -> readmodel.Facets:
    key = ("facets",)
    cached = cache.facets_cache.get(key)
    if cached is not cache.MISS:
        return cast("readmodel.Facets", cached)
    result = await readmodel.facets(database_url=db_url)
    cache.facets_cache.set(key, result)
    return result


async def _cached_map(db_url: str) -> list[dict[str, object]]:
    key = ("map",)
    cached = cache.map_cache.get(key)
    if cached is not cache.MISS:
        return cast("list[dict[str, object]]", cached)
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


@router.get("/readyz")
async def readiness() -> JSONResponse:
    from sqlalchemy import select

    from src.db.models import Listing
    from src.site.db import read_session

    try:
        async with read_session() as session:
            await session.execute(select(Listing.details_succeeded_at).limit(1))
        return JSONResponse({"status": "ready"})
    except Exception:
        return JSONResponse({"status": "not_ready"}, status_code=503)


@router.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"status": "ok"})


@router.get("/sw.js")
async def service_worker() -> Response:
    """S5-1: serve the PWA service worker at ROOT scope so it controls the whole origin.

    A worker served from /static/js/sw.js would be scoped to /static/ only; serving it here at
    /sw.js (+ `Service-Worker-Allowed: /`) lets it intercept navigations site-wide. The live
    asset version is baked into the SW's CACHE_NAME (replacing __CACHE_VERSION__), so a deploy —
    which recomputes asset_v — invalidates the SW cache and drops stale entries on activate.
    The SW file itself must NOT be cached aggressively, or a deploy could be shadowed by an old
    worker (browsers also byte-compare it on update; no-cache keeps that check honest).
    """
    version = str(templates.env.globals.get("asset_v", "0"))
    source = _SW_RENDERED.get(version)
    if source is None:
        source = _SW_SOURCE_PATH.read_text(encoding="utf-8").replace(_SW_CACHE_TOKEN, version)
        _SW_RENDERED[version] = source
    return Response(
        content=source,
        media_type="application/javascript",
        headers={
            "Service-Worker-Allowed": "/",
            "Cache-Control": "no-cache",
        },
    )


@router.get("/offline", response_class=HTMLResponse)
async def offline(request: Request) -> HTMLResponse:
    """S5-1: the offline fallback shell. Served here AND precached by the SW, which returns it
    for any failed HTML navigation (network-first → cached /offline on failure)."""
    return templates.TemplateResponse(request, "offline.html", {})


@router.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    background_tasks: BackgroundTasks,
    db_url: str = Depends(catalog_db_url),
) -> HTMLResponse:
    # Featured-подборка + «квартира недели» (hero side-card, W3-2) — одной кэшируемой парой.
    items, week = await _cached_index(db_url)
    facets = await _cached_facets(db_url)
    stats = {
        "active": facets.counts.get("total", 0),
        # Правка Даши №5: показываем количество покрываемых районов (10), а не фасеты.
        "districts": len(CORE_DISTRICTS),
        "vnj": facets.counts.get("vnj", 0),
        "white_card": facets.counts.get("white_card", 0),
    }
    # T2-2: pageview — в фоне ПОСЛЕ ответа; хук в конце success-пути, ошибки страницу не трогают.
    background_tasks.add_task(analytics.record_event, request, path="/", database_url=db_url)
    return templates.TemplateResponse(
        request, "index.html", {"featured": items, "week": week, "stats": stats}
    )


@router.get("/catalog", response_class=HTMLResponse)
async def catalog(
    request: Request,
    background_tasks: BackgroundTasks,
    db_url: str = Depends(catalog_db_url),
) -> HTMLResponse:
    items, total = await _cached_catalog(request, db_url)
    facets = await _cached_facets(db_url)
    applied = _applied(request)
    context = {
        "items": items,
        "facets": facets,
        "applied": applied,
        "total": total,
        "pager": _pager(request, total, cast("int", applied["page"])),
    }
    # T2-2: pageview (HX-пагинацию/фильтры record_event скипает сам — это не просмотр страницы).
    background_tasks.add_task(analytics.record_event, request, path="/catalog", database_url=db_url)
    # HTMX swaps only the results grid + pagination; full-page request gets the shell.
    template = "partials/_results.html" if "hx-request" in request.headers else "catalog.html"
    return templates.TemplateResponse(request, template, context)


@router.get("/apt/{site_listing_id}", response_class=HTMLResponse)
async def apt(
    request: Request,
    site_listing_id: int,
    background_tasks: BackgroundTasks,
    db_url: str = Depends(catalog_db_url),
) -> HTMLResponse:
    item, similar = await _cached_apt(site_listing_id, db_url)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    # Stage S Wave 2 (§M, FUZZY/D1): snap coords to the ~100m privacy grid BEFORE they reach the
    # page so the exact building never appears in the HTML source. The template renders the map
    # block only when coords exist; maps carries the fuzzy Yandex/Google deep-links (None if none).
    flat, flon = fuzz_coords(item.lat, item.lon)
    coords = {"lat": flat, "lon": flon} if flat is not None and flon is not None else None
    maps = maps_links(flat, flon)
    # T2-2: после 404-гейта — приватные/несуществующие id не логируются; путь нормализован
    # по контракту §A.1: path='/apt' + apt_id (кардинальность путей в агрегатах — O(1)).
    background_tasks.add_task(
        analytics.record_event, request, path="/apt", apt_id=site_listing_id, database_url=db_url
    )
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

    # PERF: the Pillow decode/resize/scrim/encode is CPU-bound and was blocking the single
    # uvicorn event loop per cache-miss (Telegram/WhatsApp crawlers hit these in bursts). Offload
    # to a worker thread so concurrent share-preview fetches don't stall every other request.
    png = await anyio.to_thread.run_sync(og_image.render_og_png, item)
    return Response(
        content=png,
        media_type="image/png",
        headers={"ETag": etag, "Cache-Control": cache_control},
    )


@router.get("/map", response_class=HTMLResponse)
async def map_view(
    request: Request,
    background_tasks: BackgroundTasks,
    db_url: str = Depends(catalog_db_url),  # T2-2: только для писателя аналитики (тот же seam)
) -> HTMLResponse:
    # Data is fetched client-side from /api/map.json; the page itself carries no listings.
    background_tasks.add_task(analytics.record_event, request, path="/map", database_url=db_url)
    return templates.TemplateResponse(request, "map.html", {})


@router.get("/api/map.json")
async def map_json(db_url: str = Depends(catalog_db_url)) -> JSONResponse:
    points = await _cached_map(db_url)
    return JSONResponse({"points": points})


@router.get("/calculator")
async def calculator(request: Request) -> RedirectResponse:
    """Правка Даши №2: страницы калькулятора больше нет — постоянный редирект на лид-форму."""
    return RedirectResponse("/#lead", status_code=301)


# Static pages that always belong in the sitemap (absolute via site_public_base_url).
_SITEMAP_STATIC = ("/", "/catalog", "/map", "/vnj", "/how")


@router.get("/sitemap.xml")
async def sitemap(db_url: str = Depends(catalog_db_url)) -> Response:
    """sitemaps.org urlset — static pages + every is_public /apt/{id}, all <loc> ABSOLUTE.

    Public ids come from readmodel.public_ids() — an id-only query (no view hydration: no photo
    URLs / район normalization / rented checks), since the sitemap only needs the ids. Private
    listings never surface (scoped to is_public). No remote/площадка urls: every <loc> is
    site_public_base_url + a site path.
    """
    base = get_settings().site_public_base_url.rstrip("/")
    ids = await readmodel.public_ids(database_url=db_url)
    locs = [f"{base}{path}" for path in _SITEMAP_STATIC]
    locs.extend(f"{base}/apt/{apt_id}" for apt_id in sorted(ids))
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
async def vnj(
    request: Request,
    background_tasks: BackgroundTasks,
    db_url: str = Depends(catalog_db_url),  # T2-2: только для писателя аналитики
) -> Response:
    # Static content page «ВНЖ и договор» — no DB; base.html globals (telegram_url,
    # district_options, csrf_token) are supplied via the shared Jinja env.
    # C3: served with an ETag + conditional 304 (content-static page).
    resp = _static_page(request, "vnj.html")
    # T2-2: pageview только на полный 200-рендер (304 — скип, см. /how).
    if resp.status_code == status.HTTP_200_OK:
        background_tasks.add_task(analytics.record_event, request, path="/vnj", database_url=db_url)
    return resp


@router.get("/how", response_class=HTMLResponse)
async def how(
    request: Request,
    background_tasks: BackgroundTasks,
    db_url: str = Depends(catalog_db_url),  # T2-2: только для писателя аналитики
) -> Response:
    # Static content page «Как работаем» (incl. the 50% commission breakdown) — no DB.
    # C3: served with an ETag + conditional 304.
    resp = _static_page(request, "how.html")
    # T2-2: pageview только на полный 200-рендер (304 — скип, см. /how).
    if resp.status_code == status.HTTP_200_OK:
        background_tasks.add_task(analytics.record_event, request, path="/how", database_url=db_url)
    return resp


@router.post("/lead")
async def lead(
    request: Request,
    background_tasks: BackgroundTasks,
    name: str = Form(""),
    contact: str = Form(""),
    budget_eur: str = Form(""),
    districts: str = Form(""),
    rooms: str = Form(""),
    message: str = Form(""),
    company: str = Form(""),  # honeypot — real users never see/fill it
    site_listing_id: str = Form(""),  # set when the form is opened for a specific apartment
    utm_source: str = Form(""),  # T2-3: hidden-поля атрибуции — utm.js (first-touch)
    utm_medium: str = Form(""),
    utm_campaign: str = Form(""),
    referrer: str = Form(""),
    _csrf: None = Depends(require_site_csrf),
    _rl: None = Depends(enforce_lead_ratelimit),
    db_url: str = Depends(catalog_db_url),
) -> Response:
    # The id is just a hint here — public/existence validation (and degradation to a general
    # lead for a private/missing id) happens in create_lead via apartment_summary.
    attribution = _form_attribution(request, utm_source, utm_medium, utm_campaign, referrer)
    payload = LeadPayload(
        name=name,
        contact=contact,
        budget_eur=_as_int(budget_eur),
        districts=districts or None,
        rooms=rooms or None,
        message=message or None,
        company=company or None,
        site_listing_id=_as_int(site_listing_id),
        utm_source=attribution["utm_source"],
        utm_medium=attribution["utm_medium"],
        utm_campaign=attribution["utm_campaign"],
        referrer=attribution["referrer"],
    )
    try:
        saved = await create_lead(payload, database_url=db_url)
    except LeadValidationError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)) from e

    # T2-3 (§A.3): lead_submit — ПОСЛЕ коммита заявки, фоном, некритично для ответа. Honeypot
    # (saved is None) события не пишет: коммита не было, счётчик заявок боты не надувают.
    # apt_id — уже ПРОВЕРЕННЫЙ create_lead id (приватная/битая квартира деградировала в None).
    if saved is not None:
        background_tasks.add_task(
            analytics.record_event,
            _submit_event_request(request, attribution),
            path="/lead",
            event_type="lead_submit",
            apt_id=saved.site_listing_id,
            database_url=db_url,
        )

    # honeypot trip returns None from create_lead → still respond OK (silent to the bot).
    if "hx-request" in request.headers:
        return templates.TemplateResponse(request, "partials/_lead_ok.html", {})
    return RedirectResponse(url="/?lead=ok", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/subscribe")
async def subscribe(
    request: Request,
    background_tasks: BackgroundTasks,
    channel: str = Form("email"),
    email: str = Form(""),
    district: list[str] = Form(default=[]),  # multivalued, like the catalog filter
    price_min: str = Form(""),
    price_max: str = Form(""),
    rooms: list[str] = Form(default=[]),
    vnj: str = Form(""),
    wc: str = Form(""),
    company: str = Form(""),  # honeypot — real users never see/fill it
    utm_source: str = Form(""),  # T2-3: hidden-поля атрибуции — utm.js (first-touch)
    utm_medium: str = Form(""),
    utm_campaign: str = Form(""),
    referrer: str = Form(""),
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

    # T2-3 (§A.3): subscribe_submit — ПОСЛЕ коммита подписки, фоном, некритично для ответа.
    # У subscriptions нет utm-колонок (§A.1) — first-touch атрибуция живёт в самом событии.
    if result is not None:
        background_tasks.add_task(
            analytics.record_event,
            _submit_event_request(
                request,
                _form_attribution(request, utm_source, utm_medium, utm_campaign, referrer),
            ),
            path="/subscribe",
            event_type="subscribe_submit",
            database_url=db_url,
        )

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
