# RentBelgrad: full-refresh results and remaining repairs, 2026-09-15

## Current controlled verification — 21:35 UTC

Production is025cfaf (exact indexed dedup); parser fixes and all-inventory nightly schedule
are installed. CI,1610unit/54browser, full local dedup equivalence,20-card public canary
and guarded production dedup reconciliation passed. Production dedup including DBwrite
completed198.856s; all7preservation checks passed.

Current full journal038a7a8a0f1e499196fa6b8358dd7857 started18:56:53UTC and is RUNNING.
All catalogues have finished; rolling details are still running:

| Source | Unique catalogue cards | Pages | New details | Failed pages | Coverage flag |
|---|---:|---:|---:|---:|---|
| Nekretnine |4147|314|2/2|0|source_total_changed|
| 4zida |4315|402|4/4|0|source_total_changed|
| Halooglasi |3266|168|5/5|0|source_total_changed|
| CityExpert |648|54|16/16|0|complete|

Catalogue Nek proxy counters:636operations,316HTTP403 recovered by browser,
4browser errors recovered,3session recycles, no failed slots or exhausted pool.
These are catalogue results only; long detail verification is not yet accepted.

Queue16364selected/16405inventory,41retry-deferred,0quota-deferred. At21:34:14UTC:
7110nominalverified,461errors,94deactivated,0skipped so far,992photos.
**Halo nominal successes are not final availability evidence:** retained inactive Halo
pages return HTTP200 with full price/photo JSON. Their explicit StateId and visible
source notice were not checked by025cfaf. The Halo source must be corrected again.
No CLIP embedding failures were found in the run log as checked20:09UTC. No fresh
outbound Telegram test was sent. Nonterminal skipped0 is not a full coverage proof.

Candidate8bf56fff73c26d2c7d5de508b63506a2c146797d supersedes ce8c9df anddb0a7c4:
1675unit/1skip,54browser,11live controls, GitHubCI35024569633 all passed.
It recognizes exact retained-inactive105/111/112 signals;101isactive;106andambiguous
responses remainUNKNOWN. Existing print404/generic redirects remain conservative.
It is NOT installed while025cfaf runs. Packet446files SHA
0acd4059dd39a3bf7195eb3ef528adbe99cbad5fc113e0a6436a9880c4566182.
See [evidence](2026-09-15-halo-explicit-removal.md).

A private correction child and receipt-gated supervisor are prepared locally, not installed:
all active Halo cards through the shared refresh core, then standard exact dedup;
all5timers owned/restored, before/after identity/media/manual-rights checks, unrelated
source rows unchanged except derived duplicate fields, removed content preserved,
selected/verified/error/deactivated/unattempted counters reconciled.26new supervisor
checks plus90existing revision-bound guard tests passed. A fresh8bfcanary is mandatory.
Healthy canary selection still requires live preflight because old Halo success flags
can describe inactive pages. No authority/threshold was weakened to force a green result.

IPRoyal20:54UTC:175.25GB/195.21GBremaining in the existing two2-IPorders, reset25Sep.
Delta1.38GB/0.01GBsince17:52 spansmultiplecataloguepasses andpartialdetails; it is not
one-night consumption. No purchases/cancellations. Current period order amounts total
$14.10; this is not a guaranteed renewal quotation or the account's complete charges.

Prior df3a88b run was interrupted by my separate otz-user diagnostic triggering its
strict guard; cleanup and timer restoration completed, and all derived-group
inconsistencies were repaired by standard dedup. Never launch extra otz-user processes
during a controlled run. Latest state and exact restart gates are in the
[resume checkpoint](2026-09-15-resume-checkpoint.md).

## Result recovered from the saved terminal evidence

Production827b904, controlled run b9f3f0e55db646a688fb0a45767c20e8:
14Sep14:00–21:08UTC, source quality degraded. 16050selected /16179inventory /
129retry-deferred;10511verified,1835errors,3704not attempted,358confirmed
source removals recorded during details,2310photos downloaded. Detail counters
were independently reconciled against the coherent after snapshot:

| Source | Attempted | Verified | Failed |
|---|---:|---:|---:|
| 4zida |5680|4440|1240|
| Halooglasi |1313|1097|216|
| Nekretnine |4480|4101|379|
| CityExpert |873|873|0|

Halo exhausted its source time allocation, leaving3704selected rows unattempted.
All five maintenance timers restored. The following automatic nightly run started
21:09UTC; its separate final counters were7789verified/4633errors/3386skipped,
15808selected,190retry-deferred. Do not confuse these later checkpoints with the
controlled run. The saved terminal copies and detail-audit preserve the first run.

Finder monitor active with0restarts;22sent delivery rows remain, latest13Sep12:59UTC.
This is service/ledger evidence, not a new delivery test. No messages sent by probes.

## Safety review of the failed supervisor invariant

Rights, overlays, inbound originals and changed public media passed. No listing
removed and id/platform/external_id remain unchanged. Only derived grouping changed:
1468dup_group_id and989is_primary fields. The existing dedup job deliberately
recomputes these after refreshing photographs. Independent after-snapshot review:
3600active groups; no singleton, wrong minimum-ID group key, missing/duplicate
primary, wrong primary rank (photo count, coordinates, lower ID), or ungrouped
nonprimary row. The old receipt remains failed; do not silently rewrite it as success.
Source quality remains degraded independently of this expected derived-field change.

## Confirmed Halo root cause and candidate correction

The remaining slow failures were HTTP400, not the earlier narrow print404 redirects.
27URLs recovered from the controlled log: fresh requests15Sep returned26detail
payloads and1catalogue redirect. Cookies reveal ViewedAds_YYYYMMDD grows by about
14bytes per distinct viewed ad. Controlled same-session proof:

