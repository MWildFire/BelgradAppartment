# DATA CONTRACT — Публичный каталог

> Контракт между backend-агентом и frontend-агентом Stage W. Зафиксирован **до** Wave 3,
> чтобы оба работали параллельно без конфликтов. Backend владеет `src/site/*.py`,
> frontend владеет `src/site/templates/*` + `static/*`. Контракт ниже — единственная точка их стыка.

## 1. Read-model: `SiteListingView`

То, что бэкенд кладёт в контекст шаблонов. Не ORM-модель — плоский dataclass/dict, собранный джойном `site_listings × matches × listings (× inbound_posts для fallback-текста)`.

```python
@dataclass(frozen=True)
class SiteListingView:
    id: int                 # = site_listings.id (стабильный публичный id для URL)
    price_eur: int          # listings.price_eur
    area_m2: float | None   # listings.area_m2
    rooms: str              # "1+1" — форматировано из listings.rooms (см. §5)
    floor: str | None       # если есть в данных, иначе None
    district: str           # человекочитаемый район (см. §5 — НЕ raw)
    address: str | None     # listings.address_raw, если есть
    lat: float | None
    lon: float | None
    for_vnj: bool           # site_listings.is_for_vnj
    for_white_card: bool    # site_listings.is_for_white_card
    is_rented: bool         # статус «снято» (см. §5); показываем приглушённо, не прячем
    photos: list[str]       # ПУБЛИЧНЫЕ URL фото (см. §4) — уже готовые пути
    note: str | None        # site_listings.note (override Дарьи)
    channel_url: str        # site_listings.channel_post_url ИЛИ дефолт t.me/RentBelgrad
    posted_at: str          # ISO, для сортировки/отображения
```

`SiteListingView.photos` — **уже** публичные пути вида `/photos/<...>.webp`; шаблон их не преобразует, только вставляет в `<img>`. Преобразование локальный файл → публичный URL делает бэкенд (§4).

## 2. Карта роут → шаблон → контекст

| Метод/роут | Шаблон | Ключи контекста | Кэш |
|---|---|---|---|
| `GET /` | `index.html` | `featured: list[SiteListingView]` (N свежих, public), `stats: {active:int, districts:int, ...}` | TTL 5 мин |
| `GET /catalog` | `catalog.html` | `items: list[SiteListingView]`, `facets: Facets` (см. §3), `applied: AppliedFilters`, `total:int` | TTL 5 мин (ключ = нормализованные query-параметры) |
| `GET /catalog` + `HX-Request` | `partials/_results.html` | то же, что выше, но только грид + пагинация | TTL 5 мин |
| `GET /apt/{id}` | `apt.html` | `item: SiteListingView`, `similar: list[SiteListingView]` (≤4, тот же район) | TTL 5 мин; 404 если не public |
| `GET /map` | `map.html` | пусто (данные тянет JS из `/api/map.json`) | — |
| `GET /api/map.json` | — (JSON) | `{points:[{id,lat,lon,price_eur,rooms,district,for_vnj,url}]}` только public+coords | TTL 5 мин |
| `GET /calculator` | `calculator.html` | пусто (чистый клиентский расчёт) | static |
| `POST /lead` | редирект/partial `partials/_lead_ok.html` | принимает форму (§6), пишет `leads`, шлёт нотис Дарье | без кэша, rate-limit |
| `GET /healthz` | — | `{"status":"ok"}` | без кэша |

Бэкенд гарантирует имена шаблонов и ключи контекста из этой таблицы. Frontend гарантирует, что шаблоны существуют под этими именами и читают эти ключи. **Менять таблицу — только синхронно, обновив этот файл.**

## 3. Фильтры каталога

Query-параметры `GET /catalog` (все опциональны, любые комбинируются):

| Параметр | Тип | Семантика |
|---|---|---|
| `vnj` | `1`/отсутствует | только `for_vnj=true` |
| `wc` | `1`/отсутствует | только `for_white_card=true` (белый картон) |
| `district` | str (мультизначный) | один или несколько человекочитаемых районов |
| `price_min`,`price_max` | int (EUR) | диапазон цены |
| `rooms` | str (мультизначный) | `studio`,`1`,`1.5`,`2`,`3+` (маппинг на `listings.rooms`) |
| `sort` | enum | `new` (default), `price_asc`, `price_desc` |
| `page` | int | пагинация, размер страницы фикс в конфиге (`SITE_PAGE_SIZE`) |
| `show_rented` | `1`/отсутствует | по умолчанию rented приглушены, но видны; флаг оставлен на будущее |

