# RentBelgrad resume checkpoint — 2026-09-15

## Current 22:03 UTC — combined City/Halo candidate final-tested; 025 full still running

A second retained-inactive bug was confirmed in CityExpert. HTTP200 full photo/price
payloads with FINISHED/NOT-AVAILABLE/KAPARISANA-RENT were counted successful by025.
Canonical Chrome independently confirmed all3 as unavailable; PUBLISHED control has
an enabled viewing action. 647unique live Search results;251of882pre-full active IDs
absent;8stratified absent controls explicitly unavailable. Absence alone is not removal.
New strict URL/propId/cityId/rental-context/status checks include fourth confirmed state
PUBLISH-PAUSED-OVERLOADED (80063canonicalUI: no viewing, disabled Nijeuponudi).
882storedstatuses:206FINISHED/18NOT-AVAILABLE/24KAPARISANA/1PAUSED/633PUBLISHED.
All249unavailable were counted successful by025; these are storedresponses, notfresh
networkproof for all249. 89focusedtests and12livecontrolsPASS.
Finalunit1719passed/1skip119.56s;browser54passed52.46s;lint/shell/diffPASS.
Alllocaltest/probe sessions finished. 578d24f was earlier combined candidate, now
superseded by fourth-status addition; final new revision pending commit/CI.
Do not install8bfHalo-only packet as final. Need combined revision and correction of
BOTH Halo and CityExpert after025terminal, with fresh healthy source-verified canary.
Private8bfHalo-only correction helpers are prepared/tested but must be superseded for
combined scope/revision. Details:2026-09-15-cityexpert-availability.md.
Current old success counters for BOTH sources overstate truthful availability.
Watcher42424 is now consumed by functions.exec cell482; do NOT poll42424 concurrently.
Use functions.wait(cell_id=482,yield_time_ms<=60000); it emits500verified/50error milestones.
Latest21:49:18UTC:7506nominalverified,486errors,119deactivated,0skippedsofar. StillNek.
Cityprobes51224/62960/97201finished. Cityresearchtabs closed.
Combined578privatehelpers prepared:90guardtests+28correctiontestsPASS, notinstalled.
Rebind them to finalrevision; 8bfHalo-only helpers are obsolete.
No production mutations, purchases or messages during this continuation.

## Historical 21:27 UTC — Halo-only candidate (superseded by combined City work)

CRITICAL NEW FINDING: Halo retained HTTP200 detail pages can be INACTIVE despite full
price/photos/JSON. Current025full Halo success counts are NOT final truthful availability.
5016unique detail HTTP200IDs inlog,1762absent fromlocaldefaultcatalogue;8stratified samples
all carry StateId105/111/112 and source visible "no longer offered" notice.
CanonicalChrome confirmed105/111/112 inactive,101healthy; rawHTMLalone containshidden
expired templates evenwhenhealthy, so generic text matching is forbidden.
SourceJS removes expired notice for101/106;106iswaitingmoderation,keepUNKNOWN.
Newlocalfix: only101confirmedactive;105/111/112 +exactidentity+sourcebanner+visibility
binding/renderedstyle =>SourceListingInactiveError;unknown/pending/ambiguous=>UNKNOWN.
Preserveoldphotos/content/success;browserinactiveexceptions donotretry/recycle.
109targetedtestsPASS,11live(8inactive+3healthy)PASS;final1675unit/1skip(212.22s),54browser(63.70s),lint0new.
Alllocaltests/probesfinished;onlywatcher42424running.
Firstfullpreformat1671unitPASS;finalincludes4additional hidden/contradictoryvisibilitycases.
DO NOT deploy db0a7c4 or ce8c9df: superseded by 8bf56fff73c26d2c7d5de508b63506a2c146797d.
GitHub CI35024569633 SUCCESS (unit5m34s/browser1m54s), exact local receipts saved.
Packetrelease-8bf56ff.tar.gz446files SHA0acd4059dd39a3bf7195eb3ef528adbe99cbad5fc113e0a6436a9880c4566182.
NOT installed. Progress21:24UTC:6864nominalverified/419errors/52deactivated; Halo successes need correction.
IPRoyal20:54UTC:175.25GB/195.21GB remaining; delta1.38GB/0.01GB since17:52 spans multiple passes, not one-night cost.
Private8bfcanary/fullhost clones passed90guardtests. NewHalo corrective child+runner
preparedLOCALONLY;26counter/scope/lifecycle/restore testsPASS. Requiresfresh8bfcanary;
healthy sample needs fresh source preflight due falselyhealthy oldHalo rows.
After current025terminal, install finaltestedrevision and reconcile Halo again; do not
accept currentHalo nominalverifiedcounts. KeepcurrentlongNek/4zidaverification running.
Privateproofs halo-off-catalogue-sample.json,halo-off-catalogue-metadata-2100.json,
halo-inactive-canonical-ui-proof.json,halo-inactive-candidate-proof-final.json.

