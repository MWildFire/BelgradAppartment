"""FastAPI app for the public catalog (ADR topology: uvicorn on 127.0.0.1:8001, behind Caddy).

uvicorn entrypoint: `src.site.app:app`. Static is mounted here for dev only — in prod Caddy
serves /static and /photos from disk (far-future cache); the mount is harmless if shadowed.
The lifespan shutdown disposes the read/writer engines so a clean restart leaves no dangling
sqlite connections.

Templates live in src.site.routes (the rendering owner); tests swap that module-level object
for a DictLoader stub, so the app must not create a second Jinja2Templates instance.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from src.config import get_settings
from src.site.db import dispose_engines
from src.site.routes import router, templates

STATIC_DIR = Path(__file__).parent / "static"

# F5: site-wide «Написать в Telegram» CTA. Register the DM url as a Jinja global on
# the SHARED templates env (the one routes.py renders through) so every page can use
# {{ telegram_url }} — mirrors the csrf_token / site_base_url globals set in routes.py.
templates.env.globals["telegram_url"] = get_settings().site_telegram_url


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    await dispose_engines()


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
    return app


app = create_app()
