# CityExpert retained inactive details — 15 September 2026

## Finding

The PropertyView API keeps HTTP 200, prices and photos after a rental is no longer
available. The old parser saved `status` in raw metadata but ignored it when deciding
whether a response could count as a successful refresh. Consequently, the current
025cfaf run's CityExpert success count is not trustworthy as availability evidence.

A separate direct, read-only Search API walk at 21:46–21:47 UTC returned 647 unique
properties. Of the 882 CityExpert rows active before the controlled full run, 251
were absent from that catalogue. Absence alone is **not** proof of removal. Eight
stratified examples from that set returned retained full detail pages: six `FINISHED`,
one `NOT-AVAILABLE`, and one `KAPARISANA-RENT`. Three catalogue controls returned
`PUBLISHED`. The catalogue changed from 648 to 647 since the production list phase.

Canonical Chrome pages independently confirmed:

| Property | API status | Visible source signal |
|---|---|---|
| 10093 | FINISHED | “Nekretnina više nije u ponudi”; disabled “Nije u ponudi” |
| 67521 | NOT-AVAILABLE | “Nekretnina trenutno nije dostupna za gledanje”; disabled “Nije u ponudi” |
| 55091 | KAPARISANA-RENT | “STATUS-KAPARISANA-RENT”; disabled “Nije u ponudi” |
| 10180 | PUBLISHED | enabled “Zakažite gledanje” |

Reservation and temporary unavailability mean unavailable **now**, not permanent
removal of the stored record. A later genuine listing can reactivate it through the
existing ingestion path. No absence-based sweep or manual publication change is added.

## Candidate behavior

Before accepting detail fields, require an exact canonical HTTPS CityExpert Belgrade
rental URL, matching integer `propId`, integer `cityId=1`, and `rentOrSale=r`.
The three explicitly confirmed unavailable statuses raise `SourceListingInactiveError`.
Only `PUBLISHED` can proceed as available. Unknown states and mismatched identity
fail conservatively; they cannot advance the successful-refresh timestamp.

The shared refresh path records confirmed unavailable rows as inactive while keeping
old price, area, photos, hashes and prior success. Finder's candidate query excludes
inactive rows. No messages or manual website publication changes are made by the probe.

## Verification and remaining work

- 87 focused parser, evidence-preservation and link-probe tests passed.
- 11 live read-only controls passed: 8 unavailable and 3 published with complete fields.
- Full unit suite: 1,717 passed / 1 skipped (126.82 s); browser: 54 passed (51.43 s).
  Changed-code lint, shell syntax and whitespace checks passed.
- Production remains 025cfaf. Do not install the Halo-only 8bf56ff packet as the final fix.
- Wait for the existing full run to terminate and preserve its audit/cleanup receipts.
- Prepare the combined revision, fresh healthy canary, then repeat **both Halo and
  CityExpert** availability checks and standard dedup under the guarded supervisor.

Private evidence: `city-availability-audit-2147.json`, `city-availability-ui-proof.json`,
`city-candidate-live-2150.json` in `/Users/mvstrike/Backups/RentBelgrad/2026-09-15/`.
The earlier `city-off-catalogue-sample-2145.json` explicitly records its rejected method:
`last_seen_at` also changes during detail refresh, so it cannot identify catalogue
membership after that phase. The independent Search walk replaces that method.

The existing CityExpert query includes non-apartment rental types as well. This was
observed in the published controls; property-type scope was not changed by this fix.
