# INTERFACE FREEZE — Wave 0 draft

Status: `BLOCKED_PENDING_FRESH_QA_GUARDIAN` on baseline `68b996a8c23da5f7dd5607f54d7b296980725b48`; the root-only production SQLite quick check is `ok`, and the immutable golden is `render-golden-manifest.json` with its sidecar SHA-256. A fresh independent Agent C PASS is still required; the last C STOP was captured before the final corpus correction.

## Snapshot

- `ListingPreview.snapshot: ListingDetail | None` is the only new parser-to-orchestrator value.
- A snapshot is complete only with: `external_id`, canonical URL, `price_eur`, `area_m2`, `rooms`, `district`, `address_raw`, `lat`, `lon`, `title`, and at least one valid `pic.nekretnine.rs` photo URL.
- `description` is not completeness-required and is not used by Render Finder Bot selection/rendering.
- `None` or incomplete snapshot means legacy `fetch_detail`; all-none or empty-photo shells are forbidden.
- Existing DB values are never cleared by missing snapshot values. `details_refreshed_at` must not be advanced by snapshot-only ingestion.

## Mode

`NEKRETNINE_LIST_SNAPSHOT_MODE` accepts exactly `legacy`, `shadow`, or `active`; default is `legacy`; invalid values fail fast at settings construction.

- `legacy`: current detail path remains authoritative.
- `shadow`: parse/compare snapshot, but persist through the legacy path.
- `active`: use complete snapshot; incomplete/drifted data falls back to legacy detail.

## Proxy selection and redaction

- Proxy scope for the new pool is Nekretnine browser/httpx list/detail traffic only. Direct Nekretnine CDN photo downloads, other platforms, Telegram, and matching remain direct. Liveness algorithm/DB semantics are unchanged; its transport is frozen to the existing baseline: Nekretnine `verify_active_links` may use the configured proxy from the H1 hardening checkpoint, while other platform HEAD paths remain direct. The migration does not add new liveness proxy routing.
- Candidate order is primary → secondary → metered fallback. Selection is bounded and only advances after a bounded browser challenge/reload failure.
- `legacy_rotating` preserves the existing session suffix behavior. `static_isp` passes credentials exactly as configured and never adds a session suffix.
- A malformed configured proxy is a fail-closed configuration/selection error; browser fallback must never silently become direct VPS traffic when proxy use is required.
- Internal proxy credentials may be consumed by the HTTP/browser client, but selection objects, repr, logs, exceptions, Telegram summaries, manifests, and reports contain only slot/kind/status and redacted host metadata.
- Every browser session gets a fresh profile/cookie scope. Page/detail caps and a runaway guard remain active even for unlimited ISP traffic.

## Telemetry

Only non-secret counters/flags are allowed: list browser navigations, legacy detail fallbacks, challenge reloads, proxy failovers, skipped pages, coverage, and optional encoded/approximate bytes only if measurement is stable and explicitly labelled non-billing.

## Audit report

The future read-only shadow audit returns a sanitized report with: run id, mode, source, page/detail counts, snapshot complete/incomplete counts, fallback counts, proxy slot/kind counters, skipped pages, coverage, no-write/no-Telegram assertions, threshold verdict, and redacted failure codes. It never returns raw URLs, cookies, credentials, captions, or inbound media.

## Protected contract

`src/telegram/monitor.py`, `src/jobs/pipeline.py`, `src/telegram/bot_delivery.py`, `src/telegram/delivery.py`, `src/post/template.py`, and `src/matching/**` remain byte-identical. New tests may import them, but implementation changes are forbidden.

## B0 gate

No P1 write access until unit baseline, pipeline probe, protected/golden hashes, ownership, production read-only preflight, and independent Agent B/C reports are all recorded on this exact baseline SHA. Any unknown red baseline or interface disagreement is `STOPPED`.