Production025cfafaf624d38072b0eb58db8617e12924462c, schema0014.
Dedup recovery terminal18:55:44.745127UTC VERIFIED,198.856s child; all7checksPASS,
onlyderivedfields changed, noresiduals/noSIGKILL,all5timersrestored. Private/localreceipt:
dedup-reconcile-025cfaf-1852-runner-result.json. This is production timing including DBwrite.
Full unitrentbelgrad-full-refresh-025cfaf-resume1737, journal038a7a8a0f1e499196fa6b8358dd7857,
started18:56:53.392062UTC. Outputacceptance-20260915/full-refresh-025cfaf-resume1737.
Companionrentbelgrad-full-terminal-025cfaf-resume1737; livewatcher localexec42424.
Rootstatushelperfull_refresh_status_025cfaf_resume1737.py with exactjournalID.
All5timerpause/restoration nowownedbyfullsupervisor; monitoractive/NRestarts0.
DO NOT launch any additional otz-user process, app probe/import/restart/deployment while it runs.
Root-only read-only status/file/SQL checks are permitted; no extra source browser profiles/leases.
Await genuine terminal, fresh detailqueue counters, per-source verified/removed/unknown/unattempted,
cleanup, media/manualrights/inbound checks and restoredtimers before accepting fullnightlygoal.
Public19UTC andnight21UTC timers can be overdue at restoration; inspect before any nextmaintenance.
User-requested stop: SIGTERM main supervisor only, then awaitcleanup/receipt/restore. NoSIGKILL.

Progress19:56UTC: all4catalogues terminal,0failedpages/0detailerrors,27newdetailsOK.
Nek4147unique/314pages,4zida4315/402,Halo3266/168,City648/54.
OnlyCitycomplete; other3source_total_changed remain explicitly incomplete.
Rollingrun657c172e0b8a4dfe9f6ad605379eaf47:16364selected/16405inventory,
41retrydeferred,0quotadeferred;2208verified/272errors/0deactivated at19:55:54UTC.
No acceptance until terminal/current-windowaudit/restored5timers.

LocalHalo2-orderaudit COMPLETE: default3345raw/3266unique,oldest3348raw/3269unique,
79duplicateIDs ineach;union3270. Only4extraoldestIDs whiletotalgrew3; no loosenedcoveragegate.
Privatehalo-local-default-1931.json,halo-local-oldest-1931.json,halo-local-comparison-1931.json.
CityIDs11830/42458 have3API-supplied imageURLs returning404 atboth1920x/470x;
corefieldsavailable; completeness remains correctlyunverified. Privatecityexpert-missing-photos-1942.json.

