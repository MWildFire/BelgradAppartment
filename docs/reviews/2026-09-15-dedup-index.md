# Exact indexed duplicate grouping — 15 September 2026

Nightly duplicate grouping compares every active listing pair and their photos.
A bounded production reconciliation exceeded12minutes without finishing; it was
cancelled cleanly, with all5timers restored and no residual processes. A separate
verified three-card reconciliation repaired the one primary mismatch left by the
user-cancelled catalogue pass, without changing any other listing fields.

The candidate splits each64-bit photo hash into h+1 disjoint chunks. Hashes within
Hamming distance h must share a complete chunk. Retrieved candidates still receive
the original XOR/popcount check. Directed matching-photo counts (including repeated
photos), transitive closure, member ordering and primary selection remain unchanged.
Unusual thresholds and out-of-range hashes use the unchanged quadratic implementation.

The wave0 baseline manifest is unchanged. Its original dedup source is preserved
byte-for-byte in tests/fixtures/dedup_wave0_reference.txt (Git blob
1031e8f8dfd733c338dab768631a799fc77207df). The regression guard verifies this blob and
requires the current module AST to equal the original after excluding only the new
index/import, module documentation and the quadratic-function rename. Other matcher,
render, delivery and DB behavior retain their existing checks.

## Validation

-1610unit passed,1skipped;54browser passed; no new lint findings.
-120seeded randomized group comparisons plus bit-boundary, duplicate-photo,
unequal-photo-count, transitive, threshold and invalid-hash cases.
-Exact full group equality on16101active rows /3664groups, using the unchanged
quadratic implementation as independent oracle.
-Local wall time:1317.793s quadratic vs34.997s index (37.65x). Peak RSS90.96MB vs185.02MB.
-This timing is a local benchmark, not a measured production runtime guarantee.
-The redundant parallel oracle was stopped after the original full oracle completed.

Private input/proofs/logs are under /Users/mvstrike/Backups/RentBelgrad/2026-09-15/.
InputSHA25614c13f4b310bddfbbb4c99444162aa3b81b4282f5611105fe86d0224f222fbf2.
Proofdedup-full-equivalence.json, logsunit-dedup-index-v2.log andbrowser-dedup-index.log.
Input contains IDs/photo hashes/counts/coordinate-presence only; no message or URL payload.

Production remainsdf3a88b while full journal39b141eaa5e4418ea9ec3115b421fb94 runs.
Do not activate this candidate while its supervisor owns release lock/timer pause.
A new release needs its own pinned canary and live acceptance after current full terminates.