`Facets` (для рендера панели фильтров с числами):
```python
@dataclass
class Facets:
    districts: list[tuple[str, int]]   # (район, кол-во public-листингов) по убыванию
    price_bounds: tuple[int, int]      # min/max среди public
    rooms: list[tuple[str, int]]       # (метка комнат, кол-во)
    counts: dict[str, int]             # {"vnj": N, "white_card": M, "total": T}
```

**ВНЖ и белый картон — главные фильтры, на видном месте** (требование дизайна, раздел 0 design-плана). Это первое, что видит релокант.

## 4. Фото: локальный файл → публичный URL

- На VPS фото лежат локально (скачаны пайплайном). Бэкенд НЕ отдаёт их через Python; их раздаёт Caddy `file_server` из публичной директории `/<photos_root>/`.
- Wave 3 backend реализует функцию `public_photo_url(local_path) -> str`, которая: (а) гарантирует, что файл доступен под `photos_root` (симлинк/копия при публикации в Wave 2, либо `photos_root` указывает на существующую директорию фото с безопасным префиксом), (б) возвращает `"/photos/" + relpath`.
- Формат: при публикации (Wave 2 admin-action) можно сгенерировать webp-превью; на MVP допустимо отдавать оригинал. Размер/формат — не блокер MVP, оптимизация в Wave 5.
- **Никаких** прямых TG-CDN или площадочных URL в публичной выдаче (протухают + ToS).

## 5. Нормализация для показа

- `rooms`: `listings.rooms` (float) → метка. `0/студия → "студия"`, `1 → "1+1"`, `1.5 → "1.5+1"`, `2 → "2+1"`, … По конвенции дизайна комнаты показываются как `N+1`. Точный маппинг — в `src/site/format.py`, покрыт тестами.
- `district`: `listings.district` — raw с площадки (напр. `Vracar Centar`). Привести к человекочитаемому кириллическому через существующий `data/districts.yaml` (он уже маппит район→хэштег; переиспользовать тот же словарь для отображения, напр. `Врачар`). Незамапленные — показывать как есть, лог в `districts-report` (этап H1 уже даёт такую команду).
- `is_rented`: вывести из статуса (`listings.is_active=false` ИЛИ метка «снято» в исходном тексте). Показывать карточку приглушённо с бейджем «снято», **не удалять** — это соц.доказательство (требование дизайн-плана DoD).
- Цена/площадь/комнаты — **всегда моноширинным** (`--mono`, JetBrains Mono) по дизайну.

## 6. Форма заявки → `leads`

`POST /lead` принимает (form-encoded):

| Поле | Обяз. | Валидация |
|---|---|---|
| `name` | да | 1..120 символов |
| `contact` | да | telegram/телефон/email — 1..200, без строгой схемы (Дарья свяжется) |
| `budget_eur` | нет | int ≥ 0 |
| `districts` | нет | строка |
| `rooms` | нет | строка |
| `message` | нет | ≤ 2000 |
| `csrf_token` | да | как в админке (паттерн H1 W3-F2): поле формы `csrf_token` ИЛИ заголовок `X-CSRF-Token` (НЕ `_csrf`) |
| `company` (honeypot) | — | скрытое поле имени `company`; если заполнено → тихо 200, не писать (анти-бот) |

При успехе: insert в `leads` (scoped writer, busy_timeout), затем нотис Дарье через `send_text_to_recipients` («📩 Новая заявка с сайта: {name}, бюджет {budget}€, {districts}»). Ответ: `partials/_lead_ok.html` (HX-swap) или редирект на `/?lead=ok`. Rate-limit по IP (память/таблица) — защита от спама.

## 7. Что frontend НЕ делает

- Не ходит в БД, не знает SQL, не импортирует ORM.
- Не строит фото-URL (получает готовые из `SiteListingView.photos`).
- Не решает, что public (бэкенд уже отфильтровал по `is_public`).
- Любая динамика на клиенте (фильтры-HTMX, карта, калькулятор, избранное, валидация формы) ест только данные из таблицы §2 и эндпоинт `/api/map.json`.