Halo candidateLOCAL: exactsingle301 fromcanonicalrental13digitID (queryempty/kid=4)
toprintcategory?referrer=oglas,HTTP200,validAdslist,exactsourceexpirednotice,
nochallenge/detailpayload => SourceListingRemovedError. Genericredirects/print404remainUNKNOWN.
9livecontrolsPASS (3removed,3healthy,3healthywrongslug302),74targetedtestsPASS.
Fullunit1640passed/1skipped, browser54passed, lint0new; allfinished. Privatehalo-candidate-live-proof-1956.json.
Historicalcandidatece8c9df9bbd6302b9370a7d0b8601c00d3cf28b4 committed/pushed. CI35016688841SUCCESS.
Packetrelease-ce8c9df.tar.gz (446files), SHA893b5325502afb162765ec1c43b9236aad23ad5e68cd96e2be5ce6a03d88fd2d.
CandidateNOTinstalled. Do notactivate/deploywhile025fullruns.
Additionalobservedkid=1/kid=3 variants nowcovered:20livecontrolsPASS,1643unit/1skip,54browserPASS,lint0new.
SourceSHA27e0746fe584c180ca834cbf43565b7199db6a2a6b8277dbb78df4054c101476.
NEWrevisiondb0a7c429f23c56ac45f4e71ac4f97f4ebb3e4f4 pushed, CI35020881966SUCCESS.
Packetrelease-db0a7c4.tar.gz446files SHA0559f1ca83e1b06241202fea3a0ca599f70886c91a7070faf49b024c13ff060b.
Historical candidate only; superseded by8bf56ff. Source025stillunchanged.
Newcanaryrunnerdb0a7c4 preparedonly,35testsPASS, SHA ff9fd9ce89e9e4f1dc12ed142b66daf10d11a20f6b6ef36c77d026eeed0ea8f3.
Healthy20cardCLI SHA unchangedb1bd18905c340d21e8cb652af765368d403d7e5cb7f6e9daf0c4ddff6efb0bb5.
Halo detailphaseendedaround20:34UTC;Nek detailphase nowrunning. HTTP400count0 incurrentlog.
Alllocalv2tests/probecompletedandpolled. Onlywatcher42424running.
Nek no-photo controls1078887/1196683/1241803: sourceChromeexplicitOglasbezfotografije,
DBprice/areaequalUI,0photos/0hashes. Private nekretnine-no-photo-controls-2040.json.
Nek last80requestintervals20:40:09–20:44:40: median3s,mean3.39s,p904s.
This is a short throughput sample, not fullnightprediction; budget tuning maybe needed afteractualterminal.
Do not enable snapshot-active: existing fullshadow gates remain unmet; no bypass/newproxyprobesduringfull.
Progress20:25:28UTC:5011verified/295errors/0skippedsofar/790photos,stillrunning.
NewdailySept16 createdfromtemplate. Localce8canaryrunner prepared+35testsPASS,NOTinstalled:
rentbelgrad-public-canary-runner-ce8c9df.py SHA0618cf11e6bbd9645d5f28d21fd5ebe279fe9bc3858844e7d86c020cb7b87b18;
public_preventive_canary_ce8c9df.py SHAb1bd18905c340d21e8cb652af765368d403d7e5cb7f6e9daf0c4ddff6efb0bb5.
Onlylocalwatcher42424 remainsrunning. Browserresearchtabsclosed; userIPRoyaltabuntouched.

## Historical18:52UTC — 025cfaf installed; canary PASS; dedup recovery RUNNING

Production025cfafaf624d38072b0eb58db8617e12924462c, schema0014.
Activationhealthz/readyz200,13attempts/11.24sec. Newcanaryterminal18:51:09UTC:
20/20verified,283photo hashes,8checksPASS,noresiduals/noSIGKILL.
ReceiptSHAa6bad41dd4a12fb766f0f21699fc1d60c9fc9e89dced5976bdb272846e5ec850;
root public-canary-025cfaf-resume1737,localcopyverified. Publictimerrestored/readbackreceipt.
Standarddedup with newexactindex now supervised by rentbelgrad-dedup-reconcile-025cfaf-1852,
outputdedup-reconcile-025cfaf-1852. It ownsrelease lock/all5timerpause. Need actualterminaland7checks.
NO new otz-user processes while supervisor runs. No diagnostics/imports/probes/restarts.
Root-only bounded read-only status is safe. Next full025requires successfulreconcilecleanup.
All six025canary/full/audit/status helpers installedroot644 and contentverified.

## Historical18:49UTC — df3a88b full CANCELLED by diagnostic collision;025activationrunning

