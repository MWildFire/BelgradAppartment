# RentBelgrad: full-refresh results and remaining repairs, 2026-09-15

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
