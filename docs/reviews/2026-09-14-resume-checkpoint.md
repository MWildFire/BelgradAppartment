# RentBelgrad: pause checkpoint — 2026-09-14 18:05 GST

User asks whether Codex/computer can be closed and work resumed at a convenient time.
Yes: server run is independent of this Mac. Do not call the full-refresh objective complete.

## First actions on resume

1. Read `docs/reviews/2026-09-14-nightly-coverage-research.md` for research and release evidence.
2. SSH using existing private key `/Users/mvstrike/Backups/RentBelgrad/offhost/ssh/rentbelgrad-backup-key`, host `ubuntu@3.121.163.130`.
3. Run root read-only helper `/usr/local/lib/rentbelgrad-recovery/full_refresh_status_827b904.py` with `/opt/otz/.venv/bin/python`.
4. Inspect `/opt/otz/data/research/acceptance-20260914/full-refresh-827b904/runner-result.json`, `comparison.json`, `full-journal.json`, `detail-audit.json` and `terminal-refetch.json` / `terminal-rolling_cursor.json` when present. Reconcile source attempts/success/errors/skipped and retry-deferred; do not count source catalogue discovery as detail verification.
5. Verify cleanup and restoration of all five timers. While the full supervisor is active, DO NOT start maintenance timers or competing otz helpers: it owns the pause and rejects unrelated maintenance writers.
6. Verify Finder service and natural delivery evidence read-only; no test messages/replay. Update final report and vault with actual results, remaining gaps and price. Do not claim 100% freshness from a small healthy sample or a running job.

## Running independently on server

- Production revision `827b904c7d405a56c289eb33c96edbc124630934`, schema0014.
- Unit `rentbelgrad-full-refresh-827b904`.
- Journal `b9f3f0e55db646a688fb0a45767c20e8`, started14:00:01 UTC.
- 14:05 UTC: active, Nekretnine117 pages/0failed; other checkpoints still belong to earlier run.
- Supervisor `/usr/local/lib/rentbelgrad-recovery/rentbelgrad-full-refresh-runner-827b904.py` owns timers and restores after confirmed cleanup. CLI `nightly-refresh --max-pages=500 --no-notify`; 34320s child budget / 35520s systemd bound.
- One-shot root companion `rentbelgrad-capture-full-terminal-827b904` waits for terminal receipt, saves freshness checkpoints before later jobs replace them, then runs read-only per-source audit. Its failure does not alter DB or control the full job. Saved checkpoints permit later audit if live counters have moved.
- No Codex heartbeat or recurring automation was created. Resume manually with user.

## Accepted release evidence

- 1573 unit passed /1 skipped,54 browser passed; CI34851632065 both jobs success.
- 827b904 contains health report correction (100%/24h, actual all-inventory scope) and narrow Halo print error redirect UNKNOWN fast path. On six production direct/ISP probes: UNKNOWN0.18–0.566s, no browser/DB/photos/messages. Canonical403 challenge persisted on both routes; no additional proxy purchase resolves those observed links.
- Public preventive canary:20/20 verified,283 photo hashes verified,8 safety checks PASS, no concurrent activity or residuals. Deliberately previously verified sample,5 per source; NOT overall source-health evidence.
- Canary receipt `/opt/otz/data/research/acceptance-20260914/public-canary-827b904/runner-result.json`, SHA `da0f847da032256241b7623fa75342702e1bf203fd81fd4ac6b276610cbf1f9e`.
- Backup generation `20260914-090745-3331d106`, accepted09:08UTC, protected/offhost restore verified. Manual backup success does not prove automatic backup fault resolved. Receipt freshness must be rechecked before any further release (36h limit).
- Prior d0 controlled run deliberately cancelled13:48:50 for Halo timeout bottleneck:1929verified/224errors, no skipped reported before cancellation. City871attempts/869verified; Halo1282/1060. All five invariants and112public albums passed; all timers restored, no SIGKILL/residuals. Never call that a completed full refresh.
- Finder getMe authenticated13:29UTC. Last independently evidenced natural sends:22 sent rows,11matches×2recipients, latest Sep13 12:59UTC. No new send test.

## Constraints and remaining decisions

Keep auto-hide flags false, Nekretnine snapshots shadow, failed/unknown fields/photos preserved, Dasha's publication rights and overlays untouched. All-inventory queue ignores numeric3000 cap, retry cooldown up to12h; shared detail25200s and full34200s settings. Full schedule21:00UTC daily (23:00Belgrade in September), regular public checks08/14/19UTC.

Cheap tested routing: City/Halo direct,4zida dedicated ISP route,Nekretnine4ISP pool/recycle100. Existing ISP period $14.10, $0 new spending. Separate Residential2GB/$11.90 renewal Sep20 shows Missing payment method; explicit cancellation question remains unanswered. Do not cancel or buy based on “continue”.

Private artifacts: `/Users/mvstrike/Backups/RentBelgrad/2026-09-14/`. Never display `.env`, proxy credentials or private rollback env. No AWS migration. No subagents unless explicitly authorized.
