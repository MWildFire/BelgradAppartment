# Wave 0 Decisions — Stage W2 «Conversion & Polish»

> Status: **locked** · Date: 2026-06-21 · Branch: `feature/web-conversion-polish`
> Plan: [`../superpowers/plans/2026-06-21-web-conversion-polish.md`](../superpowers/plans/2026-06-21-web-conversion-polish.md).
> Contract delta: [`DATA_CONTRACT-W2.md`](DATA_CONTRACT-W2.md). Answers from Дмитрий, 2026-06-21.

| # | Question (W0) | Decision |
|---|---|---|
| **F1** | Hero headline | **«КЛЮЧИ ОТ БЕЛГРАДА»** (replaces «по-человically»; drop the manual `<br>` line-breaks). Subheading kept: «Помогаем релокантам найти жильё на длинный срок — с договором, который проходит на ВНЖ и белый картон». Casing/transform = current `<h1>`. |
| **F5** | Telegram handle | **`https://t.me/Darya_kralj`** — confirmed. Header + footer + floating sticky on every page. |
| **F4** | Lead e-mail target | **`d.mashukova@bk.ru`** — confirmed. E-mail is a required channel; bot-notice to Дарья stays as the 2nd channel; lead persists even if a channel fails. |
| **F3** | Commission «50%» | **50% of one month's rent, one-time, charged on move-in (lease signing).** Stat shows exactly `50%`; this wording goes into the «Как работаем» breakdown (W3-3). |
| **F2** | District list source | `data/districts.yaml` (full curated human-readable list) ∪ facets (districts with listings + counts). Empty catalog → fall back to the full yaml list. Confirmed. |

## F4 — SMTP / deliverability (user has no relay → "figure it out yourself")

**Decision:** the F4 e-mail code is **relay-agnostic** — `src/site/email.py` uses `aiosmtplib` driven entirely by env (`SITE_SMTP_HOST/PORT/USER/PASSWORD/FROM/STARTTLS`). It works with any SMTP relay, so **Waves 1–4 build and unit-test it with a mocked SMTP** (no real send, no creds needed yet).

**Deliverability plan — CHOSEN (2026-06-21, Дмитрий): free transactional relay (Resend/Brevo).**
1. ✅ **CHOSEN — free transactional relay** (**Resend** 3k/mo free, or **Brevo** 300/day): verify the **`rentbelgrad.com`** domain (the user controls its DNS), add the relay's SPF + DKIM records, send as `SITE_SMTP_FROM=no-reply@rentbelgrad.com`. Best deliverability to `bk.ru` (Mail.ru), branded sender. At deploy (W5-3): user creates the free account + adds ~3 DNS records; the relay's SMTP host/port/user/pass go into `/opt/otz/.env` (`SITE_SMTP_HOST` etc.) by hand; then the live e-mail smoke to `d.mashukova@bk.ru`.
2. **Fallback A — VPS postfix + DKIM** (no third-party account): send-only postfix on the VPS, DKIM key, SPF (`v=spf1 ip4:90.156.254.159 ~all`) + DKIM + DMARC TXT on `rentbelgrad.com` + PTR. DNS-only, but VPS-IP deliverability to Mail.ru is riskier.
3. **Fallback B — Yandex Mail SMTP** (`smtp.yandex.ru:465`, app password, `FROM=<yandex addr>`): zero DNS (yandex.ru already has SPF/DKIM), good RU-to-RU deliverability, but unbranded sender.

Creds go into the **server `/opt/otz/.env` by hand** at W5 (never in code/logs). The **F4 DoD** (a real e-mail lands in `d.mashukova@bk.ru`) is the W5 smoke once a relay is chosen. `SITE_SMTP_FROM` placeholder updates from `rent.example.com` → `rentbelgrad.com`.

## Notes carried forward
- ⚠️ **`origin/main` is stale** (`3270a6a`, pre-Stage-W — Stage W was deployed by rsync, never pushed). The W2 worktree was based on **local** `main` (`9a3afe0`). Optional non-blocking follow-up: push `main` so the GitHub remote reflects production.
- **Overlaps with Stage W** (reconcile, don't duplicate): `apt.html` already has OpenGraph (W3-4 ⇒ add sitemap/robots/404 + verify OG format only); `index.html` already scaffolds a `featured` hero card (W3-2 ⇒ wire it to `is_featured`).
- SMTP creds + the exact commission wording do **not** block Wave 1; only F1 title + F5 handle do, and both are confirmed.
