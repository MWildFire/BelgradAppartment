# DECISIONS — Stage T «Insight & Integrity»

> Этап: **Stage T** · Дата: 2026-07-02 · Ветка: `feature/insight-and-integrity`
> Источник: `docs/superpowers/plans/2026-07-02-insight-and-integrity.md` (Wave 0, таблица §4).
> Статус: **зафиксировано** — блокирующих вопросов нет, применены все дефолты плана.

---

## Продуктовые решения (T0-1)

| # | Вопрос | Решение | Заметки |
|---|--------|---------|---------|
| **D1** | Где живёт аналитика | **Server-side в той же SQLite**, без cookies, без внешних скриптов, без баннеров | GA/Plausible/Umami отклонены архитектурно (второй рантайм против ADR «одна машина, один тулчейн»; адблоки; cookie-баннер). Инвариант записи публичного app расширяется на `site_events` (append-only, контракт §0). |
| **D2** | Атрибуция источника заявки | **first-touch utm+referrer в sessionStorage** (`static/js/utm.js`) + серверный фоллбэк same-page query; без cookies | cookie-first-touch отклонён (тянет consent-баннер). |
| **D3** | Ретеншн сырых событий | **90 дней** (`SITE_ANALYTICS_RAW_RETENTION_DAYS=90`); агрегаты `site_stats_daily` — вечно | Удаление raw только для дней с маркером `_rolled` (гард от потери несагрегированного). |
| **D4** | Фуззи `/map` | **Тот же `fuzz_coords`-снэп (~100 м)**, что на `/apt` — единая политика приватности | Круг-на-пин отклонён (визуальный шум на кластерах); «оставить точные» — это и есть дыра. |
| **D5** | Судьба inactive-«трупов» | **В этапе — только dry-run отчёт + инструмент с архивом.** Физический purge — отдельный `go` Дмитрия после чтения отчёта | ⚠️ Реальный масштаб — **26 400** inactive (см. T0-2.3), не ~8442 из плана. |
| **D6** | Дедуп | **Nightly report-only** (`data/reports/dupes-YYYY-MM-DD.txt`), Дарья курирует руками | Авто-мерж отклонён (риск склейки разных квартир). |

### Реалии-vs-план дельты (зафиксировано в Wave 0)

- **База ветки — `009d229`**, а не `8626e1a` из плана: на локальном `main` поверх Stage S лежит ещё один docs-коммит (handoff-refresh 2026-07-01). Функциональная база та же (Stage S смёржен).
- **Inactive-строк не ~8442, а 26 400** (active — 23 130, итого 49 530). Порядок работ T1-3 не меняется (dry-run + архив + FK-защита), но объём архива будет ощутимо больше — учесть в отчёте dry-run.
- **Пол suite выше планового:** фактический базлайн worktree — **843 passed** (юниты; план говорил 832) + 33 skipped, e2e — **32 passed**. Пол этапа поднят до 843/32.
- **`BACKLOG.md` размещён в `docs/BACKLOG.md`** (по STAGE_T_INDEX; в комплекте лежал рядом с web-доками).
- **Nightly-раннер уже существует** — новый systemd-timer НЕ нужен (см. T0-2.4).

---

## T0-2 — Диагностика (read-only, прогон 2026-07-02)

### 1. Утечка точных координат — ПОДТВЕРЖДЕНА

`GET https://rentbelgrad.com/api/map.json` (7323 байта) сверен с прод-БД
(`site_listings.match_id → matches.listing_id → listings.lat/lon`):

| map id | map.json lat/lon | listings.lat/lon | Совпадение |
|---|---|---|---|
| 1 | 44.809043 / 20.45019 | 44.809043 / 20.45019 | **точное** |
| 2 | 44.811325 / 20.451078 | 44.811325 / 20.451078 | **точное** |
| 3 | 44.816884149726 / 20.463808222589 | 44.816884149726 / 20.463808222589 | **точное, полная float-точность** |
| 6 | 44.81700134 / 20.4810009 | 44.81700134 / 20.4810009 | **точное** |

4/4 выборочных точки идентичны БД → любой открывший JSON получает адрес здания. T1-1 обоснован фактически.

### 2. Вес фото (`SITE_PHOTOS_ROOT=/opt/otz/data/inbound`, ssh read-only find)

- **2535 файлов, суммарно ~945.6 MB** (~902 MiB).
- **p50 = 127 KB · p95 = 1.43 MB** — оригиналы уходят на карточки/галерею как есть.
- Вывод: цели DoD (card p95 ≤ 120 KB, detail p95 ≤ 350 KB) дают ~10× экономию на p95.

### 3. Inactive-строки

- `is_active=0`: **26 400** · `is_active=1`: 23 130.
- FK-защита: **102** inactive имеют ссылки из `matches`; из них **31** дотягивается до `site_listings`
  (через `matches.id = site_listings.match_id`). Эти строки purge обязан отказаться удалять.
- Для `--older-than` есть `listings.last_seen_at` (DATETIME, NOT NULL).

### 4. Механизм nightly — ЕСТЬ, новый timer не нужен

