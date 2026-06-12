"""Application configuration via pydantic-settings (.env -> typed Settings)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Single source of truth for runtime configuration."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Database ---
    database_url: str = "sqlite+aiosqlite:///./data/otz.db"

    # --- Storage ---
    data_dir: Path = PROJECT_ROOT / "data"
    inbound_photos_dir: Path = PROJECT_ROOT / "data" / "inbound"
    listings_photos_dir: Path = PROJECT_ROOT / "data" / "listings"

    # --- Telegram (Telethon) ---
    tg_api_id: int = 0
    tg_api_hash: str = ""
    tg_session_name: str = "otz_monitor"
    tg_phone: str = ""

    # --- Publication target (informational only — we do NOT publish ourselves) ---
    # We deliver candidates to Дарья's personal DM; she copies and posts to @RentBelgrad herself.
    tg_target_channel: str = "@RentBelgrad"

    # Username (without leading @) OR Telegram user ID OR the string "me" (= Saved Messages).
    # Used only when tg_delivery_mode == "userbot". Default = "Darya_kralj".
    tg_delivery_target: str = "Darya_kralj"

    # --- Bot-based delivery (recommended) ---
    # "bot" = send via Bot API (separate @SomeBot chat, doesn't mix with personal Telegram).
    # "userbot" = send via Telethon as our own user account (legacy).
    tg_delivery_mode: str = "bot"
    tg_bot_token: str = ""
    # Comma-separated list of chat_ids (numeric) the bot sends each candidate to.
    # E.g. "1003207518,62399456" — Дарья + Дмитрий. Each chat_id is an independent recipient.
    tg_bot_recipients: str = ""

    # --- Scrapers ---
    scrape_list_interval_min: int = 30
    scrape_recheck_interval_hours: int = 6
    scraper_user_agent: str = ""
    scraper_proxy_url: str = ""

    # --- Nightly maintenance steps (nightly-refresh) ---
    # Every step is disableable via env (=0 → skip) and fault-isolated at runtime:
    # a step failure adds a 🚨 line to the nightly summary, only the base scrape is fatal.
    # Broken rows (active, no photo hashes) re-fetched per night; 0 = skip.
    nightly_heal_limit: int = 500
    nightly_detail_refresh: int = 200  # active rows for rolling detail re-fetch per night; 0 = skip
    nightly_prune_embeddings: bool = True  # NIGHTLY_PRUNE_EMBEDDINGS=0 → skip embeddings prune
    nightly_metrics: bool = True  # NIGHTLY_METRICS=0 → skip daily_metrics snapshot + drop alert

    # --- Price-drop alerts (W3-G3, consumed in _nightly_refresh) ---
    price_drop_alerts: bool = True  # PRICE_DROP_ALERTS=0 → no alerts
    price_drop_min_pct: float = 5.0  # alert only when price fell ≥ this percent vs old price

    # --- Source channels ---
    source_default_start_date: str = "2026-05-15"  # ISO date — earliest message to backfill from

    # --- Source URLs ---
    # Canonical post-migration list URL. The old /stambeni-objekti/stanovi/... URL now
    # 301-redirects here AND drops query params, which breaks ?pag=N pagination — so the
    # canonical URL must be configured directly.
    nekretnine_list_url: str = "https://www.nekretnine.rs/izdavanje-stanova/beograd/"
    cetiri_zida_list_url: str = "https://www.4zida.rs/izdavanje-stanova/beograd"
    halooglasi_list_url: str = "https://www.halooglasi.com/nekretnine/izdavanje-stanova/beograd"

    # --- Matching thresholds ---
    phash_max_hamming: int = 6
    min_matched_photos: int = 2
    area_tolerance_m2: float = 5.0
    price_tolerance_eur: int = 0
    clip_similarity_threshold: float = 0.92
    enable_clip_fallback: bool = True
    # Cross-check: drop a CLIP candidate if parsed price/area diverge from listing by more than this.
    cross_check_price_tolerance_pct: float = 0.25
    cross_check_area_tolerance_pct: float = 0.20
    # LLM-based final validation through Claude API. Requires ANTHROPIC_API_KEY.
    enable_llm_validation: bool = True
    llm_validation_min_confidence: float = 0.7
    anthropic_api_key: str = ""

    # --- Web admin ---
    admin_host: str = "127.0.0.1"
    admin_port: int = 8000
    admin_username: str = "darya"
    admin_password: str = Field(default="change_me", repr=False)

    # --- Telemetry ---
    # When True, every inbound that ends with no_match posts a one-line summary into the
    # logger userbot's Saved Messages (via otz_logger.session). Lets us see that the
    # pipeline is alive without spamming Дарья's bot inbox with unmatched posts.
    notify_no_match_enabled: bool = True

    # --- Misc ---
    log_level: str = "INFO"
    timezone: str = "Europe/Belgrade"

    @model_validator(mode="after")
    def _absolutize_sqlite_path(self) -> Settings:
        # A CWD-relative sqlite path silently targets a different/empty DB when the CLI
        # runs from another directory — anchor it at PROJECT_ROOT instead.
        prefix = "sqlite+aiosqlite:///"
        if self.database_url.startswith(prefix):
            path = self.database_url[len(prefix):]
            if path and path != ":memory:" and not path.startswith("/"):
                self.database_url = prefix + str((PROJECT_ROOT / path).resolve())
        return self

    def ensure_dirs(self) -> None:
        """Create data directories if missing (idempotent)."""
        for path in (self.data_dir, self.inbound_photos_dir, self.listings_photos_dir):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
