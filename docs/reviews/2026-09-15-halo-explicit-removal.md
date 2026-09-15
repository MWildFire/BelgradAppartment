# Halooglasi explicit removal evidence — 2026-09-15

The running full refresh on025cfaf records old Halo cards as UNKNOWN when the print
host redirects to the catalogue. A catalogue redirect alone remains insufficient
removal evidence. This change recognizes the stronger, observed source response.

## Live evidence

Three failed detail URLs from the current run returned one301 to
`/nekretnine/izdavanje-stanova?referrer=oglas`, followed by200. The catalogue contained
a valid Quiddita Ads array and one `div.info-box-expired.info-box-expired-full` with:

> Nažalost, oglas nije pronađen. Oglas je obrisan ili ste pronašli neispravan link.

The same banner was visible on the canonical site in Chrome. Direct canonical HTTP
remained403; this was not treated as removal evidence. Three healthy controls loaded
detail payloads. Changing each healthy URL's slug produced302 to the correct slug,
with a healthy detail payload; none matched removal.

Exact local candidate proof:9/9 expected outcomes, direct requests, no database writes,
photo downloads, messages or proxy use. Private receipt:
`halo-candidate-live-proof-1956.json`, source SHA256
`33d7d62c85316587db60e6c3ae85c187d7436d3fe312372ce0b8ae18b697e8bc`.

## Recognition and preservation

Require canonical HTTPS rental URL with13digitID, no fragment, query empty or kid=4;
one exact301 from its print URL to the exact observed relative destination; final200
on the expected print URL; valid catalogue Ads array; exact complete source notice
inside the expected DOM element; no detail payload or challenge. Other signals remain
UNKNOWN/fallback. Existing print404 handling is unchanged.

Raise the existing SourceListingRemovedError, already handled by detail refresh and
link checking. Preserve prior contents, photographs and successful verification time.
The source can become inactive/DEAD; this does not change manual publication rights,
overlays or automatic-hide configuration. No artificial HTTP404 is created.

Tests include27 ambiguous cases, both allowed queries and source content preservation.
Targeted suite74passed; full unit suite1640passed/1skipped (117.38s), browser54passed
(55.86s), no new lint diagnostics, shell syntax and diff checks passed.
Production remains025cfaf while its full refresh is running. Do not activate this
candidate or start another otz-user process until the supervisor is terminal and has
restored all five timers. Respect possible overdue scheduled jobs at that point.

## Independent catalogue and image findings

Two local paced Halo catalogue passes:default3345raw/3266unique; oldest-first3348raw/
3269unique. Each had79 duplicated IDs. Union3270, only4additional oldest-first IDs while
the reported source total grew3. This does not justify weakening completeness checks
or claim perfectly stable coverage. No recurring second pass was added.

Two CityExpert API cards,11830 and42458, reference three photographs returning404 at
both1920x and470x. Core fields remain available. Do not remove these photo references
or count them as completely verified to conceal the upstream gap.