IMPORTANT: at18:44:56UTC the supervisor correctly detected a new unrelated otz-user
Python process from my read-only Telegram getChat diagnostic and stopped the owned full child.
This was an agent orchestration error, NOT a source/proxy failure or a user pause.
NO new otz-user processes of any kind during a controlled supervisor run. Use root-only
read-only status helpers, or wait until terminal. Do not weaken this guard.
Terminal18:46:08UTC full_refresh_failed/full_invariants_require_review,0survivors/noSIGKILL,
all5timersrestored. Publicrights/media/overlays/inbound/identities checks allPASS;
derived duplicate consistency false for8existing groups after interrupted catalogue changes.
Full rolling details never started. Watcher86515finished. Receipt copiedlocalmode600.
Telegram getChat succeeded, exact source peer matches DB; this does not prove latest history.

025activation nowrunning localexec24175; logactivation-025cfaf.log. Beforestart public/night/weekly
servicesnotrunning. Packet previouslypassed1610unit/54browser/fulloracle/CI35007981132.
Afteractivation: install prepared025helpers, freshpubliccanary, supervised standarddedupreconcile
using newexactindex, then full025. Do not claim reconciliation/fullaccepted until actualreceipts.
New helperuploadexec39304 inprogress. Old fullcompanion mayfaildetailaudit because rollingnotreached.

## Historical18:42UTC — df3a88b full RUNNING

Production **df3a88b54dedd07ff16064bbebe6b68dec619319**, schema0014.
Canary passed18:02:13UTC:20/20cards,359photo hashes,8checks,noresiduals.
ReceiptSHA cb9d763652786e406ba558ab66ad1be1ee4117a14588d1cd3397888f4daa465b.
Publictimer restored with receipt before handoff to full supervisor.
Full unit **rentbelgrad-full-refresh-df3a88b-resume1737**, journal
**39b141eaa5e4418ea9ec3115b421fb94**, started18:03:11.847812UTC.
Output `/opt/otz/data/research/acceptance-20260915/full-refresh-df3a88b-resume1737`.
Companion `rentbelgrad-full-terminal-df3a88b-resume1737`. Supervisor owns5timerpause;
DO NOT deploy or run other maintenance while it owns release lock. Monitoractive.
Status helper `/usr/local/lib/rentbelgrad-recovery/full_refresh_status_df3a88b_resume1737.py`
argument exact journalID. Cooperative user-requestedstop only:
`sudo systemctl kill --kill-whom=main --signal=SIGTERM rentbelgrad-full-refresh-df3a88b-resume1737.service`.
Then verify receipt cleanup and all5timer restoration; no SIGKILL.

Primary recovery verified17:58UTC: all7checksPASS, no survivors/SIGKILL,5timersrestored.
Only primaryflags changed68827→71626. Receipt primary-reconcile-1756-runner-result.json private/local.
Release readiness /healthz and /readyz200, monitor/siteactive,NRestarts0.
Candidate **025cfafaf624d38072b0eb58db8617e12924462c** committed/pushed, NOT activated.
Exact dedup equality on16101active rows/3664groups: old1317.793s, candidate34.433s,
all group members identical. Candidate179.90MBpeakRSS vs original90.96MB (local benchmark).
1610unitpassed/1skip,54browserpassed,0newlint. CI35007981132 SUCCESS (unit5m30/browser1m51),
checked18:42UTC through authenticated GitHub UI; private ci-025cfaf.json saved.
Original quadratic oracle preserved; baseline manifest unchanged; AST gate protects remaining module.
Private full-equivalence receipts and input hash are recorded later in this checkpoint.
Packet release-025cfaf.tar.gz SHAe464246ae823a41a4c79da41b255cdf8e93cdbc210a8a6b4d826089ae25e3c22
is staged at /tmp/otz-release.LfvAcg0j, not activated. New025helpers prepared locally,90testsPASS;
not uploaded/installed. Require current full terminal/cleanup/idle before deployment and newcanary.
Watch persistent overdue timers when full restores them; do not race a newly started writer.
Main read-only fullrunwatcher exec86515; production full remainsdf3a88b.

