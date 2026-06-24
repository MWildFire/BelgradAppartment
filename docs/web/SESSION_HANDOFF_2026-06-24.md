# Session handoff — Stage S «Showcase & Shield» (2026-06-24)

> Ветка **`feature/showcase-and-shield`** (worktree branch `worktree-feature+showcase-and-shield`),
> от `main` @ **`7ee452f`**. **15 коммитов**, **52 файла, +3611 / −58**. Полный gate зелёный:
> **832 unit + 32 e2e**, alembic head = **0009** (ноль миграций). **НЕ задеплоено** — ждёт твоих рук
> на двух воротах (прод-выкатка + live-Cloudflare). План: `docs/superpowers/plans/2026-06-24-showcase-and-shield.md`.
> Предыдущий: [`SESSION_HANDOFF_2026-06-23.md`](SESSION_HANDOFF_2026-06-23.md) (Stage R).

---

## 0. TL;DR
Запросы Дмитрия закрыты: **#1** уродливая кнопка + неровные карточки → стилизованная CTA-кнопка +
равная высота; **#2** карта на `/apt/{id}` → фуззи-круг + Yandex/Google deep-links; **#3** большое
параллельное мульти-агентное код+дизайн-ревью (10 линз) → триаж + фиксы; **#4** Cloudflare — repo
полностью подготовлен (live-выкатка по раннбуку, совместно). Плюс taste-фичи: PWA+offline+bottom-nav,
динамический OG-image, JSON-LD, полировка `/map`. **Ноль миграций.** EN-локаль отложена (D5).

## 1. Что сделано (по волнам)

| Wave | Коммиты | Суть |
|------|---------|------|
| **0** | `1ce3b34` | `DECISIONS-S.md` (D1=**фуззи-круг ~150м**, D3=CF-доступы есть; D2/D4/D5 дефолты) + `DATA_CONTRACT-S.md` + S0-2 диагностика (origin открыт, admin светит тот же IP, NS=spaceship). Реалии: опечатки «Воџовац» нет; один `site.css` → завели `apt.css`. |
| **1** | `256afbc` `bda018f` `c8b06c0` | CTA → стилизованная oxblood-кнопка прибита к низу; равная высота карточек + line-clamp 2; ноль overflow 320–1920. 14 латинских районов → кириллица; **починен `.gitignore` (`data/*`) → districts.yaml наконец трекается** (фикс CI-папкатки). Исправлены 3 устаревших C1-e2e (rented скрыт по умолчанию). |
| **2** | `5ca87c4` `0d63822` | `src/site/maps.py::maps_links` (фуззи Yandex `ll=LON,LAT z=15` без пина + Google `@LAT,LON,15z`), реюз `yandex_maps_url(precise=)` (ТГ-посты сохраняют пин). Leaflet `L.circle` 150м без маркера + кнопки Yandex/Google + fallback без координат; mobile-гейты. |
| **4** | `047b38d` `111c018` `92fd0a0` | **10-линзовое read-only ревью + состязательная верификация** (`docs/review/stage-s/REVIEW_FINDINGS.md`): 12 находок, 9 подтв./3 опровергнуто. Blocker (карточная CTA раздувалась на ~51% — `.card-l>a{flex:1}` ловил и CTA-якорь) исправлен TDD red→green + визуально. A11y-контраст `.apt-loc-hint`→`--ink-2`. **D1-приватность сделана фактической**: координаты `/apt` снэпаются на ~100м сетку (`fuzz_coords`), точный адрес не светится в HTML. Re-review вердикт: **ship**. |
| **3a** | `87bbf8a` `d5ad841` | CF repo-подготовка: Caddyfile Origin-cert (закомм., live-switch в 3b), `scripts/cf_ufw_allowlist.sh` (origin-lock 80/443 на CF, dry-run+анти-локаут), `deploy/cloudflare/{README,RUNBOOK}.md`. **S3-3: `src/clientip.py::client_ip`** — реальный IP из `CF-Connecting-IP` (gated `TRUST_CLOUDFLARE`) → rightmost XFF → peer; конвергированы rate-limit lead/subscribe/unsubscribe + admin G2-lockout. |
| **5** | `f31e489` `7be913d` `7fdd1cd` `ed4bd6f` | **S5-2** динамический OG-image `/apt/{id}/og.png` (Pillow 1200×630, ETag/304, приватность). **S5-4** `/map`: цвет маркеров по цено-бакету + ВНЖ-тогл. **S5-1** PWA (manifest + SW `/sw.js` root-scope, cache-name на `asset_v`, offline-shell) + mobile bottom-nav. **S5-3** JSON-LD (Organization/WebSite + Apartment/Offer, `|tojson` safe, район-only). |

