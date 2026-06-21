# Stage W2 «Conversion & Polish» — HANDOFF (resume in a new session)

> Date: 2026-06-21 · Project: otz-rent-finder · Branch: **`feature/web-conversion-polish`**
> Status: **BUILD COMPLETE + e2e + broad review (verdict: ship). NOT YET DEPLOYED.**
> Read this first, then `docs/superpowers/plans/2026-06-21-web-conversion-polish.md` (the plan),
> `docs/web/DECISIONS-W2.md` (locked decisions), `docs/web/DATA_CONTRACT-W2.md` (contract delta).

## TL;DR — where we are
All build waves done on `feature/web-conversion-polish` (16 commits since Stage W base `9a3afe0`).
**Suite: 506 passed, 19 skipped. e2e (`RUN_E2E=1`): 18 passed.** Every task was TDD'd (red→green) +
adversarially reviewed + atomically committed. Broad security review = **ship** (no blockers).
**Only Wave 5 deploy + docs + merge remain** — deploy is collaborative (VPS + the e-mail relay).

The e-mail relay is now **set up and verified** (see §Relay). The single remaining action is the
**deploy** (needs the user's `go` + the Resend API key entered on the server by hand).

## Resume setup (worktree + env) — IMPORTANT
- Worktree: `/Users/mvstrike/BelgradAppartment/.claude/worktrees/feature+web-conversion-polish`,
  branch `feature/web-conversion-polish`, based on **local** `main` (`9a3afe0`, has Stage W).
  ⚠️ `EnterWorktree` defaults to `origin/main`, which is **stale** (`3270a6a`, pre-Stage-W — prod
  was rsync-deployed, never pushed). If recreating the worktree, base it on local HEAD and/or
  `git reset --hard 9a3afe0`.
- `.venv` is a symlink to the main checkout's venv. `data/districts.yaml` must be copied in
  (it's required by code/tests but **gitignored & untracked** — the `!data/districts.yaml`
  negation is a no-op under the excluded `data/` dir; copy from the main working dir).
- Run tests: `.venv/bin/python -m pytest tests/ -q` (→ 506 passed, 19 skipped).
  e2e (real chromium+uvicorn): `RUN_E2E=1 .venv/bin/python -m pytest tests/e2e -q` (→ 18 passed).

## What shipped (the 5 mandatory fixes + features)
- **F1** hero → «Ключи от Белграда» (was «по-человечески»).
- **F2** district dropdown: `district_options` global + searchable `combobox.js` over a native
  `<select>` (no-JS fallback) in hero + filters; deep-link/HTMX preserved.
- **F3** commission stat €0 → **50%**, label «комиссия за подбор»; **all** false "free / landlord-pays"
  copy reconciled sitewide (index check #04, apt.html, _lead_form). Precise breakdown only on `/how`.
- **F4** leads e-mailed to **`d.mashukova@bk.ru`** via `src/site/email.py` (aiosmtplib, relay-agnostic,
  **no-op when SITE_SMTP_HOST empty**, 15s timeout, creds env-only/never-logged) + bot-notice;
  lead committed BEFORE sends, each channel non-fatal → lead never lost.
- **F5** «Написать в Telegram» → `https://t.me/Darya_kralj` (`telegram_url` global) in header + footer +
  floating sticky on every page. Leak-guard updated to allow the agent DM.
- **W3-1** per-listing lead: `LeadPayload.site_listing_id`/`kind`; PUBLIC id → kind='listing' +
  apartment summary in e-mail/notice; private/missing id **degrades to general (never leaked)** —
  single chokepoint in `create_lead`.
- **W3-2** real «Квартира недели»: admin CSRF toggle `is_featured`; hero side-card via
  `readmodel.featured_one()` (`week` context), placeholder when None.
- **W3-3** `/vnj` + `/how` content pages (no invented legal facts; `/how` has the 50% breakdown).
- **W3-4** SEO: OG on `/apt` reconciled to «{price}€ · {district}» (absolute site image); `/sitemap.xml`
  (public-only, absolute), `/robots.txt`, custom 404 (`templates/404.html`, renders globals).
- **W4-1** admin leads inbox: `GET /leads` (newest-first, `?status=` filter), CSRF `POST /leads/{id}/status`
  (new→contacted→closed); kind='listing' links the public `/apt/{id}`.
- **Schema (migration 0007 `0007_featured_lead_fields`):** `site_listings.is_featured` + partial index;
  `leads.site_listing_id` FK (ON DELETE SET NULL) + `leads.kind`.
- **Bug e2e caught & fixed:** the hero/filters forms submit empty `district=`/`rooms=`/`price_max=`;
  `_parse_filters` kept `['']` and `_rooms_matches` matched nothing → district search returned 0.
  Fixed (strip blanks at parse + defensive guard) + unit/integration/e2e regression.

## Decisions (locked — docs/web/DECISIONS-W2.md)
Title «Ключи от Белграда» · Telegram `@Darya_kralj` · lead e-mail `d.mashukova@bk.ru` ·
commission «50% от месячной аренды, разово при заселении» · districts from `data/districts.yaml` ∪ facets ·
**e-mail relay = Resend** (free transactional, on `rentbelgrad.com`).

## Relay — DONE (Resend, verified 2026-06-21)
- Resend domain **`rentbelgrad.com` = Verified** (DKIM `resend._domainkey`, SPF on `send`, DMARC),
  provider Spaceship DNS, region **eu-west-1 (Ireland)**. Account: mvstrike17@gmail.com.
- **SMTP settings for `/opt/otz/.env` (enter by hand at deploy — NEVER commit):**
  - `SITE_SMTP_HOST=smtp.resend.com`
  - `SITE_SMTP_PORT=587`
  - `SITE_SMTP_USER=resend`
  - `SITE_SMTP_PASSWORD=`  ← **Resend API key `re_…` (provided by the user; in Resend → API keys).
    NOT stored here for security. Re-obtain from the user / Resend at deploy. Consider rotating it
    since it was pasted in chat.**
  - `SITE_SMTP_FROM=no-reply@rentbelgrad.com`
  - `SITE_SMTP_STARTTLS=true`
  - also: `SITE_TELEGRAM_URL=https://t.me/Darya_kralj`, `SITE_LEAD_EMAIL_TO=d.mashukova@bk.ru`
  (full block in `env.additions-w2.txt`; these are NOT synced by deploy_sync — `.env` is per-environment.)

## Remaining — Wave 5 deploy runbook (W5-3), needs user `go` + each ssh confirmed
VPS: `root@90.156.254.159`, `/opt/otz`, `otz-site.service` (uvicorn `127.0.0.1:8001`), Caddy serves
`rentbelgrad.com` (TLS live from Stage W), `otz-monitor` live. Deploy = **rsync, not git**; `.env` by hand.
1. Local green: `.venv/bin/python -m pytest tests/ -q` + `RUN_E2E=1 … tests/e2e -q`.
2. Backup DB: `ssh root@90.156.254.159 'cp /opt/otz/data/otz.db /opt/otz/data/backups/otz-$(date +%F-%H%M).db'`.
3. `bash scripts/deploy_sync.sh` (rsync; `.venv`/`data`/`.env`/sessions excluded — the `.venv`-symlink
   exclude bug was fixed in Stage W, safe now).
4. Server venv gets the new dep: `ssh … 'sudo -u otz bash -c "cd /opt/otz && .venv/bin/pip install aiosmtplib"'`
   (or `uv sync`). [aiosmtplib added to pyproject.]
5. Edit `/opt/otz/.env` by hand: add the `SITE_*` block from §Relay (incl. the real Resend API key).
6. Migrate: `ssh … 'sudo -u otz bash -c "cd /opt/otz && .venv/bin/alembic upgrade head"'` (→ 0007). [Backup first, step 2.]
7. `systemctl restart otz-site` + `journalctl -u otz-site -n 30` (clean start).
8. **Smoke:** title «Ключи от Белграда»; stat 50%; district dropdown filters; Telegram CTA →
   `t.me/Darya_kralj` (header/footer/sticky) on /, /catalog, /apt, 404; per-listing lead block; «Квартира
   недели» (after an admin toggle); `/vnj` + `/how`; `/sitemap.xml` + `/robots.txt`; OG preview when sharing
   `/apt/{id}` in Telegram; **a REAL lead submission must arrive at `d.mashukova@bk.ru`** (check spam/SPF/DKIM)
   + bot-notice; admin (:8000) still NOT reachable externally; `otz-monitor` `NRestarts=0`.
9. **Docs (W5-4):** update `HANDOFF.md` (§0 status, new env keys, 0007, e-mail channel), `docs/SPEC.md`
   (Stage W2 in stages), `README.md`, and fold `DATA_CONTRACT-W2.md` delta into `docs/web/DATA_CONTRACT.md`.
   (`.env.example` already has the W2 keys.)
10. **Merge (W5-5):** `finishing-a-development-branch` — merge `feature/web-conversion-polish` → local `main`
    (fast-forward), clean up worktree. Optionally push `main` to refresh the stale `origin/main`.

**Rollback:** code = `git revert` + `deploy_sync.sh`; schema = `alembic downgrade -1`; e-mail failure is
non-fatal (lead persists, visible in admin `/leads`).

## Notes / gotchas carried forward
- ⚠️ `origin/main` stale (`3270a6a`); local `main` (`9a3afe0`) is the real prod base.
- `data/districts.yaml`: not git-tracked (gitignore negation no-op); also has a typo alias **«Воџовац»**
  (correct «Вождовац») that surfaces in the district dropdown — fix in the server's `data/districts.yaml`.
- Methodology used: obra/superpowers — TDD, fresh implementer + adversarial review per task, atomic
  commits `stage-w2: <wave>/<task> — …`, full-suite gate per wave.
- Read-only/ToS invariants verified intact: public app writes only `leads`; no площадка/TG-CDN URLs in any
  output; admin localhost-only; CSRF on every new POST.

## Commits (16, since 9a3afe0)
W1-0 config · F1 · F3(×2) · F5 · F2 · F4 · W2-1 · W2-2 · W3-1 · W3-2 · W3-3 · W3-4 · W4-1 · relay-decision · W5-1(e2e+filter-fix).
