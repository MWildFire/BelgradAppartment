# otz-rent-finder — handoff документ

Цель этого документа — дать новому инстансу Claude (или человеку) исчерпывающий контекст для продолжения работы над проектом.

> **Актуальный план следующей реализации (24.08.2026):**
> [`Nekretnine Zero-Regression — list snapshot + ISP proxy без изменений Render Finder Bot`](docs/superpowers/plans/2026-08-24-nekretnine-zero-regression-isp-migration.md).
> Выполнять через обязательный multi-agent DAG: Integration Captain + три постоянно занятых worker-lane
> в изолированных worktree, с последовательными общими barriers. После абсолютных quality/Telegram
> gates — shadow, Chrome quote, отдельное подтверждение оплаты, production canary и rollback-ready activation.

---

## 0.0.1. Продолжение Zero-Regression плана — 25.08.2026

Работа продолжена на чистой integration-ветке `codex/nzr-integration`. Реальный локальный результат:

- `ffcc3a6` + `cd3d11b` + `d8b298a` — добавлена read-only команда `otz nekretnine-shadow-audit` и узкий модуль
  `src/scrapers/shadow_audit.py`. Он не открывает DB-сессию, не запускает orchestrator/stale sweep и не
  отправляет Telegram; сравнивает bounded sample list snapshot против legacy detail, считает coverage,
  skipped pages, fallback/mismatch/detail/proxy errors и пишет sanitized JSON без полных URL/адресов/секретов.
- Proxy pool теперь действительно failover-ит `primary → secondary → metered` после исчерпания
  browser challenge/reload и fail-closed останавливается после исчерпания кандидатов. В report попадают
  только числовые proxy counters и число неудачных слотов; recoverable challenge отделён от residual error.
- Проверка: **1136 passed, 55 skipped, 34 warnings**; новый shadow/proxy/parser/CLI batch — 57 focused tests;
  `pipeline-probe` — `golden-0001`, confidence `1.000`, `phash`; `git diff --check` — чисто.

Это всё ещё implementation progress, не release authorization: `B0 = STOPPED_PENDING_FRESH_INDEPENDENT_QA_PASS`.
До свежего независимого QA PASS не открывать Chrome/checkout/payment, не менять production `.env`, не делать
restart/deploy и не переводить live mode. Следующий технический шаг — независимый QA на exact integrated SHA,
после него B1/B2 review; user-side blocker отсутствует.

---

## 0. ТЕКУЩИЙ СТАТУС + ПЛАН РАЗРАБОТКИ (handoff 2026-06-12, вечер — этап H1)

> Эта секция — рабочий handoff последней сессии. Остальной документ — общий runbook.
> Память Claude по проекту: `~/.claude/projects/-Users-mvstrike-BelgradAppartment/memory/`
> (project-ops, nekretnine-nextjs-migration, site-pagination-caps).

### 0.-7 «Liveness & Resilience» (14–15.07.2026) — 🚀 ЗАДЕПЛОЕНО НА ПРОД · smoke зелёный (4/4)

> **⭐ САМЫЙ СВЕЖИЙ handoff — начни отсюда.** Влито в `main`, задеплоено 15.07.
> План: [`docs/superpowers/plans/2026-07-14-nekretnine-403-retention-fix.md`](docs/superpowers/plans/2026-07-14-nekretnine-403-retention-fix.md).
> Гейт зелёный: **unit 1100 / e2e 52** (раздельно). Alembic head **0011** (одна миграция). Ноль новых
> зависимостей. Вход: два репорта 13.07 — smoke `NEKRETNINE 403` (P0) + retention-нотис «пора убрать (42)» (P1).
>
> **✅ Деплой 15.07:** бэкап `otz-pre-liveness-2026-07-15.db` (1.4G, integrity ok, 1/442/56705/249/146) → rsync
> → `alembic upgrade head` → **0011** (integrity ok, все 146 карточек уцелели после batch-rebuild) → рестарт
> site/admin/monitor (401/200/200). **Smoke после деплоя: 4/4 OK, `NEKRETNINE previews=25 detail_ok=True`
> (recovered via browser fallback), `ExecMainStatus=0`** — поток алертов каждые 6ч прекращён.
>
> **Триаж живости на проде (`site-liveness --dry-run`, 15.07):** карточек 130 → **живы 88**, **подтверждённо
> мертва 1**, не подтверждено 41 (403 с заблокированных площадок → fail-safe держит), снято 0, errors 0.
> Т.е. старый возрастной нотис требовал снять **42** карточки, из которых реально мертва **одна**.
> `SITE_LIVENESS_AUTO_HIDE` осознанно оставлен **0** (warn-first, D1): при 41 inconclusive авто-снятие
> преждевременно; включать после ~недели чистых нотисов.
>
> **Единственная мёртвая карточка снята 15.07** разовым прогоном с `SITE_LIVENESS_AUTO_HIDE=1` (снял ровно 1,
> публичных 130→129; `site_listing id=68`, nekretnine `/oglasi/1219291/`). Проверено **браузером**, что объявление
> реально 404 (curl тут не годится — он 403-ит всё подряд).
>
> **Покрытие обхода nekretnine восстановлено (BACKLOG #23), замер на проде:**
> было `list_recent → 25 previews (skipped_pages=39)`, `new=0` → стало **`125 previews (skipped_pages=0)`,
> `new=4`, `errors=0`** (прогон `scrape nekretnine --max-pages 5`). Smoke после всех правок: **4/4 OK, exit 0**.

Два рычага: **(P0)** хардненинг скрейпера/smoke против 403 и **(P1)** переход с возрастного retention на
**живость** (карточка снимается только когда квартира подтверждённо пропала со ВСЕХ площадок).

- **Wave 1 · Хардненинг (P0).** `fetch_detail` получил ретраи (транзиентные ретраятся, 403/404/410/401 —
  сразу raise). Smoke: один битый detail не роняет зонд + честный `n_previews` (алерт «превью=0» 13.07 был
  враньём except-ветки). Браузерные заголовки в `base._make_client` (Accept/Sec-Fetch/sec-ch-ua/UIR) + `Referer`
  на detail nekretnine. `SCRAPER_PROXY_URL` прокинут в фото-загрузчик и verify-links клиент (`_make_side_client`).
- **Wave 3 · Liveness-ядро (P1).** `src/site/liveness.py::site_liveness_sweep` — C0-матрица по dup-группам A4
  (KEEP пока жив ≥1 площадка / INCONCLUSIVE держит / все DEAD → hide|candidate; pin держит). HEAD с A3-семантикой
  (404/410=DEAD; 403/5xx/таймаут=INCONCLUSIVE=fail-safe). Нотис D8 (топ-10, секции, молчит при пустоте). CLI
  `otz site-liveness [--dry-run]`. Nightly-шаг **вместо** retention-вызова (fault-isolated). Миграция **0011**
  `site_listings.keep_on_site` (pin). A4-тег пишется в nightly (`NIGHTLY_DUPES_WRITE=1`). 21-дневный retention
  (`site-retention-notice`, `src/site/retention.py`) — **удалён**.
- **Wave 4 · Админка.** `/published?window=gone` «🪦 Пропавшие» — от `site_listings` (чинит UX-баг окна «3 недели»);
  bulk-снятие (CSRF+confirm); бейджи живости; тогл «📌 Закреп». «пора убрать» → нейтральный «N дн. на сайте».

**Новые env-ключи (в `.env.example`):** `SITE_LIVENESS_ENABLED=1`, `SITE_LIVENESS_AUTO_HIDE=0` (warn-first, D1),
`SITE_LIVENESS_MAX_HIDES=10` (D3), `NIGHTLY_DUPES_WRITE=1` (D2).

**Диагноз 403 (Wave 2) и его фикс (D6) — 15.07.** Это **DataDome** (не Cloudflare): 403-тело = JS-челлендж
«Please enable JS…», заголовки `x-datadome: protected`, `x-dd-b: 1`, `datadome=`-cookie. Ключит по TLS/JA3 +
JS, поэтому **браузерные заголовки (T1-4) необходимы, но недостаточны**.
**Блок ЧАСТИЧНЫЙ: 403 только на DETAIL — LIST отдаёт httpx 200** (видно в каждом прогоне smoke).
> ⚠️ **Не диагностировать это curl'ом:** curl с VPS получает 403 и на листе — это артефакт curl (его
> TLS-отпечаток хуже httpx). 14.07 из-за этого был сделан неверный вывод «лист тоже заблокирован».
> Источник правды — `/var/log/otz/scrape-smoke.log` (что реально получает httpx).

**Фикс (D6, ноль новых зависимостей):** Playwright+stealth-фоллбэк на 403 — и для **detail**, и для **страниц
списка**. Тот же приём `launch_persistent_context` + `Stealth(...)`, которым halooglasi бьёт Cloudflare; headed
работает, т.к. юниты smoke/nightly уже идут под `xvfb-run -a`. Браузер стартует лениво на первом 403 и
переиспользуется весь прогон; профиль `data/playwright/nekretnine`. Только 403 зовёт браузер: 404 на detail =
объявление исчезло, 404 на списке = конец пагинации. Ключи: `NEKRETNINE_PLAYWRIGHT_FALLBACK=1`, `NEKRETNINE_HEADLESS`.

Замеры с VPS (повторены дважды с интервалом в час):

| | стр.1 | ?pag=2 | ?pag=3 | price-band |
|---|---|---|---|---|
| httpx | 200 | 403 | 403 | 403 |
| httpx + `datadome`-cookie из браузера | 200 | 403 | 403 | 403 |
| **браузер** | **200** | **200** | **200** | **200** |

⚠️ **Отвергнутая гипотеза — не возвращать без воспроизводимого замера:** перенос `datadome`-cookie в httpx.
Один ранний замер дал `200 200 200 200`, механизм был реализован — и **не воспроизвёлся**: cookie уходит в
запрос (проверено логом заголовков) и всё равно отвергается, DataDome валидирует её против TLS/JA3 клиента.
Реализация удалена. Вывод: гипотезы по DataDome проверять минимум дважды, разнесённо по времени.

**Как включить auto-hide на проде (после ~недели чистых нотисов, D1):** `SITE_LIVENESS_AUTO_HIDE=1` в
`/opt/otz/.env` → рестарт. До этого — warn-first: нотис перечисляет подтверждённо-мёртвые кандидаты, снимает
Дарья вручную (bulk в «🪦 Пропавшие» или тогл «На сайт»).

**⏳ Что осталось:**
- 🧑 **Через ~неделю чистых нотисов:** `SITE_LIVENESS_AUTO_HIDE=1` в `/opt/otz/.env` + рестарт (D1). До этого
  liveness-нотис приходит ночью, снятие — вручную (админка → «🪦 Пропавшие» → чекбоксы → «Снять выбранные»).
