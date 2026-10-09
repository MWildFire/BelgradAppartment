# Verified freshness and owner monitoring (F08/F09/F13)

`src/scrapers/freshness.py` supplies independent helpers. The orchestrator remains the
owner of real scraper contexts, accepted listing/photo writes, price-change reporting,
source presence and publication policy. Nothing in this module sends messages.

## Integration contract

- `load_candidates(session, platforms=None, only_broken=False)` reads scalar metadata for
  active listings and authorized public originals. No descriptions or embedding blobs are
  loaded. The full inventory includes fresh and backed-off rows so reports keep the real
  denominator. Use the full inventory for `quota_estimates`, not a broken-only subset.
- `select_fair(inventory, limit=..., now=..., source_cursor=0, due_only=True)` returns
  `FairRefreshPlan(rows, source_order, next_cursor, eligible, deferred)`. Within each source:
  public first, broken next, then oldest **successful** refresh (attempt timestamp breaks
  ties). Round-robin slots keep every source represented and donate unused quota. Persist
  `next_cursor` if the quota can be smaller than the source count. Explicit retry backoff
  applies even with `due_only=False`.
- `run_fair_refresh(plan, callback, budget_sec=..., item_timeout_sec=...,
  context_factory=...)` holds only one source context/lease at a time. Contexts are reused
  for their source's rows and released before entering the next source. Startup, all item
  work and teardown share that source's reserved time slice. A hanging first source cannot
  spend the time reserved for later sources. Contexts/callbacks must honor cancellation;
  process termination for an uncooperative browser belongs to its adapter.
- `callback(candidate, context)` returns `CallbackResult(evidence, payload)`. The arbitrary
  payload can hold the existing refetch/price-change result. Returned executions include
  `attempted`, `attempted_at`, `completed_at`, evidence and payload. Context startup failure
  yields **unattempted** rows and a source error, never invented detail attempts.
- `assess_detail(detail, ready_photo_count=..., required_fields=...)` requires valid
  price/area and complete local-photo coverage by default. `ready_photo_count` must reflect
  usable files/hashes for the exact current URL set; remote URL count alone is not proof.
  A partial album, HTTP failure or missing required field cannot advance success.
- `record_attempt(session, listing_id, evidence, attempted_at=..., completed_at=...)` updates
  attempt/failure/backoff metadata. It advances `details_succeeded_at` only for complete
  evidence and never treats historical `details_refreshed_at` as success. Commit successful
  evidence in the same transaction as accepted content. Record failures in a fresh
  transaction after the failed callback. Older or replayed attempts cannot overwrite
  newer metadata or increment failure count twice. `dry_run=True` performs no writes.

When this helper owns metadata, disable the old `touch_refreshed_at=True` callback path:
its completion stamp would make the new start-time ordering guard reject the attempt.
404/410 source presence and automatic publication decisions remain with W1/W3; freshness
metadata alone does not authorize hiding a public apartment.

## Measured capacity, not a claimed SLA

Targets: public originals within 24 hours, at least 95% of active listings within 7 days.
`quota_estimates` reports current successful freshness, deficits, broken/backoff counts,
and a minimum daily **successful** refresh requirement. A measured success rate is needed
to estimate the number of attempts. No rate, or a measured zero rate, yields `null` for
attempt quota, since successful throughput is not established.

The sustainable lower bound is public count plus the remaining active target divided
across seven days. Example: 140 active listings including 14 public originals need at
least 31 successful refreshes daily; at a measured 50% success rate that is 62 attempts.
A quota is a capacity requirement, not proof that source failures are repaired. Actual
attainment requires new successful timestamps measured after real runs.

Source crawl and shadow reports expose `source_totals`: first/last/min/max source count,
observation and change counts, and UTC time/scope/page for the first, last and changed
observations. At most 64 change records are retained; `changes_omitted` counts the rest.
Nekretnine records only the unfiltered base search here, so price-band totals cannot be
mistaken for the global denominator. This telemetry does not change census trust or sweep
authorization. A changed total remains untrusted even if it later returns to the first value.
Failed shadow details additionally report sample kind, fetch/validation phase, exception
code, available HTTP status and missing required field names. Exception messages, source
URLs, response bodies and credentials are excluded; existing readiness gates remain intact.

Public-only maintenance writes `data/state/freshness/public_refetch.json`. The admin
health report exposes its sanitized `public_refresh` status, checkpoint time, selected/
verified/error/skipped counts and source error codes separately from `latest_run`.
Failures are visible immediately even while older successful data still meets the age
target. Missing/invalid/future evidence is explicit, unfinished progress becomes stalled
after 45 minutes, and no checkpoint for 13 hours is overdue. A public-only success never
establishes a full-source crawl success. Check systemd Result/ExecMainStatus as well:
failure before the first checkpoint cannot provide a new application run record.

## Quiet heartbeat events

`src/jobs/ops_monitor.py` consumes a private JSON snapshot of
`src.admin.operational_health.build_operations`. By default it only reads inputs and
returns structured JSON. It has no Telegram, email, network or publication transport.

```bash
.venv/bin/python -m src.jobs.ops_monitor \
  --health-file /private/operations-health.json \
  --previous-state /private/operations-monitor-state.json
```

Output includes `emit`, `kind`, added/resolved attention codes, completed-run status and
`next_state`. Unchanged polls, normal running transitions, pending-queue growth and small
freshness changes on the same side of a target remain quiet. A newly completed automatic
run, threshold crossing, backup expiry/recovery, stalled progress or failed-delivery
change produces an event. Raw summaries, exception messages, recipient IDs and media URLs
are omitted. Missing source evidence stays unknown/actionable, never healthy by default.

The heartbeat operator decides whether/how to notify Dmitry and persists `next_state`
after handling an event. An explicit `--write-state /private/state.json` is available for
local state persistence; the parent must be mode 0700 and the state is atomically written
mode 0600. Even with that option, the helper never sends a message. A saved monitor state
is not proof that an external notification or automatic refresh occurred.

Public-only completions have their own identity and status in the monitor state.
Each newly completed public run or changed failure condition emits one event; unchanged
polls and ordinary progress stay quiet. Main nightly records cannot overwrite that
comparison state. These events still do not send any external messages themselves.