At18:40UTC: Nekretnine catalogue4148/4151,314pages,0failedpages,10new/details10OK;
4zida4324unique,404pages,0failedpages,46new/details46OK, source total changed.
Halo3273unique/168pages,0failedpages,processing newdetails59/59OK,notfinished.
CityExpert and full rolling details have not yet reached current-run checkpoints.
Coverage flags remain degraded honestly; catalogue counts are NOT all-inventory detail verification.
Bot monitor active,NRestarts0. Telegram DBsourcewatermark17575/Sep13;
public t.me preview exposes an older feed, cannot verify current authenticated source freshness.

## Resumed17:37UTC — historical work

User returned with «Продолжай». Bot and5timers live-active before recovery.
Paused-run audit:3628groups before/after, only group68827fails primary ranking.
Nekretnine71626reactivated (13photos), prior primary68827has12photos; full was
cancelled before nightly dedup step. No missing identities/overlays/media/rights.
Standard `dupes-report --write` was supervised by unitrentbelgrad-dedup-reconcile-1737,
outputacceptance-20260915/dedup-reconcile-1737. Release lock and5timer pause owned
until terminalreceipt. It exceeded720s childbudget and cleaned up17:56:04UTC, no survivors/SIGKILL,5timers restored. No success claimed.
New narrower recovery: unitrentbelgrad-primary-reconcile-1756, outputprimary-reconcile-1756.
Private child rechecks exact3members via standard photo grouping and only updates is_primary;
SQLite transaction asserts allotherfields unchanged, primary expected71626. Local valid/changed-scope
fixtures pass. Supervisor has before/after snapshots and7checks; wait for terminalreceipt before deploy.
Slow all-pairs dedup is a remaining nightly-runtime concern, not a reason to relax its semantics.

Candidate **df3a88b54dedd07ff16064bbebe6b68dec619319** committed/pushed.
1600unit/1skip,54browser (57.39s),lint0new,shellPASS; CI35002729471PASS.
Exact candidate sourceSHAe201dbacbef04fe1129c715d5edeb916294b5d2ffd09ba3aada967cd5afe264a
read-only server probe3/3passed17:39:34UTC: page1/page2/priceband sortednewest,
noDB/photo/messagewrites,allHTTP403browserrecovered,nofailedISPslots.
Three catalogue research proofs copied to private localbackup folder.
Releasepacket release-df3a88b.tar.gz SHA59133f845b81c45d9d3ff89a9dc125a75c9099c58b2c4fd168c83d0463ac1e34,
tests-df3a88b.json. Production remains8681742 pending dedup and releaseactivation.
New canary/full/audit helpers cloned revision-pinned df3a88b,90supervisor testsPASS.
Full output planned full-refresh-df3a88b-resume1737; notstarted.

## Latest pause — 2026-09-15 16:57 UTC

User: «Остановись не надолго. Мне надо перейти в другое место». Work paused;
SIGTERM sent only to main supervisor of full-refresh-8681742-resume1516.
Journal eacfb60645004cf0bd9c19d4ca874bc6 cancelled16:57:21UTC (CancelledError).
Terminal receipt16:58:48UTC: no survivors/no SIGKILL, all5timers restored, no restore error.
Result full_refresh_failed / full_invariants_require_review: derived_duplicate_groups_consistent=false;
other5safety checks PASS. Investigate this FIRST on resume, without weakening checks.
Receipt saved privately as full-refresh-8681742-resume1516-runner-result.json.
No further probes or deployment until user returns.

Progress before requested stop: Nekretnine4153previewed,102new/102detailsOK,
314pages,0failedpages; site total changed4158, coverage remains degraded.
4zida4315previewed,401pages/1failed,55new/55detailsOK; interrupted during ingest.
Halo/City and rolling detail queue were not reached. Do not reuse old counters.