- baseline HTTP200, detail marker present;
- tracking history18262cookie bytes: HTTP400, explicit request-header-size error;
- remove only viewed history: HTTP200,42cookie bytes, detail marker present.

Candidate discards only Halo ViewedAds_YYYYMMDD cookies before print requests,
preserving ASP.NET session, other cookies, proxy route and existing UNKNOWN logic.
Exact candidate file tested on VPS with initially oversized history:26/26available
cards parsed with price/area/photos,1UNKNOWN,30HTTP requests,max cookie header42bytes;
no browser, DB, photos or message writes. SHA5d142973d4fff081572416cd0e974a59f8600513997a48100b9fa99dca8b4a36.

## Exhausted proxy accounting

Automatic nightly logs show all Nekretnine slots exhausted around23:26UTC. The old
fair executor kept invoking the failed context, recording thousands of listing
errors/backoffs without further HTTP requests. Candidate stops that source on
NoProxyAvailableError, records its explicit source error and leaves remaining rows
unattempted; later sources continue. It does not reset failed proxy slots or override
upstream errors. A fresh four-ISP probe completed:3/3previously verified cards on each route,12/12complete. This confirms current availability, not thousands-of-cards stability. First probe had no browser
operations because the root process lacked its browser-cache path; invalid as a
proxy availability test. V2 explicitly uses the installed browser cache.

Local candidate checks:1575unit passed/1skipped (126.61s),54browser passed(59.88s).
Release c4a1454 installed; readiness200, monitor/site/admin active. GitHubCI34978620056 both jobs successful. Production canary20/20verified,0errors/skips,8safety checks pass and no residuals. Full run380cc7394a7e49229f58a8746f3c9086 started15Sep14:13:54UTC and is still running. No new spending or subscription
cancellations. Private evidence: `/opt/otz/data/research/acceptance-20260915` and
`/Users/mvstrike/Backups/RentBelgrad/2026-09-15/`.

## 4zida HTTP200 not-found response, subsequent candidate

The previous full had1240failed4zida detail attempts, mostly still-active old rows.
Direct read-only stratified sample20:18HTTP200 shells with no listing payload,
2real advertisements with price/area but no photographs. Three shells inspected in
detail contain duplicated Next.js meta redirects to the exact requested parent
category with `?redirection=not-found`, although the wire response remains200.

Candidate recognizes only canonical HTTPS www.4zida.rs rental detail URLs with a
24hex ID, unchanged response URL, HTTP200, absence of Apartment/Offer/RealEstateListing
JSON-LD, and every refresh meta tag carrying the exact observed source marker and
parent-category target. Generic empty pages, challenge/errors, wrong hosts/categories,
redirected responses and conflicting listing payloads remain unconfirmed. It raises
a distinct source-removal exception; no fabricated HTTP404. Detail refresh and link
probe recognize this explicit evidence, preserve old content/photos/success timestamps,
and mark the source listing DEAD. Public overlays/automatic-hide settings are unchanged.

Exact candidate SHA7a556ddc3848c2962e84343a3e1f3dd2e749b6e8085574ea1e19e6d9ca030791
read-only on server:18/20explicit source removals,2parsed without photos;5/5healthy
public controls remain complete.64targeted tests pass including ambiguous/contradictory
signals and content preservation. Full suite/browser checks and release pending.

Current controlled c4a1454 run may be cooperatively stopped before deployment of this
additional correction; preserve its actual terminal result and timer restoration.
Do not claim this intermediate run achieved the nightly objective.

## User-requested pause,15Sep14:36UTC

8681742 installed after CI34981550799 succeeded;1596unit and54browser checks pass.
Production canary20/20verified,300photo hashes,8safety checks pass, no residuals.
User requested a short stop while moving apartments. Controlled full8681742 was
cooperatively cancelled: terminal14:36:46UTC,6safety checks pass, no residuals or
SIGKILL,5timers restored. Monitor active,NRestarts0. No full-detail result on this
revision yet. No purchases/cancellations/test messages. Resume only on user request.
See [pause checkpoint](2026-09-15-resume-checkpoint.md) for exact gates and next steps.

## Resumed15Sep15:16UTC

User explicitly returned. Production8681742 and ordinary timers/monitor verified;
no new nightly run after the pause. Automatic offhost backup20260915-150857-e60adda9
started independently at15:08UTC and is still hashing the source, not accepted yet.
New full pass awaits fresh verified backup and matching public canary.

Authenticated IPRoyal order80643004 at15:23UTC:176.63GB remaining, reset25Sep,
final price$6.00. Other order80647128 earlier today:195.22GB,$8.10. Separate pools,
no purchase/cancellation. Before-pass usage baseline saved privately.

Additional bounded read-only probe on8681742: four last challenge-failed Nekretnine
URLs from the automatic-night log, on each existing ISP:16/16 complete with matching
identity, no source failures or poisoned slots. All16 HTTP403s recovered through
the browser; one additional initial browser retry recovered. No DB/photo/message
writes. This supports transient access/session failure, not permanent bad URLs;
it does not yet prove sustained full-night access. Probe ended; public timer restored
and no probe processes remain.

Bot read-only health16:04UTC:monitoractive,NRestarts0; last300log lines0ERROR/0WARNING,
logmtime16:03UTC. No inbound rows in last48h;22sent delivery rows remain, newest
13Sep12:59UTC. This does not independently prove source-channel inactivity and
is not a fresh end-to-end send test. No test messages sent.
