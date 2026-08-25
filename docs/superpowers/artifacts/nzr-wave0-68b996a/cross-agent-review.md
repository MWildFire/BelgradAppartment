# P0/Wave 0 cross-agent review

Base: `68b996a8c23da5f7dd5607f54d7b296980725b48`

## Findings

- Agent A initial dirty-diff audit: PASS. Its later golden review STOP was valid against the first synthetic manifest: fixture values, identity semantics, and caption platform text were wrong. Captain corrected the manifest from actual independent list/detail fixture parsing and reran parser/render/hash checks locally.
- Agent B initial config/orchestrator/CLI audit: PASS. Its later review identified the current H1 Nekretnine verify-links proxy path versus the original plan's overly broad “liveness direct” wording. Captain amended the plan and interface freeze before P1 so current liveness transport/semantics are frozen and snapshot proxy routing cannot leak into liveness.
- Agent C baseline QA: PASS for tests/probe/protected shard, STOP for missing full golden. After Captain created the golden, its later review still read the pre-correction/stale artifact state and STOPped on issues that are now corrected in the checked files.

## Captain revalidation after the STOPs

- Raw `list.html` and `detail.html` hashes match the corrected manifest.
- `NekretnineScraper.parse_detail()` and independent list payload derivation match the corrected `468187` fields and ten XXL photo URLs.
- Protected `render_post` text and Bot full-caption hashes match the corrected manifest exactly, including `Platform.NEKRETNINE`.
- `matcher.py` protected blob hash is 40 hex and all 13 protected blobs match `HEAD`.
- Baseline remains `1108 passed, 55 skipped`, `1163 collected`, `pipeline-probe` PASS.
- Production read-only evidence remains endpoints `200/401`, Alembic `0011`, SQLite `quick_check ok`, and latest service results success; no production action was taken.

## Captain-side revalidation at 2026-08-24 17:52 GST

- A detached validation worktree at exact `2a6e59e1a59dca860a885c664ac9fad40c822739` reproduced
  `1108 passed, 55 skipped, 34 warnings` and the golden pipeline probe
  `listing=golden-0001`, `confidence=1.000`, `method=phash`.
- The golden manifest sidecar SHA-256 and the embedded baseline SHA-256 match; all 13 protected blob
  hashes match, `git diff --check` is clean, and the validation worktree is clean.
- `ruff check src tests` reports 901 pre-existing findings; no lint or production-code change is made in
  Wave 0. A fresh independent Merge Guardian was retried and rejected again by the worker-thread limit.

## Provisional P1 implementation after worker-runtime blocker

Per the documented Captain exception, implementation moved forward without claiming a B0 PASS. The
Captain closed three P1 workers that remained in `running` without returning commits, then implemented
the disjoint lanes locally on clean integration history:

- `258a69e` + `d163110`: `ListingPreview.snapshot`, Nekretnine list/detail parity parser, strict XXL
  photo allowlist, typed snapshot mode, secret-safe proxy pool, fail-closed selection, and actual
  Nekretnine HTTP/browser routing through the pool.
- `6a3db71`: `legacy`/`shadow`/`active` orchestrator behavior, complete-snapshot no-detail path,
  incomplete fallback, shadow mismatch logging, non-null existing-row merge, and full-photo swap guard.
- `c03354e` + `9668cda`: immutable Render Finder parity tests and protected/golden hash assertions.

Evidence on final integrated implementation SHA `1788f86b3a2d5b850cb0a3f951dde1f75efbdf94`: full suite
`1130 passed, 55 skipped, 34 warnings`;
P1 parser/orchestrator gate `102 passed`; parity suite passed twice consecutively (`5 passed` each);
pipeline probe remains `golden-0001`, confidence `1.000`, `phash`; all 13 protected hashes remain equal.
This is implementation progress, not a release authorization: B0/B1/B2 remain blocked until a fresh
independent QA Guardian returns PASS on the integrated SHA.

The final short Guardian attempt was started after the implementation and did not return any verdict
within 60 seconds; it was closed as a worker-runtime failure. No PASS is inferred from silence.

## Barrier decision

`B0 = STOPPED_PENDING_FRESH_INDEPENDENT_QA_PASS`.

The corrected artifacts are ready, but the fresh independent Guardian runtime did not produce a PASS;
the provisional P1 exception therefore remains active only for the Captain-owned implementation already
listed above. No further release wiring, Chrome, payment, production env change, restart, Telegram send,
or production activation is authorized from this state.

## Captain-owned continuation — 2026-08-25

The user-side blocker is absent, so Captain continued the local implementation without changing the
release barrier. Integrated code chain through `d8b298a1dd2b08080a9d78bdc1fa382228251325` adds the read-only
`nekretnine-shadow-audit` core/CLI and completes the bounded proxy failover state:

- `primary → secondary → metered` is selected after browser challenge/reload exhaustion;
- all candidates exhausted raises a sanitized `NoProxyAvailableError` rather than direct/silent success;
- recoverable challenge attempts are counted separately from residual browser/proxy errors, so a successful
  failover is not reported as an unresolved audit failure;
- proxy attempts, failovers, browser errors, failed-slot count, snapshot completeness, fallback,
  mismatch, detail error, skipped-page and coverage evidence are reportable without URLs, addresses,
  credentials, or raw payloads;
- the audit has no DB/session/orchestrator/stale-sweep/Telegram imports and its report writer is the
  only intentional local file side effect.

Evidence on this exact SHA: `1136 passed, 55 skipped, 34 warnings`; focused shadow/parser/proxy/CLI batch
`57 passed`; `pipeline-probe` `golden-0001`, confidence `1.000`, `phash`; `git diff --check` clean.
The full `ruff check src tests` remains a known repository baseline/version-drift failure and is not
claimed green. No production, Chrome, checkout, payment, Telegram, restart, or DB write occurred.

