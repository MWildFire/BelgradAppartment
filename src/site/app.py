"""FastAPI app for the public catalog (ADR topology: uvicorn on 127.0.0.1:8001, behind Caddy).

uvicorn entrypoint: `src.site.app:app`. Static is mounted here for dev only — in prod Caddy
serves /static and /photos from disk (far-future cache); the mount is harmless if shadowed.
The lifespan shutdown disposes the read/writer engines so a clean restart leaves no dangling
sqlite connections.

Templates live in src.site.routes (the rendering owner); tests swap that module-level object
for a DictLoader stub, so the app must not create a second Jinja2Templates instance.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path

import yaml
from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.config import get_settings
from src.post.hashtags import DISTRICTS_PATH  # reuse the canonical districts.yaml path
from src.site.db import dispose_engines
from src.site.routes import router, templates

STATIC_DIR = Path(__file__).parent / "static"

_CYRILLIC_RE = re.compile(r"[а-яё]", re.IGNORECASE)


@lru_cache(maxsize=1)
def district_options() -> list[str]:
    """Curated Cyrillic district display names for the район dropdowns (F2).

    The static superset behind both the hero combobox and the filter rail — it works even
    when the catalog is empty (no facets). Source = data/districts.yaml's Cyrillic keys
    (the human-readable «Врачар»/«Дорћол» labels; Latin keys are aliases for lookup only),
    de-duplicated and sorted alphabetically. Reuses hashtags' DISTRICTS_PATH (no new path).
    """
    if not DISTRICTS_PATH.exists():
        return []
    raw = yaml.safe_load(DISTRICTS_PATH.read_text(encoding="utf-8")) or {}
    names = {str(key) for key in raw if _CYRILLIC_RE.search(str(key))}
    return sorted(names)


# F5: site-wide «Написать в Telegram» CTA. Register the DM url as a Jinja global on
# the SHARED templates env (the one routes.py renders through) so every page can use
# {{ telegram_url }} — mirrors the csrf_token / site_base_url globals set in routes.py.
templates.env.globals["telegram_url"] = get_settings().site_telegram_url

# F2: район dropdowns (hero combobox + filter rail) read this curated Cyrillic superset.
# Registered as a global so templates need no per-route context for the static option list.
templates.env.globals["district_options"] = district_options()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    await dispose_engines()


async def _not_found_handler(request: Request, exc: StarletteHTTPException) -> Response:
    """W3-4: custom styled 404 → templates/404.html, status 404.

    Renders through routes.templates — the SAME Jinja env that carries the globals
    (telegram_url, district_options, csrf_token, site_base_url) — so 404.html can extend
    base.html and emit the «Написать в Telegram» CTA + the link back to /catalog.
    Non-404 HTTP errors fall through to Starlette's default handling.
    """
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
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(router)
    # Custom 404 (unknown path AND a route raising HTTPException(404), e.g. /apt/{missing}).
    app.add_exception_handler(StarletteHTTPException, _not_found_handler)
    return app


app = create_app()
