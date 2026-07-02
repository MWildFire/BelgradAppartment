# Session handoff — Stage T «Insight & Integrity» (2026-07-02)

> **Статус:** ✅ **КОД ГОТОВ + ОТРЕВЬЮЕН** (Wave 5 = **SHIP**) · ⏳ **ДЕПЛОЙ ЖДЁТ joint-go** с Дмитрием.
> Ветка **`feature/insight-and-integrity`** от `main` @ **`009d229`**. **29 коммитов** (`ce21ea5..5c2b42f`),
> **79 файлов, +8828 / −139**. Гейт зелёный: **юниты 1055 / e2e 52** — гоняются **раздельно**
> (комбинированный прогон невозможен by design, см. §5 [15]). Alembic head **0010** (одна миграция).
> **Ноль новых зависимостей.** План: `docs/superpowers/plans/2026-07-02-insight-and-integrity.md`;
> решения: `docs/web/DECISIONS-T.md`; ревью: `docs/review/stage-t/REVIEW_FINDINGS.md`.
> Предыдущий: [`SESSION_HANDOFF_2026-06-24.md`](SESSION_HANDOFF_2026-06-24.md) (Stage S).

---

## 0. TL;DR
Пять рычагов закрыты: **#1** «летим вслепую» → первопартийная server-side аналитика без cookies/баннеров/
внешних сервисов + дашборд `/admin/analytics`; **#2** дыра приватности глобальной карты (`/api/map.json`
отдавал точные координаты зданий — подтверждено на проде, 4/4 точки = БД) → снэп `fuzz_coords` ~100 м;
**#3** фото-мегабайты (LCP `/apt/3` = 16 с) → webp-варианты + `<picture>`/srcset + манифест (CLS→0);
**#4** гигиена данных (26 400 inactive-трупов, `_heal_urls` без гарда, дедуп не в nightly) → гард + purge
с архивом (dry-run) + честный `stats` + `dupes-report` в nightly; **#5** большое ревью всего репо (10 линз,
состязательная верификация) → 40 находок → TDD-фиксы → **SHIP**. Одна миграция 0010, ноль новых зависимостей.

Исполнение — мульти-агентное по методологии obra/superpowers: волны последовательны, задачи внутри волны —
параллельные субагенты по ownership-матрице (план §3); на каждую задачу свежий implementer + два
состязательных ревью-субагента (spec-compliance + code-quality); TDD red→green везде.

## 1. Что сделано (по волнам)

