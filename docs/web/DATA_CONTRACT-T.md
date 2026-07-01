# DATA CONTRACT — Stage T «Insight & Integrity»

> Дельта-контракт этапа T, фиксируется в Wave 0 **до** волн 1–4 — чтобы backend/CLI/admin/frontend-агенты
> работали параллельно без конфликтов. В Wave 6 вливается в `DATA_CONTRACT.md` (как §8 у W2).
> База: `DATA_CONTRACT.md` (+§8 W2, +дельта S). Решения: `DECISIONS-T.md`.
> **Одна миграция: `0010_analytics`** (alembic head 0009 → 0010).

---

## §0 — Обновление инварианта записи (осознанное, D1)

Было: «Публичный app пишет ТОЛЬКО `leads` + `subscriptions`».
Становится: «Публичный app пишет ТОЛЬКО `leads` + `subscriptions` + **`site_events`** (append-only
телеметрия, ни одного read-модифицирующего пути, отдельный scoped-writer с busy_timeout=5000)».
Все прочие инварианты (ToS, ноль площадочных/TG-CDN URL, приватность адресов) — без изменений и
**расширяются** на `/map` (§M2).

---

## §A — Аналитика

### §A.1 Схема (миграция 0010)

```
site_events
  id            INTEGER PK AUTOINCREMENT
  ts            DATETIME (UTC, индекс)
  type          TEXT      -- 'pageview' | 'lead_submit' | 'subscribe_submit'
  path          TEXT      -- нормализованный путь ('/apt/{id}' хранится как '/apt' + apt_id)
  apt_id        INTEGER NULL  -- site_listings.id для /apt и per-listing заявок
  visitor       TEXT(16)  -- sha256(daily_salt ‖ ip ‖ ua)[:16]; сырые IP/UA НЕ хранятся НИКОГДА
  referrer      TEXT NULL -- только внешний referrer, обрезка ≤200
  utm_source    TEXT NULL -- ≤100, санитайз [A-Za-z0-9._-]
  utm_medium    TEXT NULL -- ≤100
  utm_campaign  TEXT NULL -- ≤100
  is_bot        BOOLEAN DEFAULT 0
  ИНДЕКСЫ: (ts), (type, ts), (apt_id, ts)

site_stats_daily
  day     DATE
  metric  TEXT   -- см. §A.2
  key     TEXT   -- измерение метрики ('' если не применимо)
  value   INTEGER
  PK (day, metric, key)

leads (ALTER, nullable):
  utm_source TEXT NULL · utm_medium TEXT NULL · utm_campaign TEXT NULL · referrer TEXT NULL
```

`daily_salt = sha256(SITE_ANALYTICS_SALT ‖ 'YYYY-MM-DD')` — уники не связываемы между днями по построению.

### §A.2 Метрики rollup (nightly, идемпотентный upsert; пересчитывает вчера+сегодня)

| metric | key | value |
|---|---|---|
| `views_path` | путь (`/`, `/catalog`, `/apt`, `/map`, `/calculator`, `/vnj`, `/how`) | pageview, `is_bot=0` |
| `views_apt` | `apt_id` | pageview по `/apt/{id}` |
| `uniques` | `''` | `count(distinct visitor)` за день, `is_bot=0` |
| `leads` | `''` | события `lead_submit` |
| `leads_apt` | `apt_id` | `lead_submit` с `apt_id` |
| `subs` | `''` | события `subscribe_submit` |
| `src_utm` | `utm_source` | pageview с utm_source |
| `src_ref` | домен referrer | pageview с внешним referrer |
| `_rolled` | `''` | `1` — маркер «день сагрегирован» |

**Retention:** raw-события старше `SITE_ANALYTICS_RAW_RETENTION_DAYS` (дефолт 90) удаляются nightly
**только** для дней с маркером `_rolled` (гард от потери несагрегированного). Агрегаты хранятся вечно.

### §A.3 Запись событий (site)

- Хук в роутах **аллоулиста**: `/`, `/catalog`, `/apt/{id}`, `/map`, `/calculator`, `/vnj`, `/how`.
  **Скип:** статика, `/photos/*`, `/api/*`, `/healthz`, `/sw.js`, `/offline`, `*/og.png`, любые не-2xx,
  запросы с заголовком `HX-Request` (пагинация/фильтры ≠ pageview). Admin — другой app, не логируется.