- **Проверить на первой же ночной сводке (16.07, 03:05 UTC):** `previewed` по nekretnine должен подскочить
  с ~25 к порядку `site_total` (~6000) — полный сегментированный обход теперь идёт через браузер на 403-страницах;
  если ночь станет заметно длиннее — поджать `page_cap`/`price_bands` либо ограничить число браузерных страниц.

---

### 0.-6 Stage T «Insight & Integrity» (02.07.2026) — 🚀 ЗАДЕПЛОЕНО НА ПРОД · smoke зелёный

> **Предыдущий этап (задеплоен на прод 02.07).** Полная сессия: [`docs/web/SESSION_HANDOFF_2026-07-02.md`](docs/web/SESSION_HANDOFF_2026-07-02.md).
> Ветка **`feature/insight-and-integrity`** от `main` @ **`009d229`**. **29 коммитов** (`ce21ea5..5c2b42f`),
> **79 файлов, +8828 / −139**. Гейт зелёный: **юниты 1055 / e2e 52** (гоняются **раздельно** — комбинированный
> прогон невозможен by design, playwright оставляет running-loop). Alembic head **0010** (одна миграция).
> Ноль новых зависимостей. Ревью Wave 5 (10 линз, состязательная верификация): вердикт **SHIP**
> (`docs/review/stage-t/REVIEW_FINDINGS.md`). План: `docs/superpowers/plans/2026-07-02-insight-and-integrity.md`.

Stage T = аналитика + приватность + фото + гигиена данных поверх Stage S. Пять рычагов «летим вслепую /
дыра приватности карты / фото-мегабайты / трупы в БД / большое ревью»:

- **Wave 1 · Privacy & Hygiene.** Фуззи `/api/map.json` (`readmodel.map_points` снэпает `lat/lon` через
  `fuzz_coords` ~100 м **до** сериализации; кластеры по снэпнутым точкам) + постоянный e2e-гард
  `_assert_no_precise_coords`. Гард `_heal_urls` (только `is_active=1` + canonical-resolves; escape-hatch
  `--include-inactive`). `otz purge-inactive --older-than N [--dry-run]` (архив `data/archive/*.jsonl.gz`,
  FK-отказ для строк из `matches`/`site_listings`, TOCTOU-гард; **в этапе — только dry-run**, D5). Честный
  `otz stats` (разбивка active/inactive). `dupes-report` как fault-isolated nightly-шаг (report-only).
- **Wave 2 · Analytics core.** Миграция **0010** (`site_events`, `site_stats_daily`,
  `leads.utm_source/medium/campaign/referrer`). `src/site/analytics.py::record_event` — хуки в 7 GET-роутах
  аллоулиста (`/`, `/catalog`, `/apt/{id}`, `/map`, `/calculator`, `/vnj`, `/how`) через `BackgroundTasks`;
  `visitor = sha256(daily_salt‖ip‖ua)[:16]`, **сырые IP/UA не пишутся никогда**; бот-эвристика по UA;
  HX-Request/не-2xx скип; **fail-closed** на пустой соли (события не пишутся + один warn); kill-switch
  `SITE_ANALYTICS_ENABLED`; **любая ошибка глотается — страница не 5xx-ит** (тест). Rollup/retention в
  nightly (идемпотентный upsert, `_rolled` только финальным дням `< today`, догон `unrolled_days()`,
  retention raw 90д только для помеченных дней). First-touch utm-атрибуция (`static/js/utm.js` + hidden-поля
  форм lead/subscribe + серверный фоллбэк; события `lead_submit`/`subscribe_submit` после коммита).
- **Wave 3 · Admin dashboard.** `GET /admin/analytics?days=7|30` — карточки (views/uniques/leads/conv%),
  inline-SVG спарклайны, топ-квартиры с CTR, источники (utm/referrer); токены Belgrade Editorial, SRI для
  CDN-скриптов, AA-контраст. Колонка «просмотров 7д» в admin-списке публичных листингов (T3-2).
- **Wave 4 · Photo pipeline.** `src/site/photos.py` — webp `card` (640w) / `detail` (1280w) q82,
  `photo_manifest.json` в **РОДИТЕЛЕ** `photos_root` (вне публичного `/photos`, скрыт Caddy `hide`),
  EXIF-transpose + RGBA/LA/P/CMYK, кросс-процессный `flock`, инкрементальный флаш каждые 100. `otz
  photos-backfill` (`--dry-run`, `--limit`, идемпотентен), publish-hook. Jinja `photo_v`/`photo_dims` +
  `<picture>`/srcset (CLS→0, фоллбэк на оригинал без манифеста); `img-fallback.js` (вынос onerror).
  Caddyfile: immutable `/photos/*` + hide манифеста + готовность снять `unsafe-inline` из `script-src`.
- **Wave 5 · Большое ревью.** 10 линз read-only по всему репо → 54 находки → 40 подтверждено / 5 опровергнуто
  (0 blocker, 14 major) → TDD-фиксы (6 fix-коммитов) → **SHIP**. 8 minor докс-дрейфа синхронизированы в Wave 6,
  3 minor — в бэклог.

**Новая поверхность (для деплоя):**
- **Миграция 0010** (`20260702_0010_analytics.py`) — единственная новая; downgrade полноценный.
- **Новые модули:** `src/site/analytics.py`, `src/site/photos.py`, `src/admin/analytics.py`.
- **Новый роут:** `GET /admin/analytics` (админ-app; карточки/спарклайны/топ/источники).
- **Новые статики:** `static/js/{utm,img-fallback,subscribe-form}.js`; `src/admin/templates/analytics.html`.
- **Новые env-ключи:** `SITE_ANALYTICS_ENABLED=true`, `SITE_ANALYTICS_SALT=` (⚠️ пустая при enabled → fail-closed
  no-op; генерить на сервере `openssl rand -hex 32`, только `/opt/otz/.env`), `SITE_ANALYTICS_RAW_RETENTION_DAYS=90`
  (см. `docs/web/env.additions-t.txt`).
- **Derived data на VPS:** webp-варианты + `photo_manifest.json` в git/rsync НЕ участвуют (`data/` исключена) —
  раннбук Wave 6 гоняет `otz photos-backfill` **на сервере** (в фоне/tmux), манифест на проде =
  `/opt/otz/data/photo_manifest.json`.

**✅ Деплой выполнен 02.07.2026 (Wave 6, раннбук плана §2):** бэкап `otz-pre-stageT-2026-07-02-1240.db`
(1.4G, integrity ok, счётчики 4/0/23136/58); rsync кода; env-блок + `SITE_ANALYTICS_SALT` (64 hex, на сервере);
`alembic upgrade head` → **0010** (integrity ok); `otz photos-backfill` на сервере → **2535 оригиналов →
5070 webp-вариантов, 0 битых**, `photo_manifest.json` (2535 записей) в `/opt/otz/data/` (**вне** публичного
`/photos`, отдача манифеста = 404); рестарт `otz-site`/`otz-admin`/`otz-monitor` + Caddy reload
(`/etc/caddy/Caddyfile` ← `deploy/Caddyfile`, immutable `/photos/*`). Nightly-шаги — в `_run_nightly_steps`
(отдельный timer не нужен, T0-2.4).

**Smoke-матрица (02.07, зелёная):**
- **Приватность:** `/api/map.json` — 53 точки, **все на 3-decimal сетке, 0 утечек** (Wave 0: `44.816884149726`
  → теперь `44.817`). Постоянный e2e-гард `_assert_no_precise_coords` в suite.
- **Аналитика:** pageview'ы пишутся (`utm_source=smoke`, `is_bot=0`, `/apt/1`→`path=/apt`), в схеме `site_events`
  **только хэш `visitor`** (ни IP, ни UA); страницы 200, не замедлились.
- **Дашборд:** `admin.rentbelgrad.com/analytics?days=7|30` → 200 (карточки/спарклайны/топ/источники), снаружи 401.
  ⚠️ Публичный путь — **`/analytics`** (не `/admin/analytics` — в admin-app префикса `/admin` нет, как и у `/metrics`).
- **Фото:** карточки → `.card.webp`+srcset, `/apt` → `.detail.webp` eager+fetchpriority (LCP), вариант ~38 KB
  (`image/webp`, `Cache-Control: max-age=31536000, immutable`), `width/height` везде.
- **Lighthouse mobile (дельта к baseline T0-2):** `/apt/1` **perf 0.63→0.81, LCP 16.0s→3.7s** (4.3×), CLS 0;
  `/` perf 0.94, LCP 2.7s, **CLS 0.026→0**.
- `otz stats` — active/inactive разбивка; `otz purge-inactive --older-than 90 --dry-run` — **0 кандидатов**
  (inactive-строки имеют свежий `last_seen_at`; для боевого purge Дмитрий подберёт меньший порог), 0 мутаций.
- `/healthz`=200, admin за basic-auth (401), `otz-monitor NRestarts=0`, все сервисы active.

**⚠️ Что ещё НЕ сделано (совместные шаги Дмитрия):**
- **Боевой `purge-inactive`** — только после чтения dry-run отчёта (**26 400** inactive; при пороге 90д — 0
  кандидатов, нужен меньший порог); в этапе — только dry-run (D5), бэклог `docs/BACKLOG.md` #3.
- **Снятие `'unsafe-inline'` из `script-src`** — код готов (все inline-обработчики вынесены, e2e-гард держит 0),
  но директива CSP в задеплоенном Caddyfile пока сохраняет `unsafe-inline` (безопасный дефолт); бэклог #5.
- **Cloudflare Wave 3b** остаётся в бэклоге #1 (в этап T не входит, решение 02.07).

---

### 0.-5 Stage S «Showcase & Shield» (24.06.2026) — 🚀 КОД В ПРОДЕ · ⏳ CF-выкатка ждёт

> Полная сессия: [`docs/web/SESSION_HANDOFF_2026-06-24.md`](docs/web/SESSION_HANDOFF_2026-06-24.md).
> `main` @ **`8626e1a`** (Stage R + Stage S смёржены, ff), в синхроне с `origin`. Прод @ alembic **`0009`**.
> **Проверено вживую 2026-07-01** (стабильно ~неделю): `otz-monitor NRestarts=0`, ночной прогон чистый,
> **cityexpert live — 1231 активных**, site 58 публичных (X1 автопубликует), listings 43k→49.5k.

Stage S = UX/имидж-полировка витрины (Дмитриевские запросы) поверх Stage R: стилизованная CTA + равные
карточки, фуззи-карта на `/apt` + Yandex/Google deep-links, **PWA+offline+bottom-nav**, динамический
**OG-image** `/apt/{id}/og.png`, **JSON-LD** (Organization/Apartment/Offer), полировка `/map`,
приватность координат (`fuzz_coords` ~100м), 10-линзовое ревью → ship. **Ноль миграций** (head 0009).

