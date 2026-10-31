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

    # --- LaMa limits (WM05 B2). The model's working memory grows with the crop area, about
    # 2.6 KB per pixel on top of ~0.5 GiB for torch + big-lama (measured on Linux), so the bot
    # keeps LaMa inside a pixel budget, in a separate worker process, one request at a time.
    # Longest side of a crop given to LaMa; larger crops are downscaled (rarely binding).
    watermark_lama_max_side: int = Field(default=1024, ge=64, le=4096)
    # Crop area budget in pixels; 0 = off. Over the budget the margin shrinks first
    # (down to watermark_lama_min_margin), a downscale is the last resort.
    watermark_lama_max_pixels: int = Field(default=75_000, ge=0)
    watermark_lama_min_margin: int = Field(default=16, ge=0, le=96)
    # torch threads of the LaMa process (the server has 2 vCPU shared with the monitor).
    watermark_torch_threads: int = Field(default=2, ge=1, le=16)
    # The LaMa worker exits (inline mode: the model is dropped) after this many idle seconds;
    # 0 = keep it for the life of the process.
    watermark_lama_idle_unload_seconds: int = Field(default=600, ge=0)
    # "process": LaMa in a worker process, torch never enters the bot; "inline": same process
    # (tests, one-off CLI runs).
    watermark_lama_isolation: Literal["process", "inline"] = "process"
    # One LaMa request may take at most this long; the worker is killed and the mark is kept.
    watermark_lama_timeout_seconds: int = Field(default=180, ge=1)


@lru_cache(maxsize=1)
def get_watermark_settings() -> WatermarkSettings:
    return WatermarkSettings()