- Механика: FastAPI `BackgroundTasks` → `analytics.record_event(...)` → scoped writer (busy_timeout=5000).
  **Любая ошибка глотается с warn-логом — страница никогда не 5xx-ит из-за аналитики** (тест обязателен).
- IP берётся через существующий `src/clientip.py::client_ip` (Stage S) — корректен за Caddy и за CF.
- Бот-эвристика: UA содержит любое из `bot, spider, crawl, slurp, headless, python, curl, wget, monitor,
  uptime, lighthouse, pingdom` (case-insensitive) → `is_bot=1` (пишем, агрегаты исключают).
- `lead_submit`/`subscribe_submit` пишутся **после** коммита lead/subscription, тем же request-циклом,
  некритично для ответа (сбой события не откатывает заявку).
- Kill-switch: `SITE_ANALYTICS_ENABLED=false` → все хуки no-op (ни одной записи; тест).

### §A.4 Атрибуция (frontend ↔ backend)

- `static/js/utm.js` (подключается в `base.html` **только** через Wave-4-frontend? НЕТ — см. ownership:
  скрипт подключает W2-агент строкой в существующем блоке скриптов `base.html` **запрещено**; вместо
  этого — в шаблонах форм `_lead_form.html` inline `<script src>` с `?v={{ asset_v }}`): first-touch
  `utm_*` + `document.referrer` → `sessionStorage` → hidden-поля `utm_source/utm_medium/utm_campaign/referrer`
  формы при сабмите. Без cookies.
- Серверный фоллбэк (no-JS): `POST /lead`/`/subscribe` также принимает эти опц. поля из формы; если
  пусты — backend берёт utm из query referer-страницы, если она передана; иначе NULL.
- Валидация на бэке: обрезка/санитайз по §A.1; мусор → NULL, не 400 (атрибуция best-effort).

### §A.5 Admin dashboard контракт

`GET /admin/analytics?days=7|30` (дефолт 7) → контекст:

```python
{
  "days": int,
  "cards": {"views": int, "uniques": int, "leads": int, "conv_pct": float},  # conv = leads/views*100, div0→0.0
  "series": [ {"day": date, "views": int, "leads": int}, … ],                # по дням, для inline-SVG
  "top_apts": [ {"apt_id": int, "district": str, "price_eur": int,
                 "views": int, "leads": int, "ctr_pct": float}, … ],          # ≤15, сорт по views
  "sources": [ {"kind": "utm"|"ref", "key": str, "views": int}, … ],         # ≤15
}
```

Данные: прошлые дни — из `site_stats_daily`; «сегодня» — live-подсчёт из raw (помечено в UI).
Уники за период = **сумма дневных уников** — в UI честная подпись «дневные уники, сумма». Рендер:
таблицы + inline-SVG sparkline (Jinja-цикл → `<polyline points>`), ноль JS-библиотек, токены, `--mono`.

---

## §M2 — Фуззи глобальной карты (T1-1, закрывает семя S-handoff §4)

- `readmodel`-путь `map_points` / `GET /api/map.json`: `lat/lon` каждой точки проходят
  **`fuzz_coords` из `src/site/maps.py`** (реюз Stage S, снэп ~100-м сетка) **до** сериализации.
  Кластеры считаются по снэпнутым точкам.
- `/apt/{id}` — поведение Stage S (круг 150 м + фуззи deep-links) **не меняется**.
- Политика приватности становится единой: **точные координаты не покидают БД ни через одну публичную
  поверхность** (HTML, JSON, OG, deep-links).
- e2e-гард `_assert_no_precise_coords` (сид с известной точной парой → пары нет в `/api/map.json` и в
  HTML `/map`) — добавляется в suite **постоянно**, рядом с `_assert_no_forbidden_urls`.

---

## §P — Фото-варианты (Wave 4)

### §P.1 Генерация (`src/site/photos.py`)

- Варианты: `card` — ширина 640, `detail` — ширина 1280 (высота пропорционально; апскейла нет —
  если оригинал уже, вариант = оригинал-размер), webp quality 82, `method=6`.
- Нейминг: `<orig-relpath>.card.webp`, `<orig-relpath>.detail.webp` — рядом с оригиналом (маппинг
  тривиален, коллизий нет, Caddy отдаёт как обычную статику из того же photos_root).
- Манифест `photo_manifest.json` в корне photos_root:
  `{"<orig-relpath>": {"w": int, "h": int, "card": "<relpath>", "detail": "<relpath>"}, …}`
  (w/h — размеры **оригинала**, для `width/height`-атрибутов и расчёта соотношения).
