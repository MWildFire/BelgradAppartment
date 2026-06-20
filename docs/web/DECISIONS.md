# Wave 0 Decisions — Public Web Catalog (Stage W)

> Status: **locked** · Date: 2026-06-20 · Branch: `feature/public-web-catalog`
> Output of the Wave 0 spec-check (plan §W0-2). The full design lives in
> [`ARCHITECTURE.md`](ARCHITECTURE.md) (ADR) and [`DATA_CONTRACT.md`](DATA_CONTRACT.md).
> This file records the few **open product decisions** and the defaults inherited from the ADR.

## Decisions taken with Дмитрий/Дарья (2026-06-20)

| # | Question (W0-2) | Decision | Consequence |
|---|---|---|---|
| 1 | Address detail on the public card | **District only** — show human district (`Врачар`), never street/house | `SiteListingView.address` stays `None` in MVP; templates render `district` only. Map pin uses `lat/lon` when present (no street label). Field kept in contract for future opt-in. |
| 2 | Source of ВНЖ / белый картон flags | **Auto-parse from post text + manual override in admin** | Wave 2 publish-action sets default flags from keyword parse of the post text; Дарья can correct via toggle. Keyword set below. |
| 3 | Visibility default on «Mark-posted» | **Auto-public** (`is_public=true`) | Mark-posted upserts `site_listings` with `is_public=true`; Дарья can hide later. Matches DB default `is_public default 1`. |
| 4 | Subdomain / DNS | **Deferred to Wave 6** | Config uses placeholder `rent.example.com`; real subdomain + DNS A-record → `90.156.254.159` provided before deploy. Does not block coding waves. |

## Defaults inherited from the ADR / DATA_CONTRACT (confirmed, not disputed)

- **Stack / hosting:** second FastAPI+Jinja app on the **same VPS**, reads existing SQLite **read-only**, behind **Caddy** (auto-TLS). No Astro/Vercel, no re-ingest from Telegram. (ADR.)
- **Public fields on the card:** price, human district, rooms, area (m²), photos, ВНЖ/белый-картон flags, «снято» badge. **Never** exact address (decision #1), **never** competitor-platform URLs or TG-CDN links (ToS — link goes to the channel instead).
- **Channel link:** default `SITE_CHANNEL_URL = https://t.me/RentBelgrad`; per-listing `channel_post_url` is an optional override Дарья fills in admin. Per-post deep links deferred (bot-mode has no real channel message id).
- **Lead form (`POST /lead`):** required = `name`, `contact`; optional = `budget_eur`, `districts`, `rooms`, `message`. Honeypot anti-bot, CSRF (admin pattern), per-IP rate-limit (`SITE_LEAD_RATELIMIT_MAX=5` / `_WINDOW=3600`). On success → row in `leads` + notice to Дарья via the **existing bot** (`TG_BOT_TOKEN` / `TG_BOT_RECIPIENTS`, no new keys).
- **Photos (W0-3 — resolved from code):** the pipeline already downloads photos locally (`Listing.photo_paths`, `listings_photos_dir=data/listings`). No re-download needed for MVP. `public_photo_url(local_path)` maps a local path → `/photos/<relpath>`; Caddy `file_server` serves `SITE_PHOTOS_ROOT`. On the VPS `SITE_PHOTOS_ROOT` points at the listings photos dir (or a curated copy with a safe prefix).
- **Read/write isolation:** read-only engine (`PRAGMA query_only=ON`) for catalog reads; a narrow scoped writer **only** for `Lead` (busy_timeout=5000, retry on `database is locked`). Covered by a test.
- **No client accounts in MVP.** Favorites live in `localStorage`.

## ВНЖ / белый картон — parsing keywords (decision #2)

Default `is_for_vnj` true when the post text matches (case-insensitive) any of:
`ВНЖ`, `боравак`, `boravak`, `привремени боравак`, `privremeni boravak`, `регистрациј`, `registracij`, `prijava boravka`, `пријава боравка`.

Default `is_for_white_card` true on: `бели картон`, `beli karton`, `белый картон`, `white card`, `пријава адресе`, `prijava adrese`.

Exact list + tests live in the Wave 2 publish-action; admin toggle always wins over the parse.

## Not in MVP (recorded so it isn't lost)

Client accounts/profiles · WebGPU hero · per-post channel deep-links (until `channel_post_url` is filled) · dark theme · multi-language (site is ru; i18n is a separate stage). (ADR.)

## Wave 6 prerequisites carried forward

- Real subdomain chosen + **DNS A-record → `90.156.254.159`** created (decision #4) before deploy (TLS needs it).
- `SITE_PUBLIC_BASE_URL` updated from the placeholder to the real subdomain in `/opt/otz/.env`.

## Note on methodology

The design doc and the implementation plan already existed when this stage started
(`ARCHITECTURE.md`, `DATA_CONTRACT.md`, `docs/superpowers/plans/2026-06-20-public-web-catalog.md`),
so brainstorming/writing-plans were not re-run from scratch — Wave 0 only locked the open
product questions above. Execution follows the existing plan (multi-agent per wave, TDD).