**⚠️ Что ещё НЕ сделано (для новой сессии):**
- **Cloudflare Wave 3b (live-switch) — НЕ выполнено.** Repo подготовлен (`deploy/cloudflare/RUNBOOK.md`,
  `scripts/cf_ufw_allowlist.sh`), но на проде `TRUST_CLOUDFLARE` НЕ включён (origin ещё открыт). Совместная выкатка по раннбуку.
- **`data/districts.yaml`** — ✅ на проде (mtime 24.06 01:54; `deploy_sync.sh` его явно `--include`'ит, не надо scp вручную).
  Сырой `listings.district` остаётся латиницей (21395 строк) — это норма; витрина нормализует через `human_district()`.
- **A4 дедуп не гоняли** — `dup_group_id` у 0 строк (в nightly не подключён; `dupes-report --write` вручную не запускали).
- **heal-urls guard + ~8442 до-Next.js трупов** (R §5 / S §4) — `_heal_urls` без `is_active`/canonical-resolves guard; строки `is_active=0`, только засоряют `total`.
- **Telegram-демон подписок** выключен (нужен `SITE_TELEGRAM_BOT_USERNAME`); подписок пока 0. EN-локаль (D5) отложена. CSP `unsafe-inline` (S §4).
- ⚠️ **scrape-smoke падал 1 раз** (Jun 25, OnFailure-алерт), с тех пор `success` — вероятно транзиент (halooglasi/Playwright); если повторится — глянуть `journalctl -u otz-scrape-smoke`.

---

### 0.-4 Stage R «Resilience & Reach» (23.06.2026) — 🚀 ЗАДЕПЛОЕНО НА ПРОД (24.06)

> Полная сессия: [`docs/web/SESSION_HANDOFF_2026-06-23.md`](docs/web/SESSION_HANDOFF_2026-06-23.md).
> Stage R = tip `20bd9a8` (20 коммитов `c55a787..20bd9a8`, 741 тест). Смёржен в `main`; поверх него лёг Stage S (§0.-5).
> **Прод @ alembic `0009`**, бэкап `otz-pre-stageR-20260624-021356.db` снят.

20 фич за этап (надёжность скрейпинга + авто-синхронизация витрины сайта + продукт/охват), 2 круга
состязательного ревью. **Хайлайты:** A1 фикс nekretnine-404 + самолечение URL · A2 live-smoke +
fingerprint-drift алерты · A3 проверка живости ссылок · A4 межплощадочный дедуп (миграция 0008) ·
**X1 автопубликация на сайт при постинге в Telegram** · **X2 автоскрытие при деактивации на площадке** ·
C2 подписки на поиск (e-mail + Telegram, миграция 0009) · E1 новый скрапер cityexpert.rs ·
B1/B2/B3/F1/G1/G2 наблюдаемость/аналитика/безопасность. WhatsApp/Viber убраны (только Telegram);
sasomange.rs выброшен (редиректит на новостной раздел).

🚀 **Задеплоено 24.06:** backup → rsync → миграции 0008+0009 → рестарт site/admin/monitor → scrape-smoke
таймер. C1 на проде подтверждён (rented скрыты по умолчанию). `heal-urls` **намеренно НЕ запускали** —
8442 кандидата оказались до-миграционными трупами (canonical тоже 404); см. §4-7 / §5 свежего handoff.
Опционально не делалось: дедуп A4 в nightly, Telegram-демон подписок (выключен). Env-флаги/CLI — в §2.

---

### 0.-3 Post-launch + admin/curation (21.06.2026) — 🚀 ВСЁ В ПРОДЕ

> Полная сессия: [`docs/web/SESSION_HANDOFF_2026-06-21.md`](docs/web/SESSION_HANDOFF_2026-06-21.md).
> `main` @ `dd1f534`, запушено в `origin`.

После запуска Stage W2 (см. §0.-2) — серия фиксов и недостающая инфраструктура админки/курирования:
- **Фиксы сайта:** район-combobox (был обрезан `overflow:hidden`), карта рендерит пустой Белград, нав-доводка,
  **fingerprint `/static?v=`** (иначе immutable-кэш Caddy не пускал деплои в браузеры), адаптив 320–1920px без
  горизонтального оверфлоу, и фикс карточки **«Квартира недели»** (`<a>` collapse → `.hero-photo{display:block}`).
- **Админка задеплоена** (`otz-admin.service` + `admin.rentbelgrad.com`, Caddy/HTTPS + Basic-auth, fail-closed;
  DNS A добавлена, cert выпущен). Это была причина «Дарья опубликовала — на сайте пусто»: админка (единственный
  путь публикации на сайт) раньше не была развёрнута. `SITE_CACHE_TTL` 300→**60с**.
- **Курирование на `/published`** (`/queue` пуст by design — delivery сразу метит PUBLISHED): кнопки «На сайт»/
  «Квартира недели»/флаги/заметка + бейджи «на сайте»/«снято»/возраст + фильтр неделя/3 недели/все.
  «Квартира недели» **эксклюзивна** (одна за раз). **Ретеншн 3 недели, warn-first** (без авто-удаления):
  бейдж «пора убрать» ≥21д + ночной Telegram-нотис Дарье+оператору (`src/site/retention.py`,
  hook в `nightly-refresh`; вручную: `cli site-retention-notice`).
- **Состояние прода:** `otz-monitor`/`otz-site`/`otz-admin` active, alembic 0007, **45 публичных листингов**
  (Дарья активно публикует). Лид-почта через **Resend**.
- ⚠️ **Ротировать:** Resend-ключ (`SITE_SMTP_PASSWORD`) и `ADMIN_PASSWORD` — оба светились в чате; живут только в
  `/opt/otz/.env`. Открытое: featured-карточка может быть «снято» (оба ранних листинга rented) — курирование Дарьи.

### 0.-2 Этап W2 «Conversion & Polish» (21.06.2026) — 🚀 ЗАПУЩЕН на https://rentbelgrad.com

> Самый свежий handoff. План: `docs/superpowers/plans/2026-06-21-web-conversion-polish.md`;
> решения: `docs/web/DECISIONS-W2.md`; дельта контракта: `docs/web/DATA_CONTRACT-W2.md` (влита в `docs/web/DATA_CONTRACT.md` §8).

**Что это.** Конверсионная доводка Stage W: 5 обязательных фиксов + фичи. **Задеплоено 21.06** (rsync + миграция 0007 + e-mail-релей Resend).

**Что сделано (ветка `feature/web-conversion-polish`, влита в `main`):**
- **F1** hero-заголовок → «Ключи от Белграда» (был «по-человечески»).
- **F2** дропдаун районов (combobox над нативным `<select>`, no-JS fallback) в hero и фильтрах; источник — `data/districts.yaml` ∪ фасеты. Убран джанк-алиас «Воџовац» из списка.
- **F3** стат комиссии €0 → **50%** («50% от месячной аренды, разово при заселении»); ложное «бесплатно / платит арендодатель» вычищено; разбор на `/how`.
- **F4** заявки уходят на **`d.mashukova@bk.ru`** через `src/site/email.py` (aiosmtplib, релей-агностик, no-op при пустом `SITE_SMTP_HOST`) + бот-нотис; lead коммитится ДО отправок, каждый канал не критичен → заявка не теряется.
- **F5** «Написать в Telegram» → `https://t.me/Darya_kralj` (шапка/футер/липкая) на каждой странице.
- **Фичи:** per-listing заявка (`leads.kind`/`site_listing_id`), реальная «Квартира недели» (admin `is_featured`), `/vnj` + `/how`, SEO (OG, `/sitemap.xml`, `/robots.txt`, кастомный 404), admin leads inbox (`/admin/leads`).
- **Миграция 0007** (`0007_featured_lead_fields`, head): `site_listings.is_featured` + частичный индекс; `leads.site_listing_id` FK (ON DELETE SET NULL) + `leads.kind`.
- **E-mail-релей = Resend** — домен `rentbelgrad.com` верифицирован (DKIM/SPF/DMARC, аккаунт mvstrike17@gmail.com). `SITE_SMTP_HOST=smtp.resend.com`, порт 587 STARTTLS, `SITE_SMTP_USER=resend`, `FROM=no-reply@rentbelgrad.com`. API-ключ `re_…` — в `/opt/otz/.env` руками, **не в коде/гите** (рассмотреть ротацию — был вставлен в чат).
- **Новые env** (в `.env.example`; на сервере руками): `SITE_TELEGRAM_URL`, `SITE_LEAD_EMAIL_TO`, `SITE_SMTP_HOST/PORT/USER/PASSWORD/FROM/STARTTLS`.
- **Тесты:** 506 passed, 19 skipped; e2e (`RUN_E2E=1`) 18 passed. Деплой-smoke на проде зелёный: заголовок/50%/Telegram-CTA/`/vnj`/`/how`/`/sitemap.xml`/`/robots.txt`/кастомный 404; боевая заявка (`POST /lead` → 303, lead id=1 `kind='general'`) — e-mail ушёл без ошибок (SMTP-путь отдельно проверен с VPS → `VPS_SMTP_OK`).
- **Бэкап перед миграцией:** `data/backups/otz-2026-06-21-1245-pre-stagew2.db`.
- ⚠️ **Открыто:** на проде `site_listings` пусто (0 публичных листингов) → per-listing-заявку / OG на `/apt/{id}` / «Квартиру недели» нельзя досмокать, пока Дарья не опубликует листинг (покрыто e2e-тестами). Глазами подтвердить приход письма в инбоксе `d.mashukova@bk.ru` (проверить «Спам»).

### 0.-1 Этап W «Public Web Catalog» (20.06.2026) — 🚀 ЗАПУЩЕН на https://rentbelgrad.com

> Самый свежий handoff. ADR + план: `docs/web/ARCHITECTURE.md`, `docs/web/DATA_CONTRACT.md`,
> `docs/web/DECISIONS.md`, `docs/superpowers/plans/2026-06-20-public-web-catalog.md`.

**Что это.** Stage W — **второе FastAPI+Jinja-приложение на той же VPS** (публичный каталог для
клиентов @RentBelgrad), читает **существующую SQLite read-only** (OS-уровень `mode=ro`), под **Caddy**
(авто-TLS). Код в `src/site/`. **Не** Astro/Vercel и **не** повторный ингест из Telegram — обоснование в
ADR. Каталог показывает только **отобранные Дарьей** квартиры (опубличенные в @RentBelgrad), не сырые
21.8k listings и **никогда** не линкует на площадки-конкуренты (ToS).

**Что сделано (код, ветка `feature/public-web-catalog`):**
1. **Новый сервис** `otz-site.service` (`uvicorn src.site.app:app`) на `127.0.0.1:8001`, `User=otz`,
   `--workers 1` (CSRF-токен / rate-limiter / TTL-кэши per-process), за Caddy. `OnFailure=` — один алерт,
   как у H1-юнитов. Юнит: `deploy/otz-site.service`.
2. **Миграция 0006** (`0006_site_listings_leads`, alembic head теперь 0006): таблица `site_listings`
   (курирующий оверлей: `match_id UNIQUE FK`, `is_public`, `is_for_vnj`, `is_for_white_card`,
   `channel_post_url`, `note`) + таблица `leads` (заявки с публичной формы).
3. **Admin** (`src/admin`) получил publish-to-site контролы: тоглы «На сайт» / «ВНЖ» / «Белый картон»
   + `channel_post_url`/`note`; Mark-posted авто-создаёт публичный `site_listing` (авто-парсинг
   ВНЖ/белый-картон флагов из текста поста, ручной тогл всегда перебивает). CSRF-guarded.
4. **Публичный сайт:** главная · каталог с фильтрами (ВНЖ + белый картон на видном месте,
   район/цена/комнаты/сортировка) · карточка `/apt/{id}` (цена+площадь моноширинным, «снято» приглушённо,
   CTA на канал) · карта (Leaflet) · калькулятор аренды · форма заявки (→ `leads` + нотис Дарье через
   существующий бот; honeypot-поле `company` + per-IP rate-limit + CSRF) · избранное (`localStorage`).
5. **Фото** отдаёт Caddy `file_server` из `/opt/otz/data/inbound` (`SITE_PHOTOS_ROOT`), **не** Python;
   публичные URL — `/photos/<relpath>`; **никогда** площадочный или TG-CDN URL в публичной выдаче (ToS).
   `InboundPost.photo_paths` хранятся относительными (`data/inbound/...`).
6. **Новые env-ключи** (см. `deploy/env.additions.txt` / `.env.example`): `SITE_BIND_HOST/PORT`,
   `SITE_PUBLIC_BASE_URL`, `SITE_CACHE_TTL`, `SITE_PAGE_SIZE`, `SITE_PHOTOS_ROOT` (=/opt/otz/data/inbound),
   `SITE_CHANNEL_URL`, `SITE_LEAD_RATELIMIT_MAX/_WINDOW`. Нотис заявки переиспользует
   `TG_BOT_TOKEN`/`TG_BOT_RECIPIENTS`.
7. **Тесты:** полный прогон 438 passed + 6 skipped; Playwright e2e (`RUN_E2E=1`) — 5 флоу против реального
   uvicorn+chromium. Read-only-изоляция, no-scraping-leak, CSRF, honeypot покрыты тестами + 3-линзовое
   security-ревью (вердикт fix_then_ship, все находки исправлены).

**Деплой — статус: ЗАПУЩЕН (публичный запуск 20.06.2026):**
- ✅ Код зарсинкан, миграция 0006 применена (бэкап: `data/backups/otz-2026-06-20-1813-pre-stagew.db`),
  `otz-site.service` enabled + active на `127.0.0.1:8001`.
- ✅ **Домен `rentbelgrad.com` (+ `www`)** → A-записи на `90.156.254.159`. **Caddy v2.11.4** установлен;
  `/etc/caddy/Caddyfile` = `deploy/Caddyfile`; **TLS Let's Encrypt** выпущен для apex + www; `www→apex`
  (301); `http→https` (308); **ufw 80/443 открыты**. `SITE_*` (вкл. `SITE_PUBLIC_BASE_URL=
  https://rentbelgrad.com`) добавлены в `/opt/otz/.env`.
- ✅ **Прод-smoke зелёный (external HTTPS):** `/healthz` ok, `/ /catalog /map /calculator /api/map.json
  /static/*` → 200, фильтр ВНЖ работает, HSTS/CSP/X-Frame на месте, `/photos/../..` → 404 (Caddy jail),
  `POST /lead` без CSRF → 403. **Админка наружу недоступна** (`/queue` → 404, :8000 не проксируется).
  Монитор/otz-site/caddy active, `otz-monitor NRestarts=0`.
- ⚠️ **Инцидент при деплое (устранён):** первый `deploy_sync` затёр `/opt/otz/.venv` (worktree-симлинк
  `.venv` + `--delete-after`). БД не пострадала (исключена + бэкап); монитор не падал. Venv пересобран по
  локальному dev-freeze (вкл. CPU torch + open_clip), chromium-shell переустановлен. Перманентный фикс в
  `scripts/deploy_sync.sh` (исключить `.venv` без слэша + `logs/`/`.claude/`). **Урок: не rsync'ить
  worktree с `.venv`-симлинком без точного исключения.**

**Follow-up (не блокирует):** access-log Caddy в файл нужен systemd `ReadWritePaths` drop-in для юнита
caddy (пока journald — `journalctl -u caddy`); CSP `script-src 'unsafe-inline'` можно ужесточить, вынеся
один inline `onchange` в `filters.js`.

**Порты:** пайплайн без изменений; otz-site `127.0.0.1:8001`; **Caddy 80/443 (ufw открыты)**. Outline
(50029, 28395) не задет. Админка остаётся localhost-only, наружу не проксируется никогда.

### 0.-1.1 Инцидент 20.06.2026: первый `deploy_sync` снёс `/opt/otz/.venv`

**Что случилось.** Первый прогон `deploy_sync` при деплое Stage W **затёр `/opt/otz/.venv`**: в worktree
`.venv` — это **симлинк**, а `deploy_sync` исключал `'.venv/'` (слэш в конце матчит только директорию, не
симлинк), поэтому `rsync --delete-after` снёс venv на сервере. Потери БД/данных нет (`data/` исключён +
бэкап есть); `otz-monitor` продолжал крутиться на уже загруженном в память интерпретаторе.

**Восстановление.** Пересобран `/opt/otz/.venv`, запинен к локальному dev-freeze (вкл. CPU-torch +
open_clip), переустановлен совпадающий chromium headless-shell, восстановлен ownership; монитор
проверен — живой.

**Фикс (закоммичен в `scripts/deploy_sync.sh`):** исключать `'.venv'` (БЕЗ слэша — матчит симлинк) +
исключать `logs/`, `.claude/`, `.DS_Store`, `tests/e2e/site/_artifacts/`.

**УРОК:** никогда не рсинкать worktree, чей `.venv` — симлинк, не исключив его по точному имени.

### 0.0 Инцидент 13–19.06.2026: отзыв Telethon-сессии + hardening

**Что случилось.** 13.06 ~17:35 UTC владелец аккаунта (это личный аккаунт `mvstrike`, он же userbot)
случайно «завершил все сеансы» в Telegram → обе userbot-сессии отозваны (`SessionRevokedError`).
Монитор упал; при каждом рестарте без валидной сессии Telethon `start()` просил телефон в
не-интерактивном сервисе → `EOFError`/`Aborted!` → exit 1 → крэш-луп 6× → `failed`. Лежал ~3 дня,
новые посты Otz не обрабатывались. `OnFailure` сработал на каждый из 6 рестартов → спам. Бот-сводки
продолжали идти (бот на токене, сессия ни при чём).

**Восстановление.** Вход по коду был невозможен: при живой app-сессии Telegram отдаёт
`SentCodeTypeApp`/`next_type=None` и придерживает in-app код для стороннего клиента (ни SMS, ни звонка).
Помог **QR-вход** (`scripts/auth_qr.py`) — живая сессия на телефоне поручается за новую. Реавторизованы
обе сессии (monitor + logger через `OTZ_SESSION=otz_logger`). См. процедуру в §5.4.

**Фиксы (эта сессия, ветка main):**
- Монитор больше не зовёт интерактивный `start()` в сервисе: `connect_authorized()` проверяет
  `is_user_authorized()`; при отзыве/отсутствии сессии — **один** алерт через бот (`send_text_via_bot`,
  не зависит от delivery-mode) + `exit 3`. Рантайм-отзыв (`UnauthorizedError`/`AuthKeyDuplicatedError`
  из `run_until_disconnected`) ловится так же.
- systemd: `RestartPreventExitStatus=3` — потеря сессии = **один** `failed`/алерт, без крэш-лупа
  (транзиентный exit 1 по-прежнему рестартует). Нотификатор де-дуплицирует алерты юнита в окне ~10 мин.
- Исправлена ложная модель «OnFailure только при исчерпании рестартов» в `vps_install.sh`/PHASE5/HANDOFF.
- `scripts/auth_qr.py` заведён в репо (QR-реавторизация: ASCII+PNG QR, 2FA через `OTZ_2FA`, `OTZ_SESSION`,
  коды возврата); `qrcode` добавлен в `pyproject`. §5.4 переписан: QR — основной способ.
- Ночной refetch: общий wall-clock бюджет (`NIGHTLY_REFETCH_BUDGET_SEC`) на heal+rolling + per-fetch
  cap (`asyncio.wait_for`) — страховка от зависания на challenge-страницах halooglasi (инцидент SIGTERM 16.06,
  не воспроизводился).

### 0.1 Что сделано: этап H1 «Hardening & Intelligence» (12.06.2026, ветка `stage-h1` → main)

План этапа: `FEATURE_PLAN.md` (волны W0-W5). Исполнено мультиагентно, каждая волна — spec+quality ревью, gate `pytest -q`. Тестов 105 → **207**. Деплой на VPS + миграция 0005 — см. 0.2.

1. **Миграция 0005** (`alembic/versions/20260612_0005_indexes_dedup_metrics.py`): индексы `ix_listings_last_seen_at`, `ix_listings_platform_active` (избыточный `ix_listing_platform` удалён); partial unique `uq_inbound_chan_msg_nodigest` (закрывает гонку дедупа inbound при NULL digest_share_id; перед созданием — дедуп существующих дублей с каскадом и логом); `listings.details_refreshed_at`; таблица `daily_metrics`.
2. **Монитор** (`src/telegram/monitor.py`): persist обёрнут в `try/except IntegrityError` — проигравший гонку дедупа не запускает pipeline (нет дубль-доставки Дарье). Лог-маркер: «already persisted concurrently».
3. **Ночные шаги** (`src/cli.py::_run_nightly_steps` + `src/scrapers/orchestrator.py`), все env-отключаемы (`=0`), падение шага → 🚨-строка в сводке, nightly выживает:
   - auto-heal битых строк (`NIGHTLY_HEAL_LIMIT=500`, с ротацией мёртвых через `details_refreshed_at`);
   - rolling re-fetch деталей/цен (`NIGHTLY_DETAIL_REFRESH=200`, NULLs first, поровну по платформам) → контракт `PriceChange`;
   - прунинг `photo_embeddings` неактивных >30 дней (`NIGHTLY_PRUNE_EMBEDDINGS=1`; файл БД не сожмётся без ручного VACUUM — см. §15.7);
   - snapshot `daily_metrics` + 🚨-алерт при падении active >20% против прошлого снапшота (`NIGHTLY_METRICS=1`).
4. **Матчер**: CPU-скан (pHash/CLIP/выбор) вынесен в `asyncio.to_thread` (бит-в-бит эквивалентно, квирк best_score сохранён и запинен тестом); кэш кандидатов `src/matching/candidates.py` для match-all/rematch-all — 1 SELECT на батч вместо N (live-monitor путь не тронут, embeddings в кэш НЕ кладутся — RAM).
5. **Алерты systemd**: шаблон `otz-notify-failure@.service` + `OnFailure=` у monitor/nightly/daily-summary юнитов; у монитора явные `StartLimitIntervalSec=600`/`StartLimitBurst=5` (иначе при `RestartSec=30` дефолтное 10-с окно не выбирается → монитор рестартует вечно и в `failed` не уходит). ⚠️ **OnFailure летит на КАЖДЫЙ переход в `failed`, не один раз** — при крэш-лупе будет столько алертов, сколько рестартов (инцидент 13.06: отозванная сессия = 6 алертов за 3 мин). Исправлено постфактум — см. §0.0. Smoke: `systemctl start otz-notify-failure@smoke.service`. Docs: PHASE5_DEPLOY §5.4.
6. **Admin** (`src/admin/`): Reject / Mark-posted (DB-only, идемпотентно, бот ничего не шлёт), CSRF fail-closed (per-process токен — только single-worker uvicorn), страница `/metrics` (Chart.js по `daily_metrics`). Доставка больше не воскрешает человеческий REJECTED (`resolve_post_send_status`).
7. **Product-intel**: лог LLM-вердиктов `data/logs/llm_decisions.jsonl` (с DB id) + `otz llm-report --days N` (гистограмма confidence, what-if пороги 0.6/0.65/0.7, пограничные 0.55-0.75 с url); `otz districts-report` (незамапленные районы + готовые yaml-строки); price-drop алерты в nightly (≥`PRICE_DROP_MIN_PCT=5`%, только listings с PENDING/PUBLISHED match, cap 10 сообщений/ночь). Порог LLM 0.7 НЕ менялся — решение после 2-3 недель данных llm-report.
8. **Тесты**: stages 2-4 матчера (`test_matcher_stages.py`), event-handling монитора (`test_monitor_events.py`), дедуп-гонка, миграция 0005 upgrade/downgrade, nightly-шаги, эквивалентность перфоманс-рефактора, admin actions/CSRF, отчёты, price-drop, delivery-guard.
9. W0-бэкфилл обложек 4zida (`refetch-details --platform cetiri_zida --all`) выполнен 12.06; heal-юнит прошлой сессии завершился (битых осталось ~489 — мёртвые URL, добьёт ночной auto-heal ротацией).

### 0.1.1 Что сделано (12.06.2026 днём, коммиты `3169d92`, `32f5c67`)

1. **Полнота базы решена.** nekretnine и 4zida режут ЛЮБОЙ поиск на ~2000 результатов
   (nekretnine: 80 стр × 25, стр. 81+ → 404; 4zida: ~99 × 20, дальше повторы). Полные обходы
   теперь сегментируются по 12 ценовым диапазонам (`?prezzoMinimo/?prezzoMassimo`,
   `?skuplje_od/?jeftinije_od`) с дедупом. Боевой прогон: nekretnine 7765/7769 (99.9%),
   4zida 7122/7209 (98.8%), halooglasi 5812. Активных listings: 11.7k → **21.8k**.
   Сводка ночного прогона показывает покрытие % и 🚨-алерты (<70%, мёртвые площадки,
   пропущенные страницы).
2. **Полное ревью (71 агент, 59 подтверждённых находок) — основное исправлено:**
   matcher: pHash через int XOR popcount (было 40-75с блокировки event loop на каждый inbound →
   ~1с, бит-в-бит эквивалентно) + CLIP-блобы чанками (пик RAM 415МБ → ~30МБ); доставка не
   помечает PUBLISHED при полном фейле + flood-wait retry; stale-sweep только для здоровых
   площадок (coverage ≥50% и site_total обязателен); nightly exit 1 при мёртвых площадках;
   обложка 4zida (`@id`-референс) больше не теряется; halooglasi не вставляет пустые
   замороженные строки; новая команда `refetch-details`; семафор на pipeline в мониторе;
   парсер цен «1 200€» / «этаж 3 600€»; admin auth (не-ASCII, fail-closed дефолт-пароль);
   PRAGMA foreign_keys; абсолютный путь БД; зависимости anthropic/playwright-stealth;
   `ExecStartPost=-` (ship-log при упавшем прогоне); docs/.env.example синхронизированы.
3. Тесты: **105 passed** (`pytest -q`). Деплой на VPS сделан, monitor перезапущен, юниты
   systemd на сервере пропатчены.

### 0.2 Текущий статус (19.06.2026) + что проверить дальше

**Система здорова** (health-check 19.06 19:13 UTC): монитор `active`, `NRestarts=0` с рестарта на
hardened-код (17:06 UTC), получает обновления Telethon (слушает @OtzRentBelgrad), упавших otz-юнитов
нет, обе userbot-сессии живы (monitor + logger), таймеры взведены, последние nightly/daily-summary —
`success`, диск 59%, активных listings ~23.4k. Инцидент §0.0 закрыт и задеплоен (коммит `c31a80e`).

Проверить в следующих прогонах:
- [ ] **Ночь Jun20 03:02 UTC** — первая с бюджетом refetch. Строка «пропущено по бюджету: N» в сводке
      появляется ТОЛЬКО если heal+rolling вместе превысили `NIGHTLY_REFETCH_BUDGET_SEC=7200` (2ч).
      Если N>0 регулярно — поднять бюджет или разобраться с медленными деталями halooglasi.
- [ ] **Если монитор упадёт по сессии** — теперь это ОДИН алерт «🔐 сессия отозвана» + `exit 3`
      (НЕ рестартует, `RestartPreventExitStatus=3`). Лечится QR: `scripts/auth_qr.py` (§5.4), затем
      `systemctl reset-failed otz-monitor && systemctl restart otz-monitor`. Не SMS — см. §5.4 режим отказа.
- [ ] **W0-5 (НЕ сделано, по согласованию с Дарьей):** `otz-bg rematch-all` — перепроверит старые
      NO_MATCH и СГЕНЕРИТ ДОСТАВКИ Дарье пачкой. Предупредить её заранее. (Бэкфилл обложек 4zida
      выполнен 12.06 — 7083 строк/95.7k фото; `rematch-all` так и не прогнан.)
- [ ] Если 🚨-алерты юнитов вернутся — нотификатор де-дуплицирует одинаковые в окне ~10 мин
      (маркер `/var/log/otz/.last_alert_<unit>`); лог: `/var/log/otz/notify-failure.log`.
- [ ] Price-drop / drop-алерты active>20%: при шуме для Дарьи — поднять `PRICE_DROP_MIN_PCT` в `.env`.
      Файл БД не уменьшается после прунинга без ручного VACUUM (§15.7).

### 0.3 Бэклог (приоритизировано)

**Закрыто инцидент-hardening'ом (19.06, коммит `c31a80e`):** крэш-луп монитора при отзыве сессии
(`connect_authorized` + один алерт через бот + `exit 3` + `RestartPreventExitStatus=3`) · спам OnFailure
(де-дуп нотификатора ~10 мин) · ложная модель OnFailure в доках · QR-реавторизация (`scripts/auth_qr.py`,
обход недоставляемых SMS-кодов) · бюджет+per-fetch-cap ночного refetch (страховка от SIGTERM 16.06).

**Закрыто в H1:** весь P1 (heal, гонка дедупа, индексы, rolling re-fetch) и весь P2
(column-select, прунинг, кэш match-all, to_thread) из позапрошлого handoff'а.

**Осталось / новое из ревью:**
- `--reason` для `notify-failure` из systemd (`$SERVICE_RESULT`/`$EXIT_STATUS`) — пропущено в hardening
  (шаблон юнита передаёт только `%i`); сейчас алерт без машинной причины падения.
- nightly SIGTERM 16.06: root cause не найден (не воспроизвёлся после 16.06), смягчён бюджетом + per-fetch
  cap; если повторится — `journalctl -u otz-nightly-refresh` на предмет кто шлёт TERM.
- `send_text_via_bot` без flood-wait retry (в отличие от альбомного пути) — при 429 текстовый
  алерт молча теряется (только лог). Добавить retry-хелпер как у `_send_album_with_floodwait`.
- llm-report слеп к fail-open матчам: при LLM-фейле (529 и т.п.) строка в jsonl не пишется —
  на деградированных ночах отчёт недосчитывает. Можно писать строку с verdict=null.
- `alembic/env.py`: на пути «нет DML» DDL автокоммитится постатементно (нет явного BEGIN) —
  упавшая посреди миграция оставляет частичную схему. Перейти на явные BEGIN-хуки SQLAlchemy.
- `orchestrator.py` разросся (heal/rolling/prune/metrics/refetch+бюджет); `prune_inactive_embeddings`/
  `snapshot_daily_metrics` — чистый DB-maintenance, просится `src/maintenance.py`. Механический перенос.
- ruff: добавить `RUF001/2/3` в ignore (русские строки продукта), почистить UP017/E501 хвосты.
- Финальный 55с backoff в `llm_validator` после ПОСЛЕДНЕЙ неудачной попытки — бессмысленное
  ожидание перед giving up (latency nit).
- Watermark не двигается мимо хвостовых text-only сообщений (backfill вечно их перечитывает,
  идемпотентно) — задокументировано в `test_monitor_events.py`, чинить лениво.
- W4-T3: детерминизация pHash-vs-CLIP выбора — отдельный PR со сменой семантики (квирк
  запинен тестом `test_select_best_preserves_order_dependent_quirk`).
- Throttle-маркер / ротация halooglasi mid-walk — принято как есть (не чинить).
- `send_text_via_bot` без flood-wait retry (в отличие от альбомного пути) — при 429 текстовый
  алерт молча теряется (только лог). Добавить retry-хелпер как у `_send_album_with_floodwait`.
- llm-report слеп к fail-open матчам: при LLM-фейле (529 и т.п.) строка в jsonl не пишется —
  на деградированных ночах отчёт недосчитывает. Можно писать строку с verdict=null.
- `alembic/env.py`: на пути «нет DML» DDL автокоммитится постатементно (нет явного BEGIN) —
  упавшая посреди миграция оставляет частичную схему. Перейти на явные BEGIN-хуки SQLAlchemy.
- `orchestrator.py` вырос вдвое; `prune_inactive_embeddings`/`snapshot_daily_metrics` — чистый
  DB-maintenance, просится `src/maintenance.py`. Механический перенос.
- ruff: добавить `RUF001/2/3` в ignore (русские строки продукта), почистить UP017/E501 хвосты.
- Финальный 55с backoff в `llm_validator` после ПОСЛЕДНЕЙ неудачной попытки — бессмысленное
  ожидание перед giving up (latency nit).
- Watermark не двигается мимо хвостовых text-only сообщений (backfill вечно их перечитывает,
  идемпотентно) — задокументировано в `test_monitor_events.py`, чинить лениво.
- W4-T3: детерминизация pHash-vs-CLIP выбора — отдельный PR со сменой семантики (квирк
  запинен тестом `test_select_best_preserves_order_dependent_quirk`).
- Throttle-маркер / ротация halooglasi mid-walk — принято как есть (не чинить).

**Идеи развития:** см. §18; тюнинг `LLM_VALIDATION_MIN_CONFIDENCE` — только после 2-3 недель
данных `llm-report`.

### 0.4 Критичные правила (не нарушать)

- **Деплой = rsync** (`./scripts/deploy_sync.sh [--restart]`), НЕ git. На VPS нет git-репо.
  `.env` на сервере правится только руками. После деплоя кода матчера/монитора —
  `systemctl restart otz-monitor`, иначе старый код живёт в памяти.
- CLI на сервере запускать как `otz` (через `otz-bg` или `sudo -u otz`), не root.
- `data/` на VPS не трогать rsync'ом (фото, БД, sessions).
- Полный обход = `--max-pages > page_cap` (число страниц), иначе сегментация не включится;
  `scrape-all --full` теперь сам подставляет 500.

---

## 1. TL;DR

Автоматизация для Telegram-канала **@RentBelgrad** (ведёт Дарья, @Darya_kralj).

**Что делает:**
1. **Слушает** канал-архив **@OtzRentBelgrad** (там Дарья репостит понравившиеся квартиры от конкурентов).
2. **Скрапит** 3 серба платформы: `nekretnine.rs`, `4zida.rs`, `halooglasi.com`.
3. **Матчит** каждый новый inbound с базой listings через `pHash → CLIP → text → Claude vision LLM`.
4. **Доставляет** найденные кандидаты в Telegram-бот (`@RentFinderBot`) → Дарья сама постит их в @RentBelgrad. Мы НЕ публикуем сами.

**Кто получает доставки в бот:** chat_id=`1003207518` (Дарья), chat_id=`62399456` (mvstrike).

**Stack:** Python 3.12 · async SQLAlchemy 2 · aiosqlite (WAL) · Alembic · Telethon (userbot) · aiogram (bot) · playwright+stealth+xvfb (halooglasi) · open_clip ViT-B/32 · imagehash · anthropic SDK (`claude-haiku-4-5`).

---

## 2. VPS — подключение

| | |
|---|---|
| IP | `90.156.254.159` |
| User | `root` (для setup) / `otz` (для приложения) |
| OS | Ubuntu 24.04 |
| Specs | 8 vCPU, 8 GB RAM |
| Repo path | `/opt/otz` (owner: `otz:otz`) |
| Логи | `/var/log/otz/*.log` |
| Пароль | в password manager (не хранится в репозитории) |
| SSH | ключевая auth уже настроена, password auth disabled |

**Подключение:**
```bash
ssh root@90.156.254.159
```

Параллельно на этой же машине крутится **Outline VPN** (порты 50029/tcp+udp, 28395/tcp — открыты в ufw). Эти сервисы независимы и не должны мешать друг другу.

---

## 3. Сервисы и таймеры (systemd)

Все unit-файлы лежат в `/etc/systemd/system/otz-*` и устанавливаются через [scripts/vps_install.sh](scripts/vps_install.sh).

| Сервис | Расписание | Что делает | Лог |
|---|---|---|---|
| `otz-monitor.service` | active 24/7 | Live-listener @OtzRentBelgrad через Telethon. Каждый новый альбом → `process_new_inbound` → match → bot delivery | `/var/log/otz/monitor.log` |
| `otz-nightly-refresh.timer` | `OnCalendar=*-*-* 03:00:00` UTC (05:00 Belgrade) | Полный (non-incremental) скрап всех 3 платформ + mark stale listings inactive (`inactive_after_days=3`) | `/var/log/otz/nightly-refresh.log` |
| `otz-daily-summary.timer` | `OnCalendar=*-*-* 07:00:00` UTC (09:00 Belgrade) | Считает inbound / matched / no_match / delivered за 24ч, постит в бот сводку | `/var/log/otz/daily-summary.log` |
| `otz-site.service` (этап W) | active 24/7 | Публичный веб-каталог (FastAPI/uvicorn), `127.0.0.1:8001`, `User=otz`, `--workers 1`, за Caddy. Читает БД read-only + пишет только `leads`. Юнит: [deploy/otz-site.service](deploy/otz-site.service) | journald (`journalctl -u otz-site`) |

**Проверить статус:**
```bash
ssh root@90.156.254.159 'systemctl list-timers otz-* --all --no-pager; systemctl is-active otz-monitor.service'
```

После каждого прогона oneshot-сервиса срабатывает `ExecStartPost`, который вызывает `ship-log` — он **шлёт tail лога в Saved Messages** через второй userbot session (см. §5).

---

## 4. Adhoc-запуски через `otz-bg`

Любая ручная команда запускается **в фоне как transient systemd unit**, переживает SSH disconnect:

```bash
ssh root@90.156.254.159 'otz-bg <subcommand> [args...]'
```

Команды (полный список — `otz-bg` без аргументов):

| Команда | Назначение |
|---|---|
| `stats` | Текущие счётчики БД |
| `refresh [--force]` | Инкрементальный scrape (быстро, ~1-5 мин) |
| `nightly-refresh` | Полный scrape + stale-sweep (часы) |
| `backfill --from-date 2026-05-19` | Перечитать @OtzRentBelgrad с даты (override watermark) |
| `match-all` | Прогнать matcher на всех NEW inbound |
| `rematch-all` | Сбросить matches и прогнать заново |
| `deliver-all [--force]` | Доставить PENDING (или ВСЕ через `--force`) с force_resend=True |
| `test-batch <N>` | Reset + прогнать последние N inbound через полный pipeline |
| `daily-summary` | Ручной запуск утренней сводки |
| `backfill-nekretnine-address` | Re-fetch только адресов для nekretnine listings |
| `ship-log <file> --label X` | Послать tail файла в Saved Messages |
| `nekretnine-shadow-audit --max-pages N --compare-details N --report PATH` | Read-only snapshot/detail audit; sanitized report, без DB/Telegram |

После каждого `otz-bg` запуска **автоматически** прилетает tail (150 строк) в Saved Messages.

**Где смотреть лог запуска:**
```bash
ssh root@90.156.254.159 'tail -f /var/log/otz/adhoc-<TS>.log'
# или
ssh root@90.156.254.159 'journalctl -u otz-adhoc-<TS> -f'
```

---

## 5. Telegram: bot mode + два userbot session'а

### 5.1 Bot mode (основной канал доставки)
- **Bot:** `@RentFinderBot`, токен в `.env` как `TG_BOT_TOKEN`
- **Recipients:** `TG_BOT_RECIPIENTS=1003207518,62399456` (Дарья, mvstrike)
- **Когда:** каждый matched inbound → `send_match_via_bot` шлёт album+caption обоим recipients. Doesn't deliver to Saved Messages — у бота нет туда доступа.
- **Mode** задан в `.env`: `TG_DELIVERY_MODE=bot` (альтернатива `userbot`)

### 5.2 Userbot #1 — `otz_monitor.session` (24/7)
- Telethon session в `/opt/otz/data/otz_monitor.session`
- Используется только `otz-monitor.service` — слушает @OtzRentBelgrad
- **НЕ ТРОГАТЬ** при работающем мониторе — SQLite session lock ломает всё ("disk I/O error")
- Если нужно временно остановить: `systemctl stop otz-monitor.service`, потом запустить нужную команду, потом `systemctl start otz-monitor.service`

### 5.3 Userbot #2 — `otz_logger.session` (log shipping)
- Telethon session в `/opt/otz/data/otz_logger.session`
- Используется ТОЛЬКО командой `ship-log` для отправки tail логов в **Saved Messages** того же аккаунта
- Авторизация — **QR** (`scripts/auth_qr.py` с `OTZ_SESSION=otz_logger`, см. §5.4); legacy — `scripts/auth_logger.py` (по SMS-коду, после session-terminate обычно НЕ работает)
- Можно вызывать concurrently с monitor.service — это разные session-файлы

### 5.4 Re-authorization (если sessions сломались)

**⚠️ Режим отказа (инцидент 13.06.2026):** если владелец «завершил все сеансы» в Telegram —
обе userbot-сессии отзываются (`SessionRevokedError`). Но пока жива хоть одна app-сессия аккаунта
(телефон/Desktop), Telegram отдаёт `SentCodeTypeApp` с `next_type=None` (SMS-фоллбэка НЕТ) и in-app код
для входа сторонним клиентом придерживает. Значит вход **по коду** (`auth_telethon.py`/`auth_logger.py`)
не работает. Реавторизуйся по **QR** — он использует живую сессию на телефоне как поручителя, коды не нужны.

**Основной способ — QR-вход (`scripts/auth_qr.py`):**
```bash
# monitor session (по умолчанию tg_session_name=otz_monitor):
ssh -t root@90.156.254.159 'sudo -u otz bash -c "cd /opt/otz && .venv/bin/python scripts/auth_qr.py"'
# logger session — переопределить имя сессии через OTZ_SESSION:
ssh -t root@90.156.254.159 'sudo -u otz bash -c "cd /opt/otz && OTZ_SESSION=otz_logger .venv/bin/python scripts/auth_qr.py"'
# с облачным паролем 2FA (без stdin):
ssh -t root@90.156.254.159 'sudo -u otz bash -c "cd /opt/otz && OTZ_2FA=<пароль> .venv/bin/python scripts/auth_qr.py"'
```
На телефоне: **Настройки → Устройства → Привязать устройство** → сканировать ASCII-QR из терминала
(или PNG `/tmp/otz_login_qr.png`). QR авто-обновляется каждые ~30с, окно ~20 мин; коды возврата 0/1.
После успеха: `systemctl reset-failed otz-monitor && systemctl restart otz-monitor`.

**Legacy — вход по коду (`scripts/auth_telethon.py` / `auth_logger.py`):** работает только если Telegram
реально доставляет код; после массового «завершить все сеансы» обычно НЕ работает (см. режим отказа выше).

---

## 6. БД — SQLite (WAL mode) на VPS

**Путь:** `/opt/otz/data/otz.db` (owner otz:otz).

**Таблицы (упрощённо):**
- `source_channels` — @OtzRentBelgrad (id=1), watermark `last_processed_message_id`
- `inbound_posts` — каждый альбом из source, с `photo_paths`, `photo_phashes`, `photo_embeddings` (CLIP float16 BLOB)
- `listings` — все scraped квартиры с 3 платформ. `is_active=True/False` управляется `mark_stale_inactive`. Ключевые поля: `external_id`, `url`, `price_eur`, `area_m2`, `rooms`, `district`, `address_raw`, `lat`, `lon`, `photo_urls`, `photo_phashes`, `photo_embeddings`
- `matches` — `(inbound_post_id, listing_id)` + `confidence`, `matched_photo_count`, `method` (`phash` / `clip` / `clip+llm`), `status` (PENDING/PUBLISHED/REJECTED)
- `published_posts` — `(match_id, tg_message_id, post_text)` — фактическая доставка
- `site_listings` (этап W) — курирующий оверлей для публичного каталога: `match_id` (UNIQUE FK), `is_public`, `is_for_vnj`, `is_for_white_card`, `channel_post_url`, `note`. Сайт читает джойном к `listings`; пайплайн эту таблицу не трогает (CQRS read-model)
- `leads` (этап W) — заявки с публичной формы сайта (`name`, `contact`, `budget_eur`, `districts`, `rooms`, `message`, ...). Единственная таблица, в которую публичный app пишет (scoped writer)

**Миграции:** Alembic в [alembic/](alembic/). Применять: `cd /opt/otz && .venv/bin/alembic upgrade head`. Head — **0006** (`0006_site_listings_leads`, этап W).

**Backup перед опасными операциями:**
```bash
ssh root@90.156.254.159 'cp /opt/otz/data/otz.db /opt/otz/data/backups/otz-$(date +%F-%H%M).db'
```

---

## 7. Скраперы — особенности

[src/scrapers/](src/scrapers/) — три класса от [base.Scraper](src/scrapers/base.py).

| Платформа | Особенности |
|---|---|
| `nekretnine.rs` | Next.js app (с ~мая 2026): все данные в `<script id="__NEXT_DATA__">` JSON — чистый httpx, без DOM scraping. Details: `/oglasi/<id>/`. Photos: `pic.nekretnine.rs/image/<id>/xxl.jpg` (без подписанных токенов). **Pagination через `?pag=N`** на каноничном list URL (старый `/stambeni-objekti/...` URL 301-редиректит и теряет query params). Cap 80 страниц × 25 = 2000 результатов — обходится сегментацией по ценовым диапазонам (`?prezzoMinimo`/`?prezzoMassimo`). |
| `4zida.rs` | httpx + JSON-LD. District встречается в полях типа `Vracar Centar` или `Centar Uzi`. Аналогичный cap ~2000 результатов на поиск — обходится ценовыми диапазонами `?skuplje_od`/`?jeftinije_od`. |
| `halooglasi.com` | **Playwright + playwright-stealth + xvfb-run** для обхода Cloudflare. Запускается в headed mode. Поэтому `otz-monitor.service` и `otz-nightly-refresh.service` стартуют через `xvfb-run -a`. |

**Запуск scrape вручную:**
```bash
ssh root@90.156.254.159 'otz-bg refresh --force'                  # инкремент
ssh root@90.156.254.159 'otz-bg scrape halooglasi --max-pages 10' # одна платформа
ssh root@90.156.254.159 'otz-bg nightly-refresh'                  # полный (часы)
```

### 7.1 Инкрементальный режим
Каждая платформа использует `known_ids` (set всех `external_id` из БД) и **early-stop**: если 2 страницы подряд содержат только known IDs, прекращаем. Делает ежедневный inkrement быстрым (1-5 мин вместо часов).

### 7.2 Photo download
Внутри [src/scrapers/orchestrator.py](src/scrapers/orchestrator.py) (`_download_photos`). Лог скачиваний — JSON-lines в `data/logs/photo_downloads.jsonl`. Просмотр: `otz-bg photo-stats`.

---

## 8. Matcher — pipeline

[src/matching/matcher.py](src/matching/matcher.py) — `match_inbound(inbound_id)`:

1. **Stage 1 — pHash pre-screen**: количество фото с Hamming distance ≤6 (см. `PHASH_MAX_HAMMING`).
2. **Stage 2 — CLIP cosine**: для каждого listing подсчёт фото с CLIP similarity ≥0.92 (см. `CLIP_SIMILARITY_THRESHOLD`).
3. **Stage 3 — text cross-check** (только для CLIP-кандидатов): сравнение цены/площади из caption Telegram-поста с listing.
4. **Stage 4 — Claude vision LLM** (только для CLIP-кандидатов): [src/matching/llm_validator.py](src/matching/llm_validator.py) шлёт оба набора фото Claude Haiku и спрашивает «это одна квартира?». Fail-open: если LLM crashed / 529 OverloadedError, оставляем CLIP-вердикт.

**Порог:** минимум `min_matched_photos=2` (см. `.env`).

**LLM model:** `claude-haiku-4-5` — задан в [src/matching/llm_validator.py](src/matching/llm_validator.py). НИКОГДА не менять на legacy строку с date-суффиксом.

---

## 9. Доставка (delivery)

[src/telegram/delivery.py](src/telegram/delivery.py) — dispatcher по `TG_DELIVERY_MODE`:

| Mode | Module | Куда |
|---|---|---|
| `bot` (prod) | [bot_delivery.py](src/telegram/bot_delivery.py) — aiogram | Все chat_id в `TG_BOT_RECIPIENTS` |
| `userbot` (legacy) | [delivery.py](src/telegram/delivery.py) — Telethon | `TG_DELIVERY_TARGET` (или Saved Messages если `me`) |

**Идемпотентность:** если `PublishedPost(match_id)` существует, повторный `send_match_to_saved` skip'ает (если не `force_resend=True`).

**Сообщения формируются в [src/post/template.py](src/post/template.py)**:
- Header: «🏠 Кандидат на @RentBelgrad — {platform}», ссылки на listing + Telegram source, кол-во matched фото + confidence + method
- Body: render_post(listing, inbound).text — цена, площадь, **Yandex Maps URL** (через [src/post/location.py](src/post/location.py) — text-search с улицей), описание из площадки
- Hashtags: **районный** ([src/post/hashtags.py](src/post/hashtags.py) → [data/districts.yaml](data/districts.yaml)) + ценовой + типа квартиры + brand `#квартирыOtz`

### 9.1 Yandex Maps URL (важно)
[src/post/location.py](src/post/location.py) — приоритет:
1. Если есть `address` → `?text=<address>, Beograd&ll=lon,lat&z=17` — Yandex резолвит и **подсвечивает улицу**
2. Coords-only fallback → `?ll=...&pt=...` (голый красный пин)

**Адрес есть у:**
- HALOOGLASI: ~84%
- CETIRI_ZIDA: ~76%
- NEKRETNINE: ~82% (заполнено через `backfill-nekretnine-address`)

---

## 10. Configuration (.env)

`/opt/otz/.env` — env-файл, **не синкается** через `deploy_sync.sh`. Ключи:

```ini
TG_API_ID=...                     # my.telegram.org
TG_API_HASH=...
TG_SESSION_NAME=otz_monitor
TG_DELIVERY_MODE=bot              # bot | userbot
TG_BOT_TOKEN=...                  # @RentFinderBot
TG_BOT_RECIPIENTS=1003207518,62399456
TG_DELIVERY_TARGET=me             # для legacy userbot mode
ANTHROPIC_API_KEY=sk-ant-...
ADMIN_PASSWORD=...                # FastAPI admin (read-only)
SOURCE_DEFAULT_START_DATE=2026-01-01
ENABLE_LLM_VALIDATION=true
CLIP_SIMILARITY_THRESHOLD=0.92
PHASH_MAX_HAMMING=6
MIN_MATCHED_PHOTOS=2
# --- этап H1 (дефолты в config.py боевые; в .env можно не задавать) ---
NIGHTLY_HEAL_LIMIT=500            # 0 = пропустить auto-heal
NIGHTLY_DETAIL_REFRESH=200        # 0 = пропустить rolling re-fetch цен
NIGHTLY_PRUNE_EMBEDDINGS=1        # 0 = не прунить embeddings неактивных
NIGHTLY_METRICS=1                 # 0 = не писать daily_metrics / drop-алерт
PRICE_DROP_ALERTS=true
PRICE_DROP_MIN_PCT=5
# --- этап W (публичный сайт; добавить в /opt/otz/.env руками при включении Caddy) ---
SITE_BIND_HOST=127.0.0.1
SITE_BIND_PORT=8001
SITE_PUBLIC_BASE_URL=https://rent.example.com   # заменить на реальный сабдомен
SITE_CACHE_TTL=300
SITE_PAGE_SIZE=24
SITE_PHOTOS_ROOT=/opt/otz/data/inbound          # = root /photos в Caddyfile
SITE_CHANNEL_URL=https://t.me/RentBelgrad
SITE_LEAD_RATELIMIT_MAX=5
SITE_LEAD_RATELIMIT_WINDOW=3600
# нотис заявки уходит Дарье через уже настроенный бот (TG_BOT_TOKEN / TG_BOT_RECIPIENTS)
```

Шаблон: [.env.example](.env.example). Полный Stage-W блок + деплой-файлы — `deploy/env.additions.txt`, `deploy/Caddyfile`, `deploy/otz-site.service`.

---

## 11. Deploy workflow (с laptop на VPS)

```bash
# отредактируй файлы локально
.venv/bin/python -m pytest tests/ -q     # 100+ тестов, должны пройти

# rsync code → VPS (excludes data/, .env, .venv, .session, halooglasi fixtures)
bash scripts/deploy_sync.sh

# если изменены файлы которые читает live monitor — перезапустить:
bash scripts/deploy_sync.sh --restart
```

**deploy_sync.sh внутри:**
- `rsync -rlptzv --delete-after` — **не сохраняет owner/group** (macOS rsync 2.6 не поддерживает `--chown`)
- После rsync: `ssh ... 'chown -R otz:otz /opt/otz/{data,src,scripts,tests}'`
- Эксепшен: `data/districts.yaml` синкается (с `--include`), остальные `data/*` excluded

**ВАЖНО:** не запускать `rsync -a` — это переписывало `data/` под UID 501:staff (mac user), и otz user терял доступ. См. историю фиксов §13.

---

## 12. Локальная разработка

```bash
# venv (1 ГБ)
uv sync                    # или: python -m venv .venv && pip install -e .
.venv/bin/pytest tests/ -q

# локально БД нет — все тесты используют in-memory или fixtures
# фактический прогон CLI требует .env + .session, обычно проще тестировать на VPS
```

**Что есть локально:**
- `src/`, `tests/`, `scripts/`, `alembic/`, `docs/`
- `data/districts.yaml` (это конфиг, синкается на VPS)
- `.env` (для локальных smoke runs, при необходимости)
- `.venv/`

**Чего нет локально (всё на VPS):**
- `data/otz.db`, `data/inbound/`, `data/listings/`, `data/playwright/` — 9+ ГБ, удалены 2026-05-20 после деплоя

---

## 13. Известные нюансы / quirks

### 13.1 SQLite session lock
Telethon хранит auth_key в `.session` (SQLite). Два процесса на один session-файл → `disk I/O error`. Решение: остановить `otz-monitor.service` или использовать `otz_logger.session` (отдельный).

### 13.2 Throttle race в scrape
[scrape_all_with_throttle](src/scrapers/orchestrator.py) использует file marker `data/state/last_scrape.txt`. Если 5 параллельных pipeline вызовут одновременно — race, все 5 пройдут throttle. Не критично — listings upsert'ятся без дублей.

### 13.3 pipeline.py: bot mode + client=None
[src/jobs/pipeline.py](src/jobs/pipeline.py) — раньше тихо пропускал доставку при `client is None`. Сейчас проверяет mode: пропуск только если `mode == userbot AND client is None`. В bot mode delivery вызывается всегда.

### 13.4 LLM validator non-determinism
Claude может разный verdict давать на одну и ту же пару фото. На практике stable enough для 0.7 conf threshold, но `test-batch` повторно может дать другую раскладку matched/no_match.

### 13.5 Telegram Bot API rate limits
- 30 msg/sec глобально для бота
- 20/sec в один чат
- При burst > 10 матчей одновременно — может быть FloodWait. Тогда `PublishedPost.tg_message_id=0`, fix через `otz-bg deliver-all --force`.

### 13.6 districts.yaml — единственный синкаемый data-файл
Конфиг маппинга район → хэштег. Если добавить новый микрорайон, не забыть `deploy_sync.sh` (он синкает только этот файл из `data/`).

---

## 14. История фиксов (этой сессии: 2026-05-20 — 2026-05-21)

### 14.1 Yandex URL подсвечивает улицу
- Было: `?ll=lon,lat&pt=lon,lat` — голый красный пин
- Стало: `?text=<address>, Beograd&ll=lon,lat&z=17`
- Файл: [src/post/location.py](src/post/location.py)

### 14.2 nekretnine address extraction
- Было: `address_raw=None` хардкоден
- Стало: `_extract_address` парсит `.property__location > ul > li`
- Backfill: 5445/6633 listings обновлены (82% coverage)

### 14.3 districts.yaml на VPS
- Было: `data/` excluded в rsync → yaml не доходил → `district_tag()` всегда None
- Стало: явный `--include='data/districts.yaml'` exception
- Удалён дубликат «Beograd na vodi: Savskivenac» (был после правильного → NewBeograd)
- Добавлен «Čubura → Vracar»

### 14.4 pipeline.py: bot mode delivery
- См. §13.3 выше — bot mode теперь не пропускает доставку при `client=None`

### 14.5 rsync ownership
- Было: `rsync -a` → `data/` принадлежал 501:staff → otz user не мог писать
- Стало: `-rlptzv` без preserve owner + явный `chown -R otz:otz` после

### 14.6 weekly-refresh → nightly-refresh
- CLI команда переименована (старая `weekly-refresh` оставлена hidden alias)
- Stale-grace уменьшен с 10 дней до 3 (ежедневная cadence)
- Telegram summary через `send_text_to_recipients` (bot-aware) вместо прямого Telethon
- systemd timer: `OnCalendar=*-*-* 03:00:00` daily (было: Sun 03:00)

### 14.7 deliver-all --force
- Добавлен флаг для re-rendering ВСЕХ matches (включая PUBLISHED) — нужно после template/URL изменений

### 14.8 daily-summary
- Новая CLI команда, считает inbound/matched/no_match/delivered по `InboundPost.status` за окно (default 24h)
- systemd timer `OnCalendar=*-*-* 07:00:00` daily

### 14.9 ship-log → Saved Messages
- Новая CLI команда, шлёт tail лога в Saved Messages через `otz_logger.session`
- `ExecStartPost` в каждом oneshot service unit + в `otz-bg` wrapper
- Авторизация: `scripts/auth_logger.py` (одноразовая)

---

## 15. Полезные snippets

### 15.1 Свежий state БД
```bash
ssh root@90.156.254.159 'otz-bg stats'
```

### 15.2 Принудительно прислать все matches заново
```bash
ssh root@90.156.254.159 'otz-bg deliver-all --force'
```

### 15.3 Реобработать последние N inbound
```bash
ssh root@90.156.254.159 'otz-bg test-batch 10'
```

### 15.4 Логи monitor live
```bash
ssh root@90.156.254.159 'journalctl -u otz-monitor -f'
```

### 15.5 SQL ad-hoc
```bash
ssh root@90.156.254.159 'sqlite3 /opt/otz/data/otz.db "SELECT COUNT(*) FROM matches WHERE status=\"PENDING\";"'
```

### 15.6 Восстановить ownership на VPS если что-то сломалось
```bash
ssh root@90.156.254.159 'chown -R otz:otz /opt/otz'
```

### 15.7 Ручной VACUUM (файл БД не сжимается сам после прунинга embeddings)
```bash
# ТОЛЬКО руками: backup + остановленный monitor, иначе session lock / огромный WAL
ssh root@90.156.254.159 'systemctl stop otz-monitor && \
  cp /opt/otz/data/otz.db /opt/otz/data/backups/otz-$(date +%F-%H%M).db && \
  sudo -u otz sqlite3 /opt/otz/data/otz.db "VACUUM;" && \
  systemctl start otz-monitor'
```

### 15.8 Новые отчёты (H1)
```bash
ssh root@90.156.254.159 'otz-bg llm-report --days 14'   # тюнинг LLM-порога: гистограмма + what-if
ssh root@90.156.254.159 'otz-bg districts-report'        # незамапленные районы + yaml-строки
```

### 15.9 Smoke OnFailure-алертов
```bash
ssh root@90.156.254.159 'systemctl start otz-notify-failure@smoke.service'  # 🚨 в бот
```

---

## 16. Что НЕ делать

- ❌ Не публиковать что-либо в @RentBelgrad из бота — это работа Дарьи
- ❌ Не доставлять unmatched inbounds в бот — только matched, иначе спам Дарье
- ❌ Не trogать `otz_monitor.session` пока запущен `otz-monitor.service`
- ❌ Не использовать `rsync -a` на /opt/otz — ломает ownership
- ❌ Не указывать date-суффиксы в model ID (использовать `claude-haiku-4-5`, НЕ `claude-haiku-4-5-20251001`)
- ❌ Не комментить детальные docstrings — стиль проекта: лаконичные комментарии только для нетривиального WHY

---

## 17. Где искать что (карта файлов)

```
src/
├── cli.py                          # все CLI команды (Click)
├── config.py                       # settings из .env
├── db/
│   ├── models.py                   # SQLAlchemy модели
│   └── session.py                  # async session factory
├── scrapers/
│   ├── base.py                     # Scraper ABC, ListingPreview/Detail
│   ├── orchestrator.py             # scrape_all_with_throttle, mark_stale_inactive, photo download
│   ├── shadow_audit.py              # read-only snapshot/detail quality gate
│   ├── proxy_pool.py                # secret-safe Nekretnine proxy slots/failover
│   ├── nekretnine.py
│   ├── cetiri_zida.py
│   └── halooglasi.py               # playwright + stealth + xvfb
├── matching/
│   ├── matcher.py                  # match_inbound — pipeline
│   ├── phash.py                    # imagehash wrappers
│   ├── clip_embed.py               # open_clip ViT-B/32
│   └── llm_validator.py            # Claude Haiku vision
├── telegram/
│   ├── monitor.py                  # Telethon — live + backfill
│   ├── delivery.py                 # dispatcher + send_text_to_recipients
│   └── bot_delivery.py             # aiogram — send_match_via_bot
├── jobs/
│   ├── pipeline.py                 # process_new_inbound
│   └── photo_stats.py              # photo_downloads.jsonl aggregator
├── post/
│   ├── template.py                 # render_post
│   ├── hashtags.py                 # build_hashtags, district_tag
│   └── location.py                 # yandex_maps_url
├── admin/                          # FastAPI read-only UI + publish-to-site контролы (этап W)
└── site/                           # этап W: публичный каталог (FastAPI+Jinja, read-only БД)
    ├── app.py                      # uvicorn src.site.app:app (127.0.0.1:8001)
    ├── format.py                   # нормализация rooms/district для показа (покрыт тестами)
    ├── templates/                  # Jinja (index/catalog/apt/map/calculator + partials)
    └── static/                     # CSS-токены, filters.js, favorites.js (отдаёт Caddy)

deploy/                             # этап W: артефакты деплоя публичного сайта
├── otz-site.service               # systemd-юнит публичного app
├── Caddyfile                      # reverse-proxy + file_server для /static и /photos
└── env.additions.txt              # SITE_* ключи для /opt/otz/.env

scripts/
├── vps_install.sh                  # one-shot bootstrap для Ubuntu 24.04
├── deploy_sync.sh                  # rsync laptop → VPS
├── auth_telethon.py                # auth для otz_monitor.session
├── auth_logger.py                  # auth для otz_logger.session
└── auth_qr.py                      # QR-реавторизация сессий (см. §5.4)

data/
└── districts.yaml                  # район → хэштег (единственный синкаемый data-файл)

tests/
├── test_*.py                       # 100+ тестов, должны проходить
└── fixtures/                       # HTML fixtures для скраперов

alembic/
└── versions/                       # 0001..0003 миграции
```

---

## 18. Что дальше (возможные направления)

- **Admin UI** ([src/admin/](src/admin/)) — FastAPI + HTMX, read-only. Запуск: `otz-bg admin`. Можно расширить editing.
- **Расширить районы**: при появлении новых micro-locations в халооглази/4zida — добавить в `data/districts.yaml`.
- **Алерты при сбоях monitor**: `OnFailure=` хук в `otz-monitor.service` → шлёт alert через `ship-log` или `send_text_to_recipients`.
- **Метрики**: уже есть `photo_downloads.jsonl`. Можно завести dashboard (Grafana / простой HTML отчёт).
- **Тюнинг LLM threshold**: сейчас 0.7 (`LLM_VALIDATION_MIN_CONFIDENCE`) — иногда зарезает легитимные матчи (см. логи `REJECTED by LLM`).
