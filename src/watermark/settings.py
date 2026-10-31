"""Watermark-remover settings.

They live here and not in `src/config.py` on purpose: the production release route is "code
only" and refuses a release that changes `src/config.py`. The names, defaults and the `.env`
file are the same as the Watermark block of `Settings` had in R04, so operators set the same
`WATERMARK_*` variables in `/opt/otz/.env`.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.config import PROJECT_ROOT


class WatermarkSettings(BaseSettings):
    """Runtime configuration of /clean, the clean-copy store and the site/delivery switches."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # /clean and incoming photos in RentFinderBot; served by `otz subscription-bot` (one poller).
    watermark_bot_enabled: bool = False
    # Extra chat ids (besides TG_BOT_RECIPIENTS) allowed to use /clean, comma-separated.
    watermark_allowed_chat_ids: str = ""
    # Clean copies: <clean_dir>/<path relative to inbound_photos_dir>. Originals never change.
    # Must stay OUTSIDE inbound_photos_dir (the site backfill walks that whole tree).
    watermark_clean_dir: Path = PROJECT_ROOT / "data" / "clean"
    # LaMa for central Halo and 4zida logos (needs torch from the `clip` extra + model).
    watermark_lama_enabled: bool = True
    watermark_lama_model_path: Path = PROJECT_ROOT / "data" / "models" / "big-lama.pt"
    # skip keeps Halo corners; telea is the classic fill; lama uses the shared LaMa model.
    # If LaMa is unavailable, lama keeps the corner (method=skip, kept_reason=lama_unavailable).
    watermark_halo_corner_mode: Literal["skip", "telea", "lama"] = "skip"
    # Deliver candidates to Darya already cleaned (off by default).
    watermark_clean_on_delivery: bool = False
    # The site serves clean copies instead of originals when they exist (off by default).
    watermark_site_use_clean: bool = False
    watermark_jpeg_quality: int = Field(default=95, ge=70, le=100)


@lru_cache(maxsize=1)
def get_watermark_settings() -> WatermarkSettings:
    return WatermarkSettings()