| Wave | Коммиты | Суть |
|------|---------|------|
| **0** | `ce21ea5` `98a189e` | Стейдж-кит T (план/контракт/бэклог/env). `DECISIONS-T.md`: дефолты D1-D6 зафиксированы (блокеров нет). Диагностика прода (read-only): **утечка координат подтверждена** (4/4 точки `/api/map.json` = `listings.lat/lon`); фото **2535 файлов ~945 MB** (p95=1.43 MB); **26 400 inactive** (не ~8442 из плана; 31 дотягивается до `site_listings` через FK); nightly-точка = `_run_nightly_steps` (**новый timer не нужен**); Lighthouse baseline (`/`=0.95, `/apt/3` LCP=**16 с**). |
| **1** | `052fb75` `a461328` `6bac0d9` `33eec02` `6ebc0c6` | **T1-1** фуззи `/api/map.json` (`readmodel.map_points` снэпает через `fuzz_coords` ~100 м **до** сериализации, кластеры по снэпнутым) + постоянный e2e-гард `_assert_no_precise_coords`. **T1-2** гард `_heal_urls` (только `is_active=1` + canonical-resolves; escape-hatch `--include-inactive`). **T1-3** `otz purge-inactive --older-than N [--dry-run]` (архив `data/archive/*.jsonl.gz`, FK-отказ, TOCTOU-гард; в этапе — только dry-run, D5) + честный `otz stats` (active/inactive). **T1-4** `dupes-report` fault-isolated в nightly (report-only, `data/reports/`). fix: `IntRange(min=1)` для `--older-than`, TOCTOU-гард delete. |
| **2** | `0f1fea7` `26870ed` `f600f1f` `3c70f7e` `51da3ec` `c176f5a` | **T2-1** миграция **0010** (`site_events`, `site_stats_daily`, `leads.utm_*`) + модели. **T2-2** `analytics.record_event` + хуки в 7 GET-роутах аллоулиста через `BackgroundTasks` (`visitor=sha256(daily_salt‖ip‖ua)[:16]`, сырые IP/UA не пишутся; бот-эвристика; HX/не-2xx скип; kill-switch; **сбой ≠ 5xx**). **T2-4** rollup/retention nightly (идемпотентный upsert, retention 90д только `_rolled`-дням). **T2-3** first-touch utm-атрибуция (`static/js/utm.js` + hidden-поля + серверный фоллбэк, события после коммита). fix-suite/review: адаптация соседних тестов к 0010, docstring `db.py` (writer = leads+subscriptions+site_events). |
| **3** | `93d0f3a` `d0848bd` | **T3-1** `GET /admin/analytics?days=7\|30` — карточки (views/uniques/leads/conv%), inline-SVG спарклайны, топ-квартиры с CTR, источники (utm/referrer); токены Belgrade Editorial, ноль JS-библиотек. **T3-2** колонка «просмотров 7д» в admin-списке публичных листингов. |
| **4** | `4dbe7ba` `9a31e62` `22cdbd2` `37e47ba` `4dc1412` `eb6cc9e` `92d66ea` | **T4-1** `src/site/photos.py` — webp `card`(640w)/`detail`(1280w) q82 + `photo_manifest.json`, publish-hook, `otz photos-backfill`. **T4-2** Jinja `photo_v`/`photo_dims` + `<picture>`/srcset (CLS→0, фоллбэк без манифеста). **T4-4** вынос inline-обработчиков в `static/js` (готовность снять `unsafe-inline`). **T4-3** Caddyfile immutable `/photos/*` + CSP-комментарий. fix: сериализация мерджа манифеста (lock + уникальный tmp), C1-фоллбэк фото в `img-fallback.js`, `run_coro` в conftest (сид фикстур в worker-треде). |
| **5** | `2a574d6` `765b439` `c36044a` `8cb9967` `e562146` `bc4b6b3` `5c2b42f` | **10-линзовое read-only ревью всего репо + состязательная верификация** (`docs/review/stage-t/REVIEW_FINDINGS.md`): **54 находки → 45 после дедупа → 40 подтв. / 5 опровергнуто, 0 blocker, 14 major**. Все major закрыты TDD-фиксами: `2a574d6` F1-analytics (fail-closed соль, общий `ref_domain`, догон `unrolled_days`, `_rolled` только финальным дням, kill-switch nightly), `765b439` F2-photos (манифест в родителе `photos_root` + Caddy `hide`, EXIF-transpose, кросс-процессный `flock`, флаш каждые 100), `c36044a` F3-frontend (a11y + LCP-featured + честные sizes), `8cb9967` F4-cache-valid (maxsize+эвикция TTL-кэша, лимиты длин districts/rooms), `e562146` F5-admin (SRI, caption, AA-контраст), `bc4b6b3` F6-tests (gzip-append + TOCTOU покрытие). `5c2b42f` — докс-линия F7 + findings. Re-review: **SHIP**. |

## 2. Новая поверхность (для деплоя)
- **Миграция 0010** (`20260702_0010_analytics.py`) — единственная новая; downgrade полноценный (up/down roundtrip покрыт).
- **Новые модули:** `src/site/analytics.py` (record_event + rollup + retention), `src/site/photos.py` (webp+манифест), `src/admin/analytics.py` (контекст дашборда).
- **Новый роут:** `GET /admin/analytics?days=7|30` (админ-app; шаблон `src/admin/templates/analytics.html`).
- **Хуки записи:** 7 GET-роутов аллоулиста в `src/site/routes.py` (`BackgroundTasks → analytics.record_event`); события `lead_submit`/`subscribe_submit` пишутся в `routes.py` **после** коммита lead/подписки (некритично для ответа).
- **Новые статики:** `static/js/{utm,img-fallback,subscribe-form}.js`.
- **Новые CLI:** `otz purge-inactive`, `otz photos-backfill`, `otz dupes-report`; `otz stats` расширен (active/inactive).
- **Новые env:** `SITE_ANALYTICS_ENABLED=true`, `SITE_ANALYTICS_SALT=`, `SITE_ANALYTICS_RAW_RETENTION_DAYS=90` (`docs/web/env.additions-t.txt`). В `.env.example` соль пустая.
- **Nightly:** новые fault-isolated шаги `analytics` + `dupes-report` подвешены в `_run_nightly_steps` (**новый timer НЕ создаётся**, T0-2.4).
- **Caddyfile:** immutable `/photos/*` + `hide photo_manifest.json*` + CSP-готовность снять `unsafe-inline` из `script-src`.
- **Derived data на VPS:** webp-варианты + `photo_manifest.json` в git/rsync не участвуют (`data/` исключена).
- **Ноль новых зависимостей** (Pillow уже прямая). Alembic head = **0010**.