## 2. Новая поверхность (для деплоя/следующего плана)
- **Новые env:** `TRUST_CLOUDFLARE=0` (S3-3; включать ТОЛЬКО после origin-lock). В `.env.example`.
- **Новые модули:** `src/clientip.py`, `src/site/maps.py`, `src/site/og_image.py`.
- **Новые роуты:** `GET /apt/{id}/og.png`, `GET /sw.js`, `GET /offline`, `/static/manifest.webmanifest`.
- **Новые статики:** `static/manifest.webmanifest`, `static/js/{sw,pwa,apt-map}.js`, `static/css/apt.css`, `static/icons/*.png`.
- **`data/districts.yaml` теперь трекается в git** (раньше gitignored no-op). ⚠️ **deploy_sync.sh исключает `data/`** — см. §3.
- **Pillow** — уже прямая зависимость (`Pillow>=10.4`), доустановки не требует.
- **Ноль миграций** (head = 0009).

## 3. Деплой на прод — ⛔ НЕ СДЕЛАНО (нужны твои руки + `go`)
Порядок (как в Stage R/W, каждый ssh-шаг подтверждается):
1. **Локально зелено** ✓ (832 unit + 32 e2e).
2. **Бэкап БД** на VPS: `sqlite3 .backup` → `data/backups/otz-pre-stageS-$(date +%F-%H%M).db`, `integrity_check=ok`, записать дорелизные счётчики.
3. **Деплой кода:** `scripts/deploy_sync.sh` (rsync; `.venv`/`data`/`.env` исключены). **Миграций нет** (проверить head всё ещё 0009).
   - ⚠️ **`data/districts.yaml`**: deploy исключает `data/`, поэтому новые маппинги районов **НЕ доедут rsync'ом**. Синхронизировать вручную: `scp data/districts.yaml root@90.156.254.159:/opt/otz/data/districts.yaml` (или влить дельту), иначе латинские районы останутся на витрине.
   - Новые статики (icons/manifest/sw/og) приедут с rsync кода (под `src/site/static`).
4. **Рестарт** `otz-site`/`otz-admin` (monitor не трогаем), чистый старт в логах. `asset_v` пересчитается (новые css/js) → SW-кэш сам инвалидируется.
5. **CF-выкатка** — по `deploy/cloudflare/RUNBOOK.md` (Wave 3b, совместно). `TRUST_CLOUDFLARE=1` ставить в серверный `.env` **после** origin-lock (шаг 9 раннбука).
6. **Smoke-матрица:** карточки выровнены + CTA-кнопка не раздувается; `/apt/{id}` фуззи-карта+круг+кнопки Yandex/Google (и fallback без координат), точные координаты НЕ в HTML-исходнике; OG-превью при шеринге в Telegram (`/apt/{id}/og.png`); PWA install + offline; bottom-nav на мобиле; `/map` маркеры по бакетам + ВНЖ-тогл; JSON-LD валиден (Rich Results Test); `/healthz`=200, `/health`=401; глобальная доступность через прокси/мобилу (после CF: 200, CF-IP в `dig`, прямой заход на origin заблокирован); rate-limit/lockout на реальном IP; admin не кэшируется и закрыт; `otz-monitor NRestarts=0`.

## 4. Семена для следующего Feature Plan
- **`/map` показывает точные пины** (до-Stage-S поверхность). D1-фуззи применена только к `/apt`. Если нужна приватность здания и на глобальной карте — снэпать координаты в `map_points`/`/api/map.json` тоже (тогда кластеры/пины станут фуззи). Решение продукта.
- **heal-urls guard + ~8442 до-Next.js трупов** (R-handoff §5, подтверждено L2): `_heal_urls` (src/cli.py:2725) без `is_active=1`/canonical-resolves guard; строки `is_active=0`, видны только в `total`. Не реген, но почистить.
- **A4 дедуп не в nightly** (L4, by design): `dupes-report` ручной; решить авто-wiring vs ручная курация Дарьи.
- **EN-локаль (S5-5, отложено по D5):** только скелет, полный перевод — отдельный этап.
- **CSP `'unsafe-inline'`**: вынести inline `onchange` в filters.js, тогда `script-src` сможет убрать `'unsafe-inline'` (Caddyfile-комментарий).

## 5. Мерж
По плану — `feature/showcase-and-shield` → `main` (ff), push, чистка worktree. Делается после прод-выкатки
(или до — на усмотрение; код самодостаточен и зелёный). См. финальную секцию сессии (finishing-a-development-branch).

---
**Итог:** Stage S закрыт в ветке, отревьюен (10 линз + re-review = ship), полный gate зелёный, ноль
миграций. Осталось человеку: прод-выкатка (§3) + совместная CF-выкатка (RUNBOOK) + `go` на мерж.
