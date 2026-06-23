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

    # --- Live-smoke probe (A2: markup-change protection) ---
    # Healthy list page must yield >= this many previews (mirrors Scraper.MIN_PREVIEWS).
    scrape_smoke_min_previews: int = 5
    # SCRAPE_SMOKE_ENABLED=0 → the scrape-smoke command no-ops (kill switch).
    scrape_smoke_enabled: bool = True
    # B1 — per-parser parse-success-rate degradation guard in the nightly run. Alert when
    # parse_rate (details_parsed_ok/details_attempted) < parse_rate_min AND the sample is big
    # enough (details_attempted >= parse_rate_min_sample) — don't cry wolf on tiny samples.
    parse_rate_min: float = 0.7
    parse_rate_min_sample: int = 10

    # --- Nightly maintenance steps (nightly-refresh) ---
    # Every step is disableable via env (=0 → skip) and fault-isolated at runtime:
    # a step failure adds a 🚨 line to the nightly summary, only the base scrape is fatal.
    # Broken rows (active, no photo hashes) re-fetched per night; 0 = skip.
    nightly_heal_limit: int = 500
    nightly_detail_refresh: int = 200  # active rows for rolling detail re-fetch per night; 0 = skip
    nightly_prune_embeddings: bool = True  # NIGHTLY_PRUNE_EMBEDDINGS=0 → skip embeddings prune
    # A3 liveness sweep: HEAD-check active matched listings (potentially on the site) and
    # deactivate ones that 404/410, so a public card never links to a removed page.
    nightly_verify_links: bool = True  # NIGHTLY_VERIFY_LINKS=0 → skip the liveness sweep
    nightly_verify_links_limit: int = 1000  # cap listings HEAD-checked per night
    nightly_metrics: bool = True  # NIGHTLY_METRICS=0 → skip daily_metrics snapshot + drop alert
    # X1 auto-publish: when a match is delivered (PENDING→PUBLISHED), auto-expose its card on
    # the site. auto_publish_on_send is the runtime kill switch on the delivery hot path (set
    # AUTO_PUBLISH_ON_SEND=0 to revert to manual curation); nightly_auto_publish backfills any
    # delivered-but-not-yet-exposed matches in the nightly run.
    auto_publish_on_send: bool = True  # AUTO_PUBLISH_ON_SEND=0 → never auto-expose on send
    nightly_auto_publish: bool = True  # NIGHTLY_AUTO_PUBLISH=0 → skip the auto-publish backfill
    # X2 auto-remove: when verify-links deactivates a listing the platform removed (404/410),
    # auto-hide its public card in the SAME transaction (NIGHTLY_AUTO_HIDE_SITE_LISTINGS=0 → skip).
    nightly_auto_hide_site_listings: bool = True
    # C2: nightly saved-search digest — new public listings × active subscriptions → notices
    # (idempotent via subscription_matches). NIGHTLY_NOTIFY_SUBSCRIPTIONS=0 → skip the step.
    nightly_notify_subscriptions: bool = True
    # Only listings created within this window are candidates (a fresh subscription doesn't get
    # blasted with the whole back-catalog; subscription_matches still guards against re-sends).
    subscription_notify_since_hours: int = 24
    # Wall-clock budget SHARED by heal+rolling: once exhausted both stop and report skipped rows,
    # so a night of slow Cloudflare-challenged refetches can't overrun (the 2026-06-16 SIGTERM).
    nightly_refetch_budget_sec: int = 7200  # 2h
    # Per-listing hard cap on fetch_detail — one pathological page (goto+poll+recycle retries)
    # can otherwise burn minutes; this bounds it regardless of the scraper's internal retries.
    refetch_per_fetch_timeout_sec: int = 90

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
    # A4 — cross-platform listing dedup (periodic batch job, NOT the per-inbound hot path).
    # Two active listings are the SAME apartment when they share ≥ dedup_min_shared_photos
    # photos whose pHash Hamming distance ≤ dedup_max_hamming. min=2 on purpose: a single
    # shared photo is too weak (agency/stock photos get reused across unrelated listings).
    dedup_max_hamming: int = 6
    dedup_min_shared_photos: int = 2
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

    # --- Public web catalog (Stage W) ---
    site_bind_host: str = "127.0.0.1"
    site_bind_port: int = 8001
    site_public_base_url: str = "https://rent.example.com"
    site_cache_ttl: int = 300
    site_page_size: int = 24
    # Disk root of public photos; Caddy serves it as /photos/*. InboundPost.photo_paths
    # are written under inbound_photos_dir, so the default points there (dev). On the VPS
    # SITE_PHOTOS_ROOT overrides this to the curated public dir.
    site_photos_root: Path = PROJECT_ROOT / "data" / "inbound"
    site_channel_url: str = "https://t.me/RentBelgrad"
    site_lead_ratelimit_max: int = 5
    site_lead_ratelimit_window: int = 3600

    # --- Conversion & polish (Stage W2) ---
    # F5: permanent "Написать в Telegram" CTA points at Дарья's DM (not the channel).
    site_telegram_url: str = "https://t.me/Darya_kralj"
    # C4: optional WhatsApp (wa.me/<number>) and Viber (viber://chat?number=%2B<number>) deep
    # links for the /apt CTA row. EMPTY by default — the button only renders when its URL is
    # set, so an unconfigured number shows no broken link. Fill these in the server .env once
    # the operator provides a WhatsApp/Viber number.
    site_whatsapp_url: str = ""
    site_viber_url: str = ""
    # --- C2: saved-search subscriptions (e-mail + Telegram digest) ---
    # Bot username (without leading @) used to build the Telegram linking deep-link
    # https://t.me/<username>?start=sub_<token>. EMPTY → the Telegram channel is unavailable in
    # the subscribe form (only e-mail offered). Set this to the @username of the bot whose token
    # is TG_BOT_TOKEN once the subscription linking daemon is enabled.
    site_telegram_bot_username: str = ""
    # The subscription linking daemon (otz subscription-bot) is a NEW long-running aiogram poller
    # that listens for /start sub_<token>. OFF by default — enabling it is a deploy choice (it
    # needs its own systemd unit). With it off, e-mail subscriptions work fully; Telegram ones
    # can be created but stay unlinked/inactive until the daemon runs.
    subscription_bot_enabled: bool = False

    # F4: lead-form submissions are e-mailed here (in addition to the bot notice).
    site_lead_email_to: str = "d.mashukova@bk.ru"
    # F4: SMTP relay for sending lead e-mails (relay-agnostic; creds live in the server .env).
    site_smtp_host: str = ""
    site_smtp_port: int = 587
    site_smtp_user: str = ""
    site_smtp_password: str = Field(default="", repr=False)
    site_smtp_from: str = "no-reply@rentbelgrad.com"
    site_smtp_starttls: bool = True

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
