"""FastAPI app for the public catalog (ADR topology: uvicorn on 127.0.0.1:8001, behind Caddy).

uvicorn entrypoint: `src.site.app:app`. Static is mounted here for dev only — in prod Caddy
serves /static and /photos from disk (far-future cache); the mount is harmless if shadowed.
The lifespan shutdown disposes the read/writer engines so a clean restart leaves no dangling
sqlite connections.

Templates live in src.site.routes (the rendering owner); tests swap that module-level object
for a DictLoader stub, so the app must not create a second Jinja2Templates instance.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import Scope

from src.config import get_settings
from src.site import photos
from src.site.db import dispose_engines
from src.site.format import CORE_DISTRICTS
from src.site.routes import router, templates

STATIC_DIR = Path(__file__).parent / "static"

# C3: how long browsers may cache a /static asset before revalidating. The asset-version token
# (?v=<hash>) on css/js URLs busts this on every deploy that changes those bundles, so a 1-day
# window is safe for the dev mount (in prod Caddy serves /static immutable,max-age=1y instead).
_STATIC_MAX_AGE = 86_400


class _CachedStaticFiles(StaticFiles):
    """StaticFiles that stamps a Cache-Control on every served asset (C3).

    Starlette's StaticFiles already emits ETag + Last-Modified per file; this only adds the
    missing Cache-Control max-age so browsers can revalidate instead of re-downloading. Cache
    busting still relies on the asset-version token appended to css/js URLs in templates.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        if response.status_code in (200, 304):
            response.headers.setdefault(
                "Cache-Control", f"public, max-age={_STATIC_MAX_AGE}"
            )
        return response


@lru_cache(maxsize=1)
def district_options() -> list[str]:
    """Правка Даши №4: в район-дропдаунах только 10 городских районов.

    Раньше — все кириллические ключи districts.yaml (включая микрорайоны).
    Теперь — курируемый список format.CORE_DISTRICTS; микрорайоны схлопываются
    в родителя на отображении (format._PARENT_VALUE), districts.yaml не трогаем.
    """
    return list(CORE_DISTRICTS)


# F5: site-wide «Написать в Telegram» CTA. Register the DM url as a Jinja global on
# the SHARED templates env (the one routes.py renders through) so every page can use
# {{ telegram_url }} — mirrors the csrf_token / site_base_url globals set in routes.py.
templates.env.globals["telegram_url"] = get_settings().site_telegram_url

# F2: район dropdowns (hero combobox + filter rail) read this curated Cyrillic superset.
# Registered as a global so templates need no per-route context for the static option list.
templates.env.globals["district_options"] = district_options()

# C2: subscription form reads this to (a) offer the Telegram channel option and (b) build the
# /start deep-link. Empty → only the e-mail channel is shown (no dead Telegram link).
templates.env.globals["site_telegram_bot_username"] = get_settings().site_telegram_bot_username


def _asset_version() -> str:
    """Content hash over the local css+js bundle → cache-busting token for /static URLs.

    Caddy serves /static as `immutable, max-age=1y`, which is only safe with fingerprinted
    URLs — otherwise a deploy that changes site.css/map.js never reaches browsers that cached
    the old bytes. Templates append `?v={{ asset_v }}`; the token changes iff any css/js changes,
    so unchanged assets keep their long cache and changed ones bust. Computed once per process
    (a deploy restarts otz-site, recomputing it).
    """
    h = hashlib.sha1()
    for path in sorted(STATIC_DIR.rglob("*")):
        if path.is_file() and path.suffix in (".css", ".js"):
            h.update(path.read_bytes())
    return h.hexdigest()[:10]


# Cache-busting token appended to local /static asset URLs (see _asset_version).
templates.env.globals["asset_v"] = _asset_version()


# T4-2 (§P.2): фото-варианты в шаблонах — тонкие обёртки над read-API src/site/photos.py.
# photo_v: существование через манифест; варианта нет (нет манифеста/записи/файла) → ОРИГИНАЛ
# (фоллбэк — прод без backfill не ломается). photo_dims: (w, h) ОРИГИНАЛА для width/height (CLS→0).
# Здесь ТОЛЬКО регистрация; логика/кэш манифеста — в photos.py (T4-1).
def _photo_v(src: str, kind: str = "card") -> str:
    return photos.variant_for(src, kind) or src


templates.env.filters["photo_v"] = _photo_v
templates.env.filters["photo_dims"] = photos.dims_for


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    await dispose_engines()


async def _not_found_handler(request: Request, exc: Exception) -> Response:
    """W3-4: custom styled 404 → templates/404.html, status 404.

    Renders through routes.templates — the SAME Jinja env that carries the globals
    (telegram_url, district_options, csrf_token, site_base_url) — so 404.html can extend
    base.html and emit the «Написать в Telegram» CTA + the link back to /catalog.
    Non-404 HTTP errors fall through to Starlette's default handling.

    Typed as `exc: Exception` to match Starlette's add_exception_handler signature; we register
    it for StarletteHTTPException only, so the narrow is always satisfied at runtime.
    """
    assert isinstance(exc, StarletteHTTPException)
    if exc.status_code != 404:
        return await http_exception_handler(request, exc)
    return templates.TemplateResponse(request, "404.html", {}, status_code=404)


def create_app() -> FastAPI:
    # openapi_url=None: no /openapi.json route inventory on an internet-facing app.
    app = FastAPI(
        title="otz-rent-finder site",
        docs_url=None, redoc_url=None, openapi_url=None,
        lifespan=lifespan,
    )
    if STATIC_DIR.exists():
        # C3: cache-aware static mount (adds Cache-Control; ETag/Last-Modified come from base).
        app.mount("/static", _CachedStaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(router)
    # Custom 404 (unknown path AND a route raising HTTPException(404), e.g. /apt/{missing}).
    app.add_exception_handler(StarletteHTTPException, _not_found_handler)
    return app


app = create_app()