Local uncommitted candidate: explicit Nekretnine newest-first default
criterio=data&ordine=desc, preserving explicit configured sort/filters.
Official UI and3read-only server requests confirm default relevance differs
from newest.1600unit passed/1skip; lint0newdiagnostics; not deployed.
Changed files: src/scrapers/nekretnine.py, tests/test_crawl_completeness.py,
tests/test_scrapers_nekretnine.py. Exact candidate probe NOT run yet;
local candidate_nekretnine_newest.py and nekretnine_newest_candidate_probe.py
under /Users/mvstrike/Backups/RentBelgrad/2026-09-15/; verify remote/tmp upload
before resuming, install only recovery helper copy, not app source.
Unit log: unit-newest-sort.log. Proofs nekretnine-sort-probe.json and
halo-catalogue-reverse-count-probe.json still need copy from root acceptance folder.
Next: verify clean terminal receipt and5timer restoration; preserve receipts;
finish candidate proof/release checks, then fresh revision-matched canary/full.
Fresh accepted backup and canary details below remain valid subject to age gate.

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

Release df3a88b staged at `/tmp/otz-release.2mRwVQ0j`; not activated. Pinned uv `/opt/otz/tools/uv-venv/bin/uv` version0.8.22 verified. IPRoyal17:52UTC displays unchanged176.63/195.22GB; no reliable per-pass traffic delta, possible delayed metering. New helper files uploaded/tmp, notinstalledexcept primary recovery.

## Candidate025cfaf — local verified; DO NOT interrupt current full to install

Commit025cfafaf624d38072b0eb58db8617e12924462c pushed.1610unit/1skip,54browser,
lint0new,shellPASS. Full immutable quadratic oracle completed1317.793s vsprototype34.997s,
all16101rows/3664groups EXACTLY equal. Exact committed candidate separately34.433s,
179.9MBpeakRSS, identicalfullgroups. Proofdedup-candidate-full-equivalence.json.
Original singleprocess exec29391 completed. Redundant parallel oracle exec26605 cancelled
cleanly by its parent after full equality; its30smallcase checks passed, no resultclaimed.
Candidate sourceSHA32db436992b53071b2bcc1acd8690c55a42f776aa99a24f9278f66c1cbcfb7d0.
Release/tests privatefiles release-025cfaf.tar.gz,tests-025cfaf.json. CI check pending.

Current full Nex catalogue terminal18:18:20UTC:4148/4151,314pages,10new/detailsOK,
0failedpages/errors/failedslots;324HTTP403allrecovered,328browserattempts,4recoverederrors,
3recycles. Coverage incomplete by3ads remains honestdegraded.
4zida18:30UTC:4324previewed,404pages,0failedpages; firstsite_total4112changed,
ingestunderway. Halo/City/rolling stillpending at this observation.

Read-only3page priceproof18:21UTC:unfiltered4151 vsmin0/min1both4145.
Unpriced exclusion is plausible coverage gap; not individually proven. UIconfirmed price
sortcriterio=prezzo&ordine=asc/desc. Subsequent firstpagespriceasc/pricedesc/oldest all4152,
25numericprices each; no hidden-price ad found in those samples. Bothprivateproofs copiedlocal.
Stored DBprice missing15of4148exactseen rows is NOT currentrawpriceproof (oldfieldspossible).
No extra long sourceprobe while current full is working. No new spending/cancellations.

## Additional observation20:54UTC

IPRoyal authenticated meters now175.25GB/order80643004 ($6.00) and195.21GB/order80647128
($8.10), resetSep25. Previous17:52meters176.63/195.22;delta1.38/0.01GB includesmultiple
controlledcataloguepasses/partialdetails, NOT a single-night cost. No purchase/cancel.
Privateiproyal-traffic-2054.json. HTTPlog midrun audit20:56:20UTC:rollingNek381HTTP403,
4browserchallengereloadannouncements, no rotation/failureannouncements; HTTPstatuses
arenotallbrowserrequests. Halo264301+10print404, detailsandcataloguesshownseparately.
Preparedlocalfull_http_log_audit_025cfaf.py andmidrun-http-audit-025cfaf.json;rootread-only,
noDBwrites. Runatgenuineterminalagainwithnewreceiptname.
Optionaluserquestionpending: desiredmorningfinishBelgrade09:00/07:00/nofixeddeadline.
Noanswerreceived;do nottreatsilenceasapproval. Budgetchangesnotmade.