- systemd: `otz-nightly-refresh.timer` (@ ~03:04 UTC) → `otz-nightly-refresh.service` → `src/cli.py::nightly-refresh`.
- Внутри — `_run_nightly_steps()` (`src/cli.py`): **fault-isolated шаги** (сбой шага → 🚨-строка в
  ТГ-summary, nightly не падает). Паттерн уже используют price-drop и site-retention.
- **Решение:** T1-4 (dupes-report), T2-4 (rollup+retention) подвешиваются как новые fault-isolated
  шаги `_run_nightly_steps` — отдельный `otz-nightly.timer` из плана §N НЕ создаётся.
- Прочие таймеры: `otz-scrape-smoke.timer`, `otz-daily-summary.timer` — не трогаем.

### 5. Baseline Lighthouse mobile (локальный lighthouse, headless Chrome, throttling simulate)

| Страница | Perf | LCP | CLS | FCP | Speed Index |
|---|---|---|---|---|---|
| `/` | **0.95** | 2.8 s | 0.026 | 1.7 s | 2.4 s |
| `/apt/3` | **0.63** | **16.0 s** | 0 | 2.7 s | 7.8 s |

LCP 16 s на детальной странице — прямое следствие оригиналов-мегабайтников в галерее; Wave 4
целится ровно сюда. Дельту после деплоя мерить тем же способом (локальный lighthouse, mobile,
simulate) — PSI API в Wave 0 упёрся в квоту.

### 6. Git-санитария

`git fetch` → `main == origin/main == 009d229` — расхождения нет, толкать нечего было.
Worktree `feature/insight-and-integrity` создан от локального `main`.

### 7. Пол suite зафиксирован

Базлайн в worktree до начала волн: **юниты 843 passed / 33 skipped · e2e 32 passed**.
Гейт закрытия каждой волны: не ниже этих чисел.

---

## Дельты Wave 4/5 — файлы сверх ownership-матрицы §3 плана

> Фактический набор изменённых файлов (`git diff --stat 009d229..HEAD --name-only`) вышел за
> ownership-матрицу плана §3 / контракта §OWN. Матрица §3 в плане **не переписывается** (она —
> Wave-0-снимок распараллеливания); эта таблица амендирует её фактом до Wave 6, чтобы следующий
> этап не унаследовал ложную карту владения. Причина расширения — review-фиксы Wave 5 (10 линз,
> состязательная верификация) и точечные правки, законно затронувшие соседние файлы.

| Файл | Не был во владении | Почему затронут |
|---|---|---|
| `src/site/db.py` | § OWN | Docstring write-аллоулиста: writer обслуживает `leads`+`subscriptions`+`site_events` (c176f5a). |
| `src/site/subscriptions.py` | § OWN | Лимиты длины элементов `districts`/`rooms` в `_validate` (F4, finding [6]). |
| `src/site/cache.py` | § OWN | `TTLCache` maxsize=512 + эвикция (анти memory-DoS каталога, F4, finding [9]). |
| `templates/partials/_subscribe_form.html` | § OWN (только `_lead_form.html`) | Hidden utm-поля (W2) + вёрстка формы по токенам, синк `.on` тумблера (F3, finding [28]). |
| `templates/catalog.html` · `index.html` · `partials/_results.html` | § OWN | `h1` на контентных страницах, `outerHTML`-своп + стабильный `#results`-status, featured LCP-вариант (F3, findings [10][22][27]). |
| `templates/partials/_filters.html` | W4 frontend (не перечислен явно) | pill visually-hidden — клавиатура/SR (F3, finding [21]). |
| `templates/map.html` · `calculator.html` · `how.html` · `vnj.html` · `unsubscribe.html` | § OWN | `h1` на контентных страницах (F3, finding [27]). |
| `static/js/img-fallback.js` · `static/js/subscribe-form.js` | § OWN | C1-фоллбэк фото (делегированный capture-error) и вынос inline subscribe-radio (T4-3/T4-4, готовность снять `unsafe-inline`). |
| `src/admin/publish.py` · `admin/routes.py` · `admin/templates/{base,metrics,published}.html` | W3 (только analytics) | Pillow-хук вариантов в publish (T4-1), SRI для CDN-скриптов, AA-контраст бейджей, колонка просмотров 7д (F5, findings [2][24]; T3-2). |
| `deploy/Caddyfile` | § OWN | far-future+immutable для `/photos/*`, CSP-готовность, `hide photo_manifest.json*` (T4-3, F2). |
| `src/config.py` | § OWN | Ключи аналитики (§ENV) — миграция настроек этапа. |
| `.env.example` · `docs/*` · `scripts/restore_check.sh` | § OWN | Синхрон доков/контрактов/скриптов с кодом (Wave 5 docs-линия F7). |

Также §A.4 контракта обещал hidden-поля формам lead **и** subscribe, а §OWN числил только
`_lead_form.html` — фактически правился и `_subscribe_form.html` (устранено выше).

---

## Что нужно от Дмитрия (открыто)

1. **Deploy `go`** на Wave 6 — каждый ssh-шаг раннбука подтверждается.
2. **`go` на боевой purge-inactive** — после чтения dry-run отчёта (бэклог #3); в этапе только dry-run.
3. Cloudflare Wave 3b остаётся в бэклоге #1 (решение 02.07) — в этап не входит.
