# Delivery recovery contract — 8 September 2026

New match sends claim `(match_id, recipient_id)` before transport. `sent` requires positive provider message IDs. A partial success keeps the existing Render Finder caption, media order, publication result and recipients; only failed recipients enter the automatic retry queue. Historical `PublishedPost` rows without recipient evidence remain historical, receive no fabricated confirmations and are not replayed.

`src.telegram.bot_delivery.retry_pending_match_deliveries(limit=30)` is the bounded scheduled entry point. It covers only existing pending/failed deliveries for current configured bot recipients and excludes rejected matches. A process interruption or acceptance followed by a transport timeout leaves `unknown`, never an automatic resend. A stale claim becomes unknown after 15 minutes. Even `force_resend` cannot bypass an unresolved unknown outcome.

An operator first checks provider or recipient evidence, then calls `src.telegram.delivery_ledger.reconcile_match_delivery` with confirmed message IDs, or `confirmed_not_sent=True` after establishing non-delivery. This helper does not send. Existing explicit manual resend behavior remains for known delivered/historical messages.

Subscription discovery persists all matching items before transport and before the digest cap. Its cursor covers later scheduler gaps; its backlog survives the initial 24-hour window. Claims and confirmations are per subscription/card. A 100-card backlog drains 30/30/30/10 even after a 72-hour transport outage. Missing configuration returns no confirmation and never writes `SubscriptionMatch`. Queue completion and confirmed ledger insertion share one transaction. Unknown subscription outcomes require `src.jobs.subscription_outbox.reconcile_unknown` after verifying the outcome. Dry-run changes neither queue, cursor, ledger nor transport.

Queue delivery rechecks current public authorization and criteria. Unpublished/unavailable candidates remain unsent, do not consume the digest cap, and can become eligible again when the underlying public apartment recovers. No job performs historical mass repair.

Photo GET/HEAD authorization follows raw public overlays, including permitted secondary and manually retained direct cards. Caddy must forward `/photos/...` unchanged to the app, with no-store responses. Existing immutable browser/CDN copies cannot be recalled; any prior edge cache needs separate purge/expiry accounting at rollout.


Public visibility and availability are separate facts. Catalogue, home, map, sitemap and direct URLs retain authorized public overlays until the shared publication policy changes `is_public`. `active`, `unknown` and `dead` are displayed truthfully: unknown reads «Актуальность уточняется», and dead carries the existing снято badge. Pin/warn-only/cap cannot be bypassed by a read-model filter. The old `show_rented` query remains accepted as a no-op; its former toggle is removed. Recommendations (subscriptions and similar apartments) skip confirmed dead candidates, while retaining their unsent queue rows for possible reactivation.