## 8. Дельта Stage W2 «Conversion & Polish» (применена 21.06.2026)

> Влито из `DATA_CONTRACT-W2.md`. Контракт backend↔frontend для конверсионной доводки (этап W2).

### 8.1 Изменённые/новые роуты
- `POST /lead` — принимает опц. `site_listing_id: int` (валидируется: существует и `is_public`) и `kind`
  (`general`/`listing`/`subscription`); при публичной квартире письмо/нотис включают её сводку. Канал
  доставки: **e-mail на `SITE_LEAD_EMAIL_TO` (обяз.)** + бот-нотис. Honeypot (`company`)/CSRF/rate-limit — без изменений.
- `GET /` — контекст пополнен: `district_options` (hero-дропдаун), `featured: SiteListingView | None` («квартира недели»).
- `GET /vnj`, `GET /how` — новые контентные страницы (`templates/vnj.html`, `how.html`; на `/how` — разбор комиссии 50%).
- `GET /sitemap.xml` — статические страницы + все `is_public` `/apt/{id}` (абсолютные URL).
- `GET /robots.txt` — allow + ссылка на sitemap.
- 404 — кастомный handler → `templates/404.html` (Telegram-CTA + ссылка в каталог).
- `GET/POST /admin/leads` — админка заявок (список новых сверху, фильтр по статусу, смена `status` POST с CSRF).

### 8.2 Новые Jinja-globals
- `telegram_url` = `SITE_TELEGRAM_URL` (=`https://t.me/Darya_kralj`) — кнопка «Написать в Telegram» (шапка/футер/липкая), F5.
- `district_options` — курированный Cyrillic-список районов (`data/districts.yaml` ∪ фасеты), дропдаун района в hero и фильтрах, F2.

### 8.3 Read-model / схема (миграция 0007)
- `site_listings.is_featured: bool` («квартира недели», admin-тоггл) + частичный индекс.
- `leads.site_listing_id: int | None` (FK ON DELETE SET NULL), `leads.kind: str` (`general`/`listing`/`subscription`).
- `readmodel.featured_one() -> SiteListingView | None` (featured → newest public → None);
  `readmodel.apartment_summary(site_listing_id) -> dict | None` (сводка для письма/нотиса).

### 8.4 OpenGraph на `/apt/{id}`
`og:title` = «{price}€ · {district}», `og:description`, `og:image` = первое фото (абсолютный URL через
`SITE_PUBLIC_BASE_URL`), `og:url` = canonical — богатые превью при шеринге ссылки в Telegram.

### 8.5 Новые env
`SITE_TELEGRAM_URL`, `SITE_LEAD_EMAIL_TO`, `SITE_SMTP_HOST/PORT/USER/PASSWORD/FROM/STARTTLS`. E-mail-релей =
**Resend** (`smtp.resend.com:587` STARTTLS, `FROM=no-reply@rentbelgrad.com`, домен `rentbelgrad.com` верифицирован:
DKIM/SPF/DMARC). Креды (API-ключ `re_…`) — в серверном `/opt/otz/.env` руками, никогда не в коде/логах/гите.

## 9. Дельта Stage T «Insight & Integrity» (🚀 задеплоено на прод 02.07.2026, smoke зелёный)

> Влито из `DATA_CONTRACT-T.md` (оригинал оставлен на месте). Финальная семантика — **после фиксов Wave 5**
> (fail-closed соль, `_rolled` только финальным дням, манифест в родителе `photos_root`). Одна миграция: **0010**.
> ⚠️ Фактический путь дашборда — **`/analytics`** (admin.rentbelgrad.com/analytics); в §9.A он назван
> `/admin/analytics` как логический адрес, но в admin-app префикса `/admin` нет (как и у `/metrics`, `/published`).

### 9.0 Расширение инварианта записи (D1)
Публичный app пишет ТОЛЬКО `leads` + `subscriptions` + **`site_events`** (append-only телеметрия, ни одного
read-модифицирующего пути; тот же scoped-writer, `busy_timeout=5000`). Прочие инварианты (ToS, ноль
площадочных/TG-CDN URL, приватность адресов) — без изменений и **расширяются на `/map`** (§9.M2).

