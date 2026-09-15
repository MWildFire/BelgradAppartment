# RentBelgrad resume checkpoint — 2026-09-15

User returned with “Продолжай работу”; work resumed15:16UTC. The pause evidence
below is historical. Production8681742/monitoractive and all5timers verified; no
new nightly run after the pause. Automatic backup20260915-150857-e60adda9 is
already running, not accepted yet. Source snapshot completed;1097134files,
2.797GBchanged/71.762GBunchanged. Local generation assembly in progress at15:51UTC.
Read-only last-four-failed-card probe on all4ISP succeeded16/16; proof is private
nekretnine-failed-routes-proof-8681742.json; no residuals, public timer restored.
Wait for verified receipt before matching new
canary/full pass. New full output suffix planned:8681742-resume1516.

IPRoyal order80643004 checked15:23UTC:176.63GB remaining, reset25Sep, price$6.00.
The first order had195.22GB remaining today. No purchase/cancellation.

## Current continuation,16:18UTC

Fresh automatic backup **20260915-150857-e60adda9** accepted: data/application/offhost/
protected checks pass and published server receipt matches. Created15Sep15:09:26UTC,
schema0014; offline restore catalog/apartment/photo200 and photo decoded.
Root accepted receipt: `/opt/otz/data/research/acceptance-20260915/accepted-backup-resume1516.json`.
SHA256 `b4b87df38b3a78b219597646e5e01e41174f244da72f53b75f2f52e1ec2a4c29`.
Manifest SHA `cab9c75c7c21bc1e0d65316fb8e716491e6a7d991d2092819da73cfba62ae470`.
New canary unit `rentbelgrad-public-canary-8681742-resume1516`, output same suffix
under acceptance-20260915. Public timer pause is recorded in
`resume1516-public-timer-before.json`; restore it after clean canary completion,
then full supervisor owns its own timer pause. Canary passed16:20:27UTC:20/20,
275photo hashes,8checks,clean cleanup; resultSHA
`80cb741afa0bc8a03a6c0d38db5eb0fbd7366ebaf45bde7ef6fab8d336d3c067`.
Public timer restored before full launch. **Full running since16:21:57UTC**,
journal`eacfb60645004cf0bd9c19d4ca874bc6`, unit`rentbelgrad-full-refresh-8681742-resume1516`.
Output`/opt/otz/data/research/acceptance-20260915/full-refresh-8681742-resume1516`.
Companion`rentbelgrad-full-terminal-8681742-resume1516`. New units use KillMode=mixed
and SendSIGKILL=no; full36,000sec bound,180secstop. Status helper
`/usr/local/lib/rentbelgrad-recovery/full_refresh_status_8681742_resume1516.py`
takes this exact journal ID. Do not start competing maintenance while running.
Monitoractive/NRestarts0. Full acceptance remains pending.

## First actions on resume

1. Production code is **8681742246115bf3cfc8a652c08b2e6d35ca08fe**. Branch
   `codex/nightly-full-20260914`, code pushed; any newer local commit may be docs only.
2. SSH key `/Users/mvstrike/Backups/RentBelgrad/offhost/ssh/rentbelgrad-backup-key`,
   host `ubuntu@3.121.163.130`, application `/opt/otz`.
3. First inspect terminal receipt for the user-cancelled
   `rentbelgrad-full-refresh-8681742`, folder
   `/opt/otz/data/research/acceptance-20260915/full-refresh-8681742`.
   Check owned cleanup, all five timer restorations, current monitor and release.
4. Check whether an automatic nightly run has occurred since the pause. A live
   freshness checkpoint may belong to a later run: use the exact run journal and
   saved terminal copies, not just the newest counters.
5. If a new controlled full pass is needed, first recheck backup freshness and
   current maintenance writers. Use the revision-pinned8681742 supervisors below.
   Do not start competing writers or timers while a supervisor owns their pause.
6. Finish verification of long-run Halo behavior, Nekretnine pool stability and
   remaining source coverage gaps. No full pass of8681742 has been accepted yet.

## Installed fixes and evidence

- Halo: only its growing ViewedAds date cookie is removed before print requests;
  ASP.NET session, other cookies and routing preserved. Reproduced200→header-size400
  →200 in the same session. Exact candidate26complete/1UNKNOWN, max42cookie bytes.
- Exhausted Nekretnine pool: stop the source once, record remaining rows unattempted
  instead of thousands of false callback attempts/backoffs. No pool reset/bypass.
- 4zida: exact canonical HTTP200 Next.js parent-category redirect with
  `redirection=not-found`, without any listing JSON-LD, becomes a distinct explicit
  source-removal error. No fabricatedHTTP404. Wrong hosts/paths, challenge/errors,
  conflicting payload and generic shells remain unknown. Old content/photos/success
  timestamps are preserved; source availability changes, not publication rights.
- Read-only4zida proof:18of20old failures are explicit removed responses,2real
  listings lack photos;5/5healthy controls complete. Candidate SHA
  `7a556ddc3848c2962e84343a3e1f3dd2e749b6e8085574ea1e19e6d9ca030791`.
-8681742 checks:1596unit passed,55skipped (54browser tests gated in this invocation
  and tested separately; one other skip),54browser passed, no new lint,5shell checks.
  GitHubCI34981550799 both jobs success.90supervisor tests pass.
