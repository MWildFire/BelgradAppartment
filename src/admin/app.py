"""FastAPI app factory for the admin UI."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates

from src.admin.routes import router

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def create_app() -> FastAPI:
    app = FastAPI(title="otz-rent-finder admin", docs_url=None, redoc_url=None)
    app.include_router(router)
    return app


app = create_app()