## P2 telemetry plumbing — 2026-08-25

Captain connected the snapshot/proxy evidence to the normal scrape path in code commit
`2fcb3b35a4adcbb511febe770605d5482d440555`: `ScrapeResult` now carries snapshot mode, complete/incomplete
counts, active uses, fallback rate, shadow mismatch count, coverage rate, and integer-only proxy metrics;
the standard CLI scrape summary prints the non-secret counters. Focused telemetry/orchestrator tests:
`16 passed`. Full integrated suite: `1140 passed, 55 skipped, 34 warnings`; pipeline probe and parity
remain green. No DB schema or protected Render Finder files changed.

This continuation does not change the barrier: `B0 = STOPPED_PENDING_FRESH_INDEPENDENT_QA_PASS`.
The next required gate is an independent QA verdict on this exact integrated SHA; silence or a timeout
is STOPPED, never PASS.

## Fresh independent barrier attempt — 2026-08-25

Three new read-only workers were launched against the exact code SHA `2fcb3b35a4adcbb511febe770605d5482d440555`:
full/parity QA, security/no-write audit, and docs/evidence consistency. All three remained `running` for
the bounded 45-second window and were closed without a verdict. No PASS is inferred; B0 remains
`STOPPED_PENDING_FRESH_INDEPENDENT_QA_PASS`. No production, Chrome, payment, Telegram, or DB action occurred.

## Shadow report contract completion — 2026-08-25

Captain closed the remaining local report-contract gap in code commit `1d4bebb`: every
`ShadowAuditReport` now carries a generated `run_id`, fixed `source=nekretnine`, fixed `mode=shadow`, and
explicit `no_db_writes`, `no_telegram_sends`, and `no_stale_sweep` assertions. The serialized report keeps
these fields alongside the existing sanitized counts and mismatch evidence; no URL, address, credential,
or raw payload is added. The five shadow-audit tests cover the in-memory and JSON contracts, and the full
suite remains `1140 passed, 55 skipped, 34 warnings`.

This is still Captain-owned local implementation, not an independent QA verdict. The next fresh QA must
target exact integrated SHA `1d4bebb`; B0 remains `STOPPED_PENDING_FRESH_INDEPENDENT_QA_PASS`. No production,
Chrome, payment, Telegram, restart, deploy, or DB action occurred.

## Fresh QA retry on report-contract SHA — 2026-08-25

Three new read-only workers were started on exact SHA `1d4bebb` for full/parity, security/no-write, and
docs/evidence review. All three stayed `running` through the bounded 45-second wait and were closed without
verdict. This is a repeated worker-runtime STOP, not a QA PASS and not a user-side blocker. B0 remains
`STOPPED_PENDING_FRESH_INDEPENDENT_QA_PASS`; no external side effect occurred.

## Generic completeness hardening — 2026-08-25

Captain found and fixed a real audit classification gap in `d2c36e0`: when a scraper did not expose its
platform completeness helper, the generic path could treat a snapshot with coordinates/photos but missing
price or other bot-visible fields as complete. The fallback now requires the full frozen text/numeric field
set and strict `pic.nekretnine.rs/image/<numeric-id>/xxl.jpg` URLs; a new negative fake-scraper test proves
that the partial snapshot is classified as fallback without calling legacy detail. Focused shadow tests are
`6 passed`; the full suite is `1141 passed, 55 skipped, 34 warnings`; probe, parity twice, shell syntax and
diff-check remain green.

This is Captain-owned hardening, not an independent QA verdict. The next fresh QA target is exact SHA
`d2c36e0`; B0 remains `STOPPED_PENDING_FRESH_INDEPENDENT_QA_PASS`. No production or other external side
effect occurred.

## Fresh QA retry on completeness-hardening SHA — 2026-08-25

Three new read-only workers were started on exact SHA `d2c36e0` for full/parity, security/no-write, and
docs/evidence review. All three stayed `running` through a bounded 60-second wait and were closed without
verdict. This is a worker-runtime STOP, not a QA PASS and not a user-side blocker. B0 remains
`STOPPED_PENDING_FRESH_INDEPENDENT_QA_PASS`; no production, Chrome, payment, Telegram, restart, deploy, or
DB action occurred.

## Shared parity contract hardening — 2026-08-26

Cross-review found that orchestrator shadow telemetry and the standalone audit had drifted: the former
compared raw photo URLs and omitted `external_id`/URL while the latter normalized photo IDs and included
address/title. Commit `208a35e` adds pure `src/scrapers/snapshot_parity.py` and makes both paths use the same
field set and normalization. Focused contract/orchestrator tests: `23 passed` before integration and the
full suite is `1142 passed, 55 skipped, 34 warnings`; detached exact gate also records `1197 collected`,
probe PASS, parity `5 passed` twice, protected diff empty, syntax and diff-check clean.

The prior B0 STOP is superseded: independent full QA, security/no-write QA, and evidence QA had PASS on
the preceding contract SHA; fresh final B1/B2 observers are being rerun on exact `208a35e` after this fix.
No external action has started yet.

## Photo ID normalization hardening — 2026-08-26

The final security review found that the shared helper still extracted `xxl` rather than the numeric photo
ID from `/image/<id>/xxl.jpg`, allowing different photos to compare equal. Commit `eb6611e` fixes the
numeric path-segment extraction and adds a negative audit test. Focused contract tests are now `24 passed`
and the full exact gate is `1143 passed, 55 skipped, 34 warnings`, `1198 collected`, probe PASS, parity
`5 passed` twice, protected diff empty, syntax/diff-check clean.

This invalidated earlier final QA evidence on `208a35e`; fresh independent B1/B2 verdicts must target
exact `eb6611e`. No external action has started.