- Activation: schema0014, healthz/readyz200, monitor/site/admin restarted successfully.
-8681742 public canary:20/20verified,0errors/skips,300photo hashes,8safety checks,
  no residuals/SIGKILL. Receipt SHA
  `b3847f2cf96b513b711a4a8451d0ecd5dd4332a0122cfdcd464df2ae71a7ecc9` at
  `/opt/otz/data/research/acceptance-20260915/public-canary-8681742/runner-result.json`.
- Telegram getMe15Sep14:31UTC: authenticated,HTTP200, no message sent.
  Ledger22sent rows, latest13Sep12:59:36UTC. Service/auth evidence is not a new send test.

## Earlier runs and honest interpretation

Old full827b904, journalb9f3f0e55db646a688fb0a45767c20e8, finished14Sep21:08UTC:
16050selected/16179inventory/129retry-deferred;10511verified,1835errors,3704skipped.
Per-source attempted/verified/errors:4zida5680/4440/1240;Halo1313/1097/216;
Nekretnine4480/4101/379;City873/873/0.358of379Nekretnine errors were actual404/410.
Halo timed out because of growing viewed-history cookies. The following automatic
night has separate7789verified/4633errors/3386skipped counts and incomplete source
coverage. Never attribute those later counters to the controlled run.

Old full identity check was falsely strict on recomputed dedup fields. Separate
review confirmed immutable id/platform/external_id, no deleted rows, and3600valid
active duplicate groups. Old failed receipt is retained. New full supervisor checks
immutable identity and derived-group consistency separately and preserves terminal
cursors before restoring timers. Source-quality criteria are unchanged.

Intermediate c4a1454 full journal380cc7394a7e49229f58a8746f3c9086 was cooperatively
cancelled to include4zida repair: terminal14:29:05UTC, full_refresh_degraded,
KeyboardInterrupt, no residuals/SIGKILL,6safety checks pass,5timers restored. It had
not reached rolling details. Its terminal detail audit correctly rejects older
copied cursors; those are NOT counts from that cancelled run.

## Backup and continuation commands

Accepted generation20260914-090745-3331d106, created14Sep09:08:13UTC,36h gate expires
15Sep approximately21:08UTC. Offhost/protected restore verified, but the earlier
automatic source-snapshot failure has not been diagnosed as fixed.
Receipt `/opt/otz/data/research/acceptance-20260914/accepted-backup.json`, SHA
`c81cc2efb2587997d22026ef54c5572b607e77320400d179efa2ba5a9cb529f2`.
A later release/full run may require a fresh accepted generation and matching canary;
never change timestamps or reuse mismatched receipts.

Root helpers under `/usr/local/lib/rentbelgrad-recovery/`:
`rentbelgrad-public-canary-runner-8681742.py`,
`public_preventive_canary_8681742.py`,
`rentbelgrad-full-refresh-runner-8681742.py`,
`full_detail_audit_8681742.py`, `capture_full_terminal_8681742.py`.
Their local copies and tests are in `/Users/mvstrike/Backups/RentBelgrad/2026-09-15/`.
Full child: `nightly-refresh --max-pages=500 --no-notify`,34320sec bounded child,
36000sec systemd outer bound. The supervisor owns release lock and5timer pause;
stop it via SIGTERM to its main process, allowing owned SIGINT cleanup. No SIGKILL.
A second run needs a new output directory; private receipts use exclusive creation.

## Proxy cost/limits and outstanding decisions

No purchases or subscription cancellations. Four current SerbiaISP remain active
through25Sep. Existing orders were$8.10+$6.00 for the current period (not a guarantee
of future renewal). New spending$0. Four-route Nekretnine probe today:3/3each,12/12.
Small probes do not prove full-night stability.

Official FUP checked15Sep:100GB per proxy per30day cycle, pooled inside each order;
dedicated ISP slow down after exhaustion. Dashboard order80647128 shows195.22GB
remaining, reset25Sep, final price$8.10. The other order's current traffic has not
been read yet. Do not aggregate separate order pools as freely interchangeable.
Source: https://help.iproyal.com/en/articles/12094903-what-is-the-fair-usage-policy

Residential still exhausted (dashboard-0.02/6GB). Separate2GB/$11.90 subscription
renewal20Sep and cancellation question from14Sep remain unresolved; do not cancel
from a generic continuation. Paid routes are existing ISP only; photos direct,
heavy browser assets blocked; City/Halo direct,4zida dedicatedISP,Nekretnine4ISP.

Bothauto-hide flags false, snapshots shadow, noauto-publish, failed/unknown data
preserved. Full nightly schedule21UTC (23Belgrade in September); no AWS migration,
new Codex heartbeat, new tasks, subagents or test sends. Resume only on user request.

## Pause cleanup

Confirmed15Sep14:36:46UTC:8681742full terminal full_refresh_degraded from requested
KeyboardInterrupt; no residuals, no SIGKILL, all6safety checks pass,5timers restored,
no restoration error. Monitor active,NRestarts0. Terminal companion exited after
rejecting earlier rolling cursors: this run was cancelled before rolling details,
so no fresh full-detail result exists. Both controlled supervisors and companions
are stopped. Ordinary app timers remain enabled. Canonical checkpoint saved locally;
resume work only after the user returns.
