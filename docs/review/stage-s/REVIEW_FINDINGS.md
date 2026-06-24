# Stage S — Wave 4 Review Findings (triaged)

> Method: 10 read-only lenses (L1–L10) over the Stage S branch + adversarial verification of every finding
> (independent skeptic per finding, default-refute). 12 findings → **9 confirmed, 0 uncertain, 3 refuted**.
> Lenses with 0 findings: L3 (concurrency/sqlite), L8 (perf/caching) — clean.

## Blockers (fix in Stage S) — TDD red→green + re-review

| ID | Lens | File | Issue | Fix |
|----|------|------|-------|-----|
| **card-cta-balloon** | L5 a11y + L6 design (dup) | `static/css/site.css:163` | `.card-l>a{flex:1}` matches BOTH the photo/body link AND the `.card-cta` anchor (both direct `<a>` children of `.card-l`). With `.cards{grid-auto-rows:1fr}` the surplus row height splits 50/50 → CTA balloons to ~51% of card as an oxblood slab covering price/title/district. That hidden text is the card link's accessible name → WCAG regression, not just cosmetic. Reproduced with chromium by both lenses. `test_card_grid.py` missed it (only checks bottom-alignment, not height/visibility). | `.card-l>a:not(.card-cta){…flex:1…}` + `.card-cta{flex:0 0 auto}`; add e2e guard: CTA height < card/2 AND price visible. |

## Should (fix — cheap, real)

| ID | Lens | File | Issue | Fix |
|----|------|------|-------|-----|
| **apt-loc-hint-contrast** | L5 a11y | `static/css/apt.css:20` | `.apt-loc-hint` uses `--ink-3` (#8A8275) = **3.32:1** on `--paper`, fails WCAG 1.4.3 AA (needs 4.5:1) at ~13px normal. Project already moved `.kicker` off `--ink-3` for the same reason (site.css:69-71). | `color: var(--ink-2)` (#4A463E, 8.2:1). |

## Nice (fixed now — trivial/cheap)

| ID | Lens | File | Issue | Fix |
|----|------|------|-------|-----|
| **apt-map-gate-dead-css** | L6 | `static/css/apt.css:71` | `.apt-map-gate::before` is dead (0×0 empty `content`) — label comes from JS `textContent`. | Remove the dead `::before` block. |
| **apt-overflow-test** | L7 | `tests/e2e/site/test_apt_map.py` | No 320/1920 horizontal-overflow guard on `/apt/{id}` (the Leaflet block). Safe today (`overflow:hidden` + screenshots) but the plan DoD wants 320–1920 zero-overflow on **all** pages incl map. | Add `scrollWidth ≤ clientWidth+1` assertion at 320 & 1920 for `/apt/1` (map) and `/apt/2` (fallback). |
| **maps-float-coercion** | L1 (refuted as live bug; hardening kept) | `src/site/maps.py:39` | URLs interpolate `lat/lon` without `float()` cast. Unreachable today (ORM Float columns + Jinja autoescape) but cheap defense-in-depth for the URL builder. | `float(lat)`/`float(lon)` in `maps_links`/builders. |
| **card-grid-docstring** | L10 | `tests/e2e/site/test_card_grid.py:9` | Stale docstring "3 public cards" — C1 hides rented → 2 visible by default. | Update docstring to 2 visible. |

## Refuted (verified NOT actionable — recorded for audit trail)

| ID | Lens | Why refuted |
|----|------|-------------|
| maps-no-coercion (as a *bug*) | L1 | `lat/lon` always ORM `Float`/None; `None` short-circuited; Jinja autoescapes the href. No reachable injection. (The cheap hardening is still applied above.) |
| dedup-not-in-nightly | L4 | Design-as-intended: `dupes-report` is manual by spec (FEATURE_PLAN.md:374); the plan itself tasks L4 to flag this as an open *decision*, not a defect. Suggested `settings.nightly_dedup` doesn't exist. → seed. |
| map-e2e-cdn | L10 | The CDN-dependent `wait_for_selector(.leaflet-container)` is the established house pattern (test_map.py) for an opt-in `RUN_E2E` suite; a CDN/SRI failure SHOULD turn the test red (that's the signal). Soft-skip would defeat it. |

## Seeds → next Feature Plan (out of Stage S scope)

- **heal-urls guard + 8442 pre-Next.js dead nekretnine rows** (R-handoff §5): `_heal_urls` (src/cli.py:2725) still lacks the `is_active=1` + canonical-resolves guard; ~8442 `is_active=0` rows inflate `total`. Non-reachable for the public app; carry forward.
- **A4 cross-platform dedup wiring**: `find_cross_platform_dupes` is manual-only (`dupes-report`); decide auto-nightly vs. manual given Darya's curation. (Plan risk row.)

## Clean lenses (no action)
- **L3 concurrency/sqlite**: WAL/busy_timeout/scoped-writer/read-only-engine isolation sound; no new race from Stage S.
- **L8 perf/caching**: asset_v on new apt.css/apt-map.js; Leaflet+tiles load only when `coords` (`{% if coords %}` gating verified); no N+1 added.
- **L9 deps/supply-chain**: `pyproject.toml` untouched (no new Python dep); Leaflet pinned `@1.9.4` with SRI hashes verified byte-for-byte against upstream jsdelivr.
