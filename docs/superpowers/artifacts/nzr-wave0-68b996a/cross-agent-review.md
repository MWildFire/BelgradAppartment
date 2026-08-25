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
release barrier. Integrated commit `ffcc3a6259ad8d94a34eb4caa93e6152613c7aca` adds the read-only
`nekretnine-shadow-audit` core/CLI and completes the bounded proxy failover state:

- `primary → secondary → metered` is selected after browser challenge/reload exhaustion;
- all candidates exhausted raises a sanitized `NoProxyAvailableError` rather than direct/silent success;
- proxy attempts, failovers, browser errors, failed-slot count, snapshot completeness, fallback,
  mismatch, detail error, skipped-page and coverage evidence are reportable without URLs, addresses,
  credentials, or raw payloads;
- the audit has no DB/session/orchestrator/stale-sweep/Telegram imports and its report writer is the
  only intentional local file side effect.

Evidence on this exact SHA: `1135 passed, 55 skipped, 34 warnings`; focused shadow/parser/proxy batch
`56 passed`; `pipeline-probe` `golden-0001`, confidence `1.000`, `phash`; `git diff --check` clean.
The full `ruff check src tests` remains a known repository baseline/version-drift failure and is not
claimed green. No production, Chrome, checkout, payment, Telegram, restart, or DB write occurred.

This continuation does not change the barrier: `B0 = STOPPED_PENDING_FRESH_INDEPENDENT_QA_PASS`.
The next required gate is an independent QA verdict on this exact integrated SHA; silence or a timeout
is STOPPED, never PASS.