### 9.A Аналитика

**Схема (миграция 0010):**
- `site_events(id PK, ts DATETIME UTC, type TEXT[pageview|lead_submit|subscribe_submit], path TEXT,
  apt_id INT NULL, visitor TEXT(16), referrer TEXT NULL≤200, utm_source/medium/campaign TEXT NULL≤100
  санитайз `[A-Za-z0-9._-]`, is_bot BOOL)`; индексы `(ts)`, `(type,ts)`, `(apt_id,ts)`.
- `site_stats_daily(day DATE, metric TEXT, key TEXT[''=n/a], value INT, PK(day,metric,key))`.
- `leads` ALTER (nullable): `utm_source · utm_medium · utm_campaign · referrer`.
- `daily_salt = sha256(SITE_ANALYTICS_SALT‖'YYYY-MM-DD')`; `visitor = sha256(daily_salt‖ip‖ua)[:16]` —
  **сырые IP/UA не хранятся никогда**, уники не связываемы между днями по построению.

**Метрики rollup** (`views_path`, `views_apt`, `uniques`=`count(distinct visitor)`, `leads`, `leads_apt`,
`subs`, `src_utm`, `src_ref`; маркер `_rolled`). Агрегаты `is_bot=0`.

**Запись событий (§A.3):** хук в 7 GET-роутах аллоулиста (`/`, `/catalog`, `/apt/{id}`, `/map`,
`/calculator`, `/vnj`, `/how`) через `BackgroundTasks → analytics.record_event`. **Скип:** статика,
`/photos/*`, `/api/*`, `/healthz`, `/sw.js`, `/offline`, `*/og.png`, любые не-2xx, `HX-Request`. IP через
`src/clientip.py::client_ip` (за Caddy/CF). Бот-эвристика по UA-подстрокам → `is_bot`.
**Fail-closed:** пустая/незаданная `SITE_ANALYTICS_SALT` при enabled → события **не пишутся** + один warn.
Kill-switch `SITE_ANALYTICS_ENABLED=false` → все хуки no-op. **Любая ошибка глотается с warn — страница
никогда не 5xx-ит из-за аналитики** (тест обязателен). `lead_submit`/`subscribe_submit` — **после** коммита
lead/subscription (сбой события не откатывает заявку).

**Rollup/retention (nightly, §N):** идемпотентный upsert, пересчёт вчера+сегодня + **догон `unrolled_days()`**
(несагрегированный день не выпадает). ⚠️ **`_rolled` ставится только дням `< today`** (партиал-роллап
«сегодня» без маркера — финализируется на следующую ночь). Retention: raw старше
`SITE_ANALYTICS_RAW_RETENTION_DAYS` (90) удаляется **только** для дней с `_rolled` (гард от потери
несагрегированного); агрегаты — вечно. Crafted `Referer` → `_ref_domain` с `ValueError`-гардом (день не
«застревает» на роллапе).

**Атрибуция (§A.4):** `static/js/utm.js` — first-touch `utm_*`+`document.referrer` → `sessionStorage` →
hidden-поля форм lead **и** subscribe при сабмите (без cookies). Серверный фоллбэк (no-JS): `POST /lead`/
`/subscribe` принимают опц. поля; пустые → utm из query same-page или NULL. Валидация best-effort
(обрезка/санитайз по схеме; мусор → NULL, не 400).

**Admin dashboard (§A.5):** `GET /admin/analytics?days=7|30` (дефолт 7; окна `(7, 30)`) → карточки
(`views/uniques/leads/conv_pct`, `conv=leads/views*100`, div0→0.0), `series` по дням (inline-SVG polyline),
`top_apts` (≤15, сорт по views, с `ctr_pct`), `sources` (utm/ref, ≤15). Прошлые дни — из `site_stats_daily`,
«сегодня» — live-подсчёт из raw (помечено в UI). Уники за период = **сумма дневных** (честная подпись).
Ноль JS-библиотек, токены Belgrade Editorial, SRI CDN-скриптов, AA-контраст.