## 3. Деплой на прод — ⏳ ЖДЁТ joint-go (раннбук = план §2 Wave 6)
> Раннбук **НЕ переписываем** — исполняется по `docs/superpowers/plans/2026-07-02-insight-and-integrity.md`
> §2 Wave 6 (backup → rsync → env → миграция 0010 → photos-backfill → nightly-подвес → рестарт → smoke →
> merge). Ниже — только **особенности после фиксов Wave 5**, которых нет в исходном раннбуке:

- **Шаг 6 (photos-backfill) — запускать в фоне** (`otz-bg`/`tmux`/`nohup`): ~2535 оригиналов × 2 варианта ×
  webp `method=6` = десятки минут, обрыв ssh убьёт процесс. Манифест пишется **инкрементально (флаш каждые
  100)**, поэтому обрыв теряет ≤100 записей, повторный прогон идемпотентен (добор без потери webp). Сначала
  `--dry-run` → цифры, затем боевой.
- **Проверить манифест в РОДИТЕЛЕ** `SITE_PHOTOS_ROOT`: файл должен появиться как **`/opt/otz/data/photo_manifest.json`**
  (не в `/opt/otz/data/inbound`); Caddy `hide photo_manifest.json*` скрывает его из `/photos`.
- **`SITE_ANALYTICS_SALT` обязателен:** пустая соль при `SITE_ANALYTICS_ENABLED=true` → **fail-closed no-op**
  (события не пишутся, один warn на процесс). Сгенерить на сервере `openssl rand -hex 32`, вписать в
  `/opt/otz/.env` (никогда в гит). Без этого дашборд будет пуст.
- **Nightly без нового timer'а:** rollup/retention/dupes уже подвешены в `otz-nightly-refresh` через
  `_run_nightly_steps` — отдельный `systemctl enable` не нужен, только проверить чистый прогон.
- **Rollback:** аналитика — `SITE_ANALYTICS_ENABLED=false` (глушит хуки **и** nightly-шаг целиком); фото —
  `photo_v` фоллбэчит на оригиналы (варианты+манифест можно удалить); схема — `alembic downgrade -1`
  (0010 аддитивна).
- **Lighthouse-дельту** к baseline T0-2 (`/`=0.95, `/apt/3` LCP=16 с) мерить **тем же способом** (локальный
  lighthouse, mobile, simulate — PSI API упирался в квоту).

## 4. Ревью Wave 5 — вердикт SHIP (кратко)
- Воронка: **54 → 45 (дедуп) → 40 подтв. / 5 опровергнуто**. Блокеров **0**, major **14** (все закрыты
  TDD-фиксами), minor 26 (8 докс-дрейфа → синхрон Wave 6, 3 → бэклог, остальные закрыты).
- Опровергнуто 5 (в т.ч. «rollup abort вчера→сегодня» = осознанный контракт §N; «90-числовой aria-label
  спарклайна» = окна только `(7,30)`; «emoji-бейдж просмотров» = правило no-emoji только для публичного сайта).
- Полный список и `file:line` — `docs/review/stage-t/REVIEW_FINDINGS.md`.

## 5. Открытые пункты для Дмитрия
1. **Deploy `go`** на Wave 6 — каждый ssh-шаг раннбука подтверждается совместно.
2. **`go` на боевой `purge-inactive`** — только после чтения dry-run отчёта (**26 400** inactive; в этапе —
   только dry-run, D5). Бэклог `docs/BACKLOG.md` #3.
3. **Cloudflare Wave 3b** остаётся в бэклоге #1 (в этап T не входит, решение 02.07; repo подготовлен ещё в Stage S).

## 6. В бэклог (осознанно, из ревью)
- **[15]** комбинированный `RUN_E2E=1 pytest tests/` падает order-зависимостью pytest-asyncio после playwright
  (sync-драйвер оставляет running-loop) → документированный гейт = **два раздельных запуска**; полная изоляция — отдельная тема.
- **[18]** дубль utm-санитайза `leads.py`/`analytics.py` (`ref_domain` уже консолидирован) → вынос в
  `attribution.py` — рефактор без дефекта.
- **[20]** слоистость `auto_publish_site_listing` в `src/admin`, исполняется пайплайном (пре-существующий
  дрейф Stage W2, T добавил Pillow-генерацию) → вынос publish-домена в нейтральный модуль.

---
**Итог:** Stage T закрыт в ветке, отревьюен (10 линз + re-review = **SHIP**), полный gate зелёный
(1055 / 52 раздельно), одна миграция 0010, ноль новых зависимостей. Осталось человеку: совместный
деплой по раннбуку (§3), `go` на боевой purge (§5.2), `go` на мерж в `main` + push origin.