- Точки генерации: (а) publish-action админки (для новых публикаций), (б) CLI `otz photos-backfill`
  (`--dry-run`, `--limit N`, идемпотентен — скипает существующие, битые файлы скип+лог).
- ⚠️ **Derived data:** варианты+манифест живут на VPS, в git/rsync не участвуют (`data/` исключена).
  Раннбук Wave 6 гоняет backfill на сервере.

### §P.2 Шаблоны (Jinja-фильтр)

```python
photo_v(src: str, kind: str = "card") -> str
# src = публичный путь из SiteListingView.photos ('/photos/…')
# есть в манифесте → путь варианта; нет манифеста/записи/файла → src (фоллбэк, прод не ломается)

photo_dims(src: str) -> tuple[int, int] | None   # (w, h) оригинала из манифеста
```

- Манифест читается лениво + кэш в памяти с mtime-инвалидацией (backfill на живом сервере подхватится
  без рестарта; допустимая задержка — минуты).
- `_card.html`: `<img src="{{ p|photo_v('card') }}" srcset="… 640w" sizes="(max-width:700px) 100vw, 33vw"
  loading="lazy" decoding="async" width=… height=…>`; `apt.html`-галерея — `detail` + клик на оригинал.
- `SiteListingView.photos` **не меняется** (list[str] публичных оригиналов) — обратная совместимость,
  вся вариантность в фильтре.

---

## §C — CLI-контракты (Wave 1)

- `_heal_urls`: дефолт — только `is_active=1` **и** canonical резолвится (валидный ответ) перед записью;
  нерезолвящийся → скип + warn. Флаг `--include-inactive` возвращает старый охват.
- `otz purge-inactive --older-than N [--dry-run]`:
  - dry-run: полный отчёт (кол-во по источникам/датам, объём, сколько защищено FK), **ноль мутаций**;
  - боевой: архив `data/archive/listings-YYYYMMDD.jsonl.gz` (полные строки, roundtrip-читаемый) → delete;
  - **отказ** удалять строки, на которые ссылаются `matches`/`site_listings` (перечислить в отчёте);
  - Stage T исполняет **только dry-run** (D5).
- `otz stats`: total → разбивка `active / inactive` (+ прежние счётчики).
- `otz photos-backfill` — см. §P.1 (владелец Wave 4, здесь для полноты CLI-поверхности).

---

## §N — Nightly (точка подвеса из T0-2)

Порядок джобов: `analytics.rollup(вчера, сегодня)` → `analytics.retention()` → `dupes-report`
(артефакт `data/reports/dupes-YYYY-MM-DD.txt`, report-only). Каждый джоб изолирован (сбой одного не
валит остальные, warn-лог). Если в проекте нет пригодного раннера — systemd `otz-nightly.timer`+`.service`
(юниты в `deploy/`, User=otz, 04:30 Europe/Belgrade).

---

## §ENV — новые ключи (см. `env.additions-t.txt`)

```
SITE_ANALYTICS_ENABLED=true
SITE_ANALYTICS_SALT=            # openssl rand -hex 32; ТОЛЬКО серверный .env, не гит
SITE_ANALYTICS_RAW_RETENTION_DAYS=90
```

---

## §OWN — Ownership-матрица (копия из плана §3 — единственный источник: план)

| Агент | Пишет | Не трогает |
|---|---|---|
| W1 backend | `readmodel.py` (map_points), `tests/site/test_map_privacy.py` | routes, шаблоны, maps.py |
| W1 cli | `src/cli.py` (heal/purge/stats), nightly, `tests/test_cli_*` | `src/site/*` |
| W2 backend | `0010_*`, models (новое), `src/site/analytics.py`, `routes.py` (хуки), `leads.py` (utm), `static/js/utm.js`, `_lead_form.html` (hidden), тесты | admin, публичный CSS, `_card.html`/`apt.html`, `base.html` |
| W3 admin | `src/admin/*` (роут+шаблон analytics), admin-CSS | `src/site/*` |
| W4 backend | `src/site/photos.py`, publish-hook, `cli.py` (backfill), тесты | шаблоны |
| W4 frontend | `_card.html`, `apt.html`, фильтры Jinja (регистрация в `app.py`), `static/js/filters.js`, css (минимум) | `base.html`, admin |

`base.html` — **никто** (эскалация кондуктору). Конфликт `cli.py`/`app.py` разведён очерёдностью волн.