### 9.M2 Фуззи глобальной карты (T1-1)
`readmodel.map_points` / `GET /api/map.json`: `lat/lon` каждой точки проходят **`fuzz_coords` из
`src/site/maps.py`** (реюз Stage S, снэп ~100 м) **до** сериализации; кластеры считаются по снэпнутым точкам.
`/apt/{id}` (круг 150 м + фуззи deep-links Stage S) не меняется. Политика единая: **точные координаты не
покидают БД ни через одну публичную поверхность** (HTML/JSON/OG/deep-links). Постоянный e2e-гард
`_assert_no_precise_coords` — рядом с `_assert_no_forbidden_urls`.

### 9.P Фото-варианты (Wave 4)
- Варианты: `card`=640w, `detail`=1280w (высота пропорционально, апскейла нет), webp `quality=82 method=6`;
  нейминг `<orig-relpath>.card.webp`/`.detail.webp` рядом с оригиналом.
- ⚠️ **Манифест `photo_manifest.json` — в РОДИТЕЛЕ `photos_root`** (вне публичной раздачи `/photos`, скрыт
  Caddy `hide photo_manifest.json*`): `{"<orig-relpath>": {"w","h","card","detail"}, …}` (w/h — оригинала,
  для `width/height`/соотношения). Индекс всего inbound-корпуса наружу не светится.
- `process_photo`: EXIF-transpose (правильная ориентация + w/h) + режимы RGBA/LA/P/CMYK. Мердж манифеста —
  кросс-процессный `fcntl.flock` + уникальный tmp (конкурентные publish-хуки/backfill не теряют записи).
- Точки генерации: (а) publish-hook админки, (б) `otz photos-backfill` (`--dry-run`, `--limit N`, идемпотентен,
  битые файлы скип+лог, **инкрементальный флаш каждые 100**).
- Jinja: `photo_v(src, kind='card') -> str` (в манифесте → путь варианта; нет манифеста/записи → src,
  прод не ломается) и `photo_dims(src) -> (w,h)|None`. Манифест кэшируется в памяти с mtime-инвалидацией.
  `_card.html`/`apt.html`-галерея — `<img>`+`srcset`/`sizes`+`width/height`+`loading=lazy decoding=async`.
  `SiteListingView.photos` не меняется (list[str] оригиналов — обратная совместимость).
- ⚠️ **Derived data:** варианты+манифест живут на VPS, в git/rsync не участвуют (`data/` исключена);
  раннбук Wave 6 гоняет backfill на сервере (манифест на проде = `/opt/otz/data/photo_manifest.json`).

### 9.C CLI (Wave 1)
- `_heal_urls`: дефолт — только `is_active=1` **и** canonical резолвится перед записью (нерезолвящийся →
  скип+warn); флаг `--include-inactive` возвращает старый охват.
- `otz purge-inactive --older-than N [--dry-run]`: dry-run — полный отчёт (по источникам/датам/объёму +
  сколько защищено FK), ноль мутаций; боевой — архив `data/archive/listings-YYYYMMDD.jsonl.gz` (roundtrip-
  читаемый; ⚠️ `photo_embeddings` в архиве **обнуляются намеренно**) → delete с TOCTOU-гардом; **отказ**
  удалять строки со ссылками из `matches`/`site_listings`. **Stage T исполняет только dry-run** (D5).
- `otz stats`: разбивка `active / inactive` (+ прежние счётчики).
- `otz photos-backfill` — см. §9.P.

### 9.N Nightly
Новые fault-isolated шаги подвешены в существующий `otz-nightly-refresh` (`_run_nightly_steps`), **без нового
timer'а**: `analytics` (догон `unrolled_days()` → `rollup(сегодня)` → `retention()`; kill-switch глушит шаг
целиком) и `dupes-report` (артефакт `data/reports/dupes-YYYY-MM-DD.txt`, report-only). Сбой шага не валит
остальные (🚨-строка в ТГ-summary).

### 9.ENV Новые ключи (`env.additions-t.txt`)
`SITE_ANALYTICS_ENABLED=true` · `SITE_ANALYTICS_SALT=` (⚠️ пустая при enabled → fail-closed no-op; генерить
на сервере `openssl rand -hex 32`, только `/opt/otz/.env`, не гит) · `SITE_ANALYTICS_RAW_RETENTION_DAYS=90`.
