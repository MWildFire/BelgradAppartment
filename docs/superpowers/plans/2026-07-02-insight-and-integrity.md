# План этапа T «Insight & Integrity» — аналитика · приватность карты · фото-пайплайн · большое ревью

> Этап: **Stage T** · Дата: 2026-07-02 · Ветка: `feature/insight-and-integrity`
> База: локальный `main` @ `8626e1a` (Stage S смёржен; код на проде с ~24.06, стабилен ~неделю).
> Методология: **obra/superpowers по максимуму** — карта скиллов в §1. TDD везде. **Ноль новых зависимостей.
> Ровно одна миграция (0010).** Контракт этапа: `docs/web/DATA_CONTRACT-T.md` (зафиксирован ДО волн).
> Решения: `docs/web/DECISIONS-T.md` (создаётся в Wave 0 из таблицы §4).
> **Cloudflare Wave 3b в этот этап НЕ входит** — вынесен в `docs/BACKLOG.md` #1 (решение Дмитрия, 02.07).

---

## 0. Зачем этот этап (TL;DR)

Витрина (S) и конверсионный контур (W2) готовы и стабильны на проде. Четыре самых больших рычага сейчас:

1. **Мы летим вслепую.** Ноль аналитики: не видно ни трафика, ни конверсии заявок, ни источников, ни того,
   какие квартиры смотрят. Конверсионные фичи W2 нечем измерять. → Первопартийная server-side аналитика
   без cookies/баннеров/внешних сервисов + дашборд в админке для Дарьи и Дмитрия.
2. **Дыра приватности** (прямое семя S-handoff §4): глобальная `/map` и `/api/map.json` до сих пор отдают
   **точные координаты зданий**, хотя `/apt/{id}` после Stage S фуззит их (D1). Любой открывает JSON и
   получает адрес дома. → Снэп тем же `fuzz_coords`.
3. **Фото — главный тормоз.** Карточки и галерея отдают оригиналы (мегабайты) → LCP на мобиле страдает,
   а мобильный трафик для ТГ-аудитории основной. → webp-варианты + `srcset` + манифест размеров (CLS→0).
4. **Гигиена данных** (семена S-handoff §4): ~8442 неактивных «трупа» видны в total, `_heal_urls` без
   гарда, дедуп-отчёт не в nightly. → Гард + purge с архивом + report-only wiring.

Плюс **обязательная часть этапа — большое кодовое ревью всего репозитория** (Wave 5, 10 линз,
состязательная верификация — как S Wave 4, но охват шире диффа).

### Не-цели (фиксируем, чтобы не расползтись)
- **Cloudflare** (origin-lock, `TRUST_CLOUDFLARE=1`) — бэклог #1, repo уже готов.
- **EN-локаль** — бэклог (D5 Stage S остаётся в силе).
- **Внешние аналитики** (GA/Plausible/Umami) — отклонено архитектурно (§4 D1): второй рантайм против ADR,
  адблоки, cookie-баннер.
- **Авто-мерж дублей** — только report-only; курация остаётся за Дарьей.
- **Физическое удаление трупов в этом этапе** — только dry-run отчёт + инструмент; исполнение по
  отдельному `go` Дмитрия (D5 ниже).

---

## 1. Superpowers skills map (использовать по максимуму)

| Момент | Skill | Как применяем |
|---|---|---|
| Старт | `using-git-worktrees` / EnterWorktree | Worktree `feature/insight-and-integrity` **от локального `main`** (`8626e1a`+). ⚠️ Сначала `git fetch && git log origin/main..main` — если origin отстаёт (болячка W2/S), `git push origin main` ДО создания worktree. |
| Wave 0 | `brainstorming` | Прогнать таблицу §4 с Дмитрием, зафиксировать → `docs/web/DECISIONS-T.md`. Дефолты применяются без блокировки, спорное — вопросом. |
| План | `writing-plans` | Этот файл. Изменения плана — только через кондуктора, с правкой файла (не «по-тихому»). |
| Исполнение | `executing-plans` + Conductor | Волны последовательны; задачи внутри волны — параллельные субагенты по ownership-таблице §3. |
| Каждая задача | `subagent-driven-development` | Свежий implementer на задачу. Вход: секция задачи + релевантный § из `DATA_CONTRACT-T.md`. После — **два состязательных ревью-субагента**: spec-compliance (сверка с контрактом) и code-quality. Только потом коммит. |
| Код | `test-driven-development` | Red → green → refactor. Коммит невозможен без нового красного-ставшего-зелёным теста. |
| Затык | `systematic-debugging` | 4 фазы, «чинить наугад» запрещено. Флаки e2e — тоже через этот скилл. |
| Wave 5 | `requesting-code-review` + `receiving-code-review` | 10 параллельных read-only линз → `REVIEW_FINDINGS.md` → состязательная верификация → триаж → TDD-фиксы → re-review → вердикт. |
| Визуал | `taste` + `impeccable` | Обязательное ревью дашборда (T3-1) и `<picture>`-вёрстки (T4-2) — анти-slop закон Belgrade Editorial в силе. |
| Финал | `finishing-a-development-branch` | Merge ff → `main`, **push origin**, снос worktree. |

**Коммиты:** `stage-t: <wave>/<task> — <кратко>`, атомарно (задача = коммит; фиксы ревью — отдельными).
**Гейт закрытия волны:** `.venv/bin/python -m pytest tests/ -q` (пол: 832 passed) +
`RUN_E2E=1 … tests/e2e -q` (пол: 32 passed). Следующая волна не стартует, пока гейт не зелёный.

---

## 2. Волны и задачи

### Wave 0 — Решения + диагностика (последовательно, кондуктор)

- **T0-1 · Решения.** Таблица §4 → `docs/web/DECISIONS-T.md` (формат как `DECISIONS-S.md`: решение,
  заметки, дельты «реалии-vs-план»). Блокирующих вопросов у этапа нет — все дефолты применимы.
- **T0-2 · Диагностика (read-only, результаты записать в DECISIONS-T §T0-2):**
  1. Подтвердить утечку: выкачать `/api/map.json` с прода, сверить 3–5 точек с `listings.lat/lon` — точные?
  2. Вес фото: p50/p95/суммарный объём под `SITE_PHOTOS_ROOT` (ssh read-only `find … -printf '%s\n'`).
  3. Точное число inactive-строк + сколько из них имеют FK-ссылки из `matches`/`site_listings`.
  4. Найти механизм nightly (systemd timer? внутренний scheduler `src/jobs/`?) — точка подвеса для T1-4/T2-4.
     Если nightly-раннера для site-задач нет — решение: отдельный `otz-nightly.timer` (юнит в `deploy/`).
  5. Baseline Lighthouse mobile для `/` и `/apt/{seed}` — записать LCP/CLS/Perf score (для дельты в Wave 6).
  6. Git-санитария: `origin/main` vs `main`; протолкнуть при расхождении.
  7. Зафиксировать текущие счётчики suite (832/32) как пол этапа.

### Wave 1 — Privacy & Hygiene (backend ∥ CLI; файлы не пересекаются, см. §3)

| ID | Задача | Суть | TDD |
|---|---|---|---|
| **T1-1** | Фуззи `/api/map.json` | Координаты точек карты проходят `fuzz_coords` (реюз из `src/site/maps.py`, снэп ~100 м) **до** сериализации в read-model (`map_points`). Кластеры строятся по снэпнутым точкам. `/apt/{id}` (поведение Stage S) не трогается. Контракт: `DATA_CONTRACT-T.md §M2`. | Юнит: снэп применён, детерминирован, `/apt`-путь без регрессии. E2e: сид с известной точной парой → её **нет** в JSON; новый постоянный гард `_assert_no_precise_coords` остаётся в suite навсегда. |
| **T1-2** | Гард `_heal_urls` | `src/cli.py::_heal_urls` (≈:2725): обрабатывать только `is_active=1` + canonical-resolves (валидный ответ) перед записью; нерезолвящийся — скип + лог. Escape-hatch `--include-inactive`. | Юнит с мокнутым резолвером: inactive не трогаются; битый canonical — скип; флаг включает старое поведение. |
| **T1-3** | `otz purge-inactive` + честный `stats` | Новая команда: `--older-than N` дней, `--dry-run` (полный отчёт по источникам/датам/объёму), реальный прогон = архив `data/archive/listings-YYYYMMDD.jsonl.gz` (полные строки) → delete. **Отказ** для строк с FK из `matches`/`site_listings`. `otz stats`: разбивка active/inactive вместо раздутого total. **В этом этапе исполняется только dry-run** (D5). | Dry-run не мутирует БД (снапшот-сравнение); FK-защита; архив читается обратно (roundtrip); stats-разбивка. |
| **T1-4** | Дедуп в nightly (report-only) | `dupes-report` подвешивается в nightly (точка из T0-2): артефакт `data/reports/dupes-YYYY-MM-DD.txt` + счётчик в лог. **Без авто-мержа** (D6) — курация Дарьи. | Джоб идемпотентен, пишет отчёт, пустой кейс не падает. |

### Wave 2 — Analytics core (backend)

**Единственная миграция этапа — `0010_analytics`:** таблицы `site_events`, `site_stats_daily`;
колонки `leads.utm_source/utm_medium/utm_campaign/referrer` (nullable). Схема/типы событий/хэширование —
**жёстко по `DATA_CONTRACT-T.md §A`** (менять только синхронно с контрактом).

| ID | Задача | Суть | TDD |
|---|---|---|---|
| **T2-1** | Миграция 0010 + модели | Alembic (sqlite batch mode как 0006–0009), модели SQLAlchemy, индексы по §A. Downgrade полноценный. | Up/down roundtrip на пустой и засеянной БД. |
| **T2-2** | Логгер событий | `src/site/analytics.py::record_event(...)` + хук в HTML-роуты **аллоулиста** (§A.3) через FastAPI `BackgroundTasks`. `visitor = sha256(daily_salt‖ip‖ua)[:16]`, daily_salt = sha256(`SITE_ANALYTICS_SALT`‖YYYY-MM-DD); **сырые IP/UA не пишутся никогда**. Бот-эвристика по UA-подстрокам → `is_bot`. HX-Request и не-2xx — скип. Kill-switch `SITE_ANALYTICS_ENABLED`. Клиентский IP — через существующий `src/clientip.py::client_ip` (Stage S). | Ключевой тест: **сбой писателя аналитики ≠ 5xx страницы** (monkeypatch raise → страница 200). Плюс: флаг off → ноль записей; бот помечен; HX скипнут; хэш стабилен в сутках и меняется на следующий день. |
| **T2-3** | Атрибуция заявок | `static/js/utm.js`: first-touch `utm_*`+`referrer` → sessionStorage → hidden-поля форм lead/subscribe при сабмите. Серверный фоллбэк без JS: utm из query текущей страницы. Значения → `leads.utm_*`/`referrer` + событие `lead_submit` (после коммита lead, некритично для ответа). Валидация длин по §A.1. | Юнит: парс/обрезка/санитайз utm; lead сохраняет атрибуцию; событие пишется после коммита. E2e: сабмит с `?utm_source=tg` → utm в БД заявки. |
| **T2-4** | Rollup + retention (nightly) | `analytics.rollup(day)`: агрегаты в `site_stats_daily` (метрики §A.2), **идемпотентный upsert**, пересчёт «вчера+сегодня». Retention: delete raw старше `SITE_ANALYTICS_RAW_RETENTION_DAYS` (дефолт 90) **только** для дней с маркером `_rolled` (гард от потери несагрегированного). Подвес в nightly из T0-2. | Корректность агрегатов на фикстуре (руками посчитанные значения); идемпотентность (двойной прогон = те же числа); retention не трогает несролапленные дни. |

### Wave 3 — Admin dashboard (frontend admin; стартует после T2-1, ∥ остальному Wave 2)

| ID | Задача | Суть |
|---|---|---|
| **T3-1** | `GET /admin/analytics` | Страница в админке (basic-auth уже есть; read-only GET, CSRF не нужен). Контекст — по `DATA_CONTRACT-T.md §A.4`: карточки 7д/30д (views, uniques*, leads, conversion %), ряд по дням, топ-квартиры (views → leads → CTR, ссылка на публичный `/apt/{id}`), источники (utm_source/referrer). **Спарклайны — inline-SVG из Jinja** (polyline по series), ноль JS-библиотек. Стиль — токены Belgrade Editorial, цифры `--mono`, анти-slop. *Уники = сумма дневных уников (честная подпись в UI). Обязательное ревью `taste`+`impeccable`. |
| **T3-2** | *(stretch)* Views в списке листингов | Колонка «👁 7д» в admin-списке публичных листингов (join на агрегаты). Режется первой при нехватке бюджета волны. |

### Wave 4 — Photo pipeline (backend + публичные шаблоны)

| ID | Задача | Суть | TDD |
|---|---|---|---|
| **T4-1** | Генератор вариантов + манифест | `src/site/photos.py`: webp-варианты `card` (640w) и `detail` (1280w), quality 82, нейминг `<orig>.card.webp` рядом с оригиналом; `photo_manifest.json` в **РОДИТЕЛЕ** photos_root (`relpath → {w, h, card, detail}`). Генерация: (а) hook в publish-action админки, (б) `otz photos-backfill` — идемпотентный (скип существующих), `--dry-run`, `--limit`, прогресс. ⚠️ Варианты и манифест — **derived data на VPS**: в git не попадают, `deploy_sync` их не возит (исключает `data/`) → в раннбуке Wave 6 backfill гоняется **на сервере**. Pillow уже прямая зависимость. | Вариант ≤ оригинала по весу и с точным именованием; идемпотентность; dry-run без записи; манифест-roundtrip; битое фото — скип+лог, не падение. |
| **T4-2** | `<picture>`/srcset в шаблонах | Jinja-фильтр `photo_v(src, kind)` (contract §P.2; existence через манифест, **фоллбэк на оригинал** — прод без backfill не ломается). `_card.html`: card-вариант + `srcset/sizes`; `apt.html`: detail в галерее, полный оригинал по клику. Везде `loading=lazy decoding=async` + `width/height` из манифеста (**CLS→0**). | Юнит фильтра (вариант есть/нет/манифест отсутствует). E2e: карточки ссылаются на `.card.webp` при наличии; без манифеста — оригиналы, страница валидна. |
| **T4-3** | Кэш-заголовки фото | `deploy/Caddyfile`: far-future + `immutable` для `/photos/*` (проверить текущее, добавить при отсутствии). Изменение применяется на деплое (Wave 6). | Ручная проверка в smoke (заголовки в ответе). |
| **T4-4** | *(stretch, security)* Убрать inline-JS | Вынести inline `onchange` из фильтров в `static/js/filters.js` → в Caddyfile комментарий-готовность снять `'unsafe-inline'` из `script-src` (само снятие — на деплое, при зелёном smoke). Семя S-handoff §4. | E2e: фильтры работают без inline-обработчиков. |

### Wave 5 — БОЛЬШОЕ КОДОВОЕ РЕВЬЮ (обязательная часть этапа)

Read-only ревью **всего репозитория** (не только диффа этапа), 10 параллельных линз-субагентов:

| # | Линза | Фокус |
|---|---|---|
| L1 | Security | AuthZ/CSRF/rate-limit/lockout, заголовки, инъекции, секреты в коде/логах, path traversal в photo-путях |
| L2 | Privacy & ToS-инварианты | Точных координат нет **нигде** (включая JSON/HTML/OG); ноль площадочных/TG-CDN URL; writes публичного app = `leads`+`subscriptions`+`site_events`; в аналитике только хэши (ни IP, ни UA сырьём) |
| L3 | Data integrity | Миграция 0010/FK, математика rollup, retention-гард, purge-архив, WAL/busy_timeout на новом писателе |
| L4 | Performance | N+1, TTL-кэши, вес страниц после T4, SW-кэш vs новые ассеты, индексы `site_events` |
| L5 | Tests | Покрытие новых путей, качество ассертов, стабильность/флаки e2e |
| L6 | Architecture | Границы модулей, соответствие `DATA_CONTRACT.md`+дельте T, дубли, мёртвый код |
| L7 | A11y | Дашборд (таблицы/контраст/фокус/aria), `<picture>`-вёрстка, alt'ы |
| L8 | Taste / Design | Дашборд и карточки по токенам, анти-slop закон (скиллы `taste`+`impeccable`) |
| L9 | Ops / Deploy | Точность раннбука, systemd/nightly-юниты, бэкапы покрывают новые таблицы, rollback-пути |
| L10 | Docs drift | README/SPEC/HANDOFF/контракты синхронны коду после волн 1–4 |

**Процесс (по `requesting-code-review`):**
1. Линзы пишут находки в `docs/review/stage-t/REVIEW_FINDINGS.md` — формат: `severity (blocker/major/minor) · file:line · описание · repro/доказательство`.
2. **Состязательная верификация**: каждый finding перепроверяет свежий агент → confirmed/refuted (с доказательством).
3. Триаж кондуктором: blocker'ы блокируют Wave 6; major — фиксятся в этапе; minor — фикс или бэклог с записью.
4. Фиксы — **только TDD red→green**, отдельными коммитами `stage-t: 5/fix-<id>`.
5. Re-review затронутых линз → вердикт **ship / no-ship** в findings-файле.

### Wave 6 — Deploy + Docs + Merge (совместно с Дмитрием; каждый ssh-шаг подтверждается)

Раннбук (паттерн Stage R/W/S):
1. **Локально зелено:** full gate (юниты + e2e).
2. **Бэкап БД** на VPS: `sqlite3 … ".backup 'data/backups/otz-pre-stageT-$(date +%F-%H%M).db'"` + `integrity_check=ok` + дорелизные счётчики (leads/subscriptions/active listings).
3. **Код:** `scripts/deploy_sync.sh` (rsync; `.venv`/`data`/`.env` исключены).
4. **Env:** дописать в `/opt/otz/.env` блок из `env.additions-t.txt`; `SITE_ANALYTICS_SALT` сгенерить на сервере: `openssl rand -hex 32`. Никогда не коммитить.
5. **Миграция:** `.venv/bin/alembic upgrade head` → **0010** (только после шага 2).
6. **Фото-варианты на сервере:** `otz photos-backfill` (сначала `--dry-run` → цифры, затем боевой; прогресс в лог). **Запускать в фоне** (`otz-bg`/`tmux`/`nohup` — паттерн `deploy_sync.sh`): при ~2535 оригиналах × 2 варианта × webp `method=6` прогон занимает десятки минут, обрыв ssh убьёт процесс. Манифест пишется инкрементально (флаш каждые 100 оригиналов), поэтому обрыв теряет **≤100 записей манифеста**, а повторный прогон идемпотентен (добор без потери уже сгенерённых webp). Проверить `photo_manifest.json` создан **в родителе** `SITE_PHOTOS_ROOT` (`/opt/otz/data/photo_manifest.json`, не в `/opt/otz/data/inbound`).
7. **Nightly:** подвесить rollup/retention/dupes по механизму из T0-2 (если новый timer — юнит из `deploy/` + `systemctl enable --now`).
8. **Рестарт** `otz-site`/`otz-admin`; чистые логи; `asset_v` пересчитался (новые js/css) → SW-кэш инвалидировался.
9. **Smoke-матрица:**
   - `/api/map.json`: выборочные точки ≠ точным координатам из БД (снэп виден); e2e-гард на проде зелёный логикой сравнения.
   - Аналитика: открыть 3–4 страницы + тестовая заявка с `?utm_source=smoke` → строки в `site_events`, utm в `leads`; страницы не замедлились.
   - `/admin/analytics` рендерит карточки/ряд/топ/источники (после первого rollup — цифры за сегодня из raw).
   - Карточки/`/apt` отдают `.card.webp`/`.detail.webp` (DevTools Network), `width/height` на месте, CLS≈0.
   - Lighthouse mobile `/` и `/apt/{id}` — записать дельту к baseline T0-2 (ожидание: LCP заметно ниже).
   - `otz purge-inactive --dry-run` — отчёт получен (исполнение отдельным `go`).
   - `otz stats` — разбивка active/inactive.
   - `/healthz`=200, `/health`=401, admin снаружи закрыт, `otz-monitor NRestarts=0`.
10. **Docs (T6-док):** обновить `HANDOFF.md` (статус, env, 0010, новые модули/роуты), `README.md` (§Stage T), `docs/SPEC.md` (этапы), **влить `DATA_CONTRACT-T.md` в `DATA_CONTRACT.md`** (как §8 у W2), новый `SESSION_HANDOFF_2026-07-XX.md`, закоммитить `docs/BACKLOG.md`.
11. **Merge:** `finishing-a-development-branch` — ff → `main`, **`git push origin main`** (закрыть stale-remote навсегда), снос worktree.

**Rollback:** код — `git revert` + `deploy_sync.sh`; схема — `alembic downgrade -1` (0010 аддитивна);
аналитика — `SITE_ANALYTICS_ENABLED=false` (полный kill-switch: хуки записи no-op **и** nightly-шаг
rollup/retention глохнет целиком — `_analytics_nightly_line` выходит ранним «off»); фото — `photo_v`
фоллбэчит на оригиналы (варианты можно просто удалить, манифест стереть); nightly-джобы — фиксятся
per-step флагами (`NIGHTLY_*=0`) в рамках `otz-nightly-refresh` (отдельного `otz-nightly.timer` нет, T0-2.4).

---

## 3. Ownership (параллельность без конфликтов)

| Агент | Владеет (пишет) | НЕ трогает |
|---|---|---|
| **W1 backend** (T1-1) | `src/site/readmodel.py` (map_points), `tests/site/test_map_privacy.py` | `maps.py` (только импорт), routes, шаблоны |
| **W1 cli** (T1-2/3/4) | `src/cli.py` (heal/purge/stats), nightly-модуль, `tests/test_cli_*.py` | `src/site/*` |
| **W2 backend** (T2-*) | `alembic/versions/0010_*`, `src/db/models.py` (только новые сущности), `src/site/analytics.py` (новый), `src/site/routes.py` (только хуки записи), `src/site/leads.py` (utm), `static/js/utm.js`, `templates/partials/_lead_form.html` (только hidden-поля), тесты | admin, публичный CSS, `_card.html`/`apt.html` |
| **W3 admin** (T3-*) | `src/admin/` (роут+шаблон analytics), admin-CSS | `src/site/*` (читает агрегаты через свои сессии) |
| **W4 backend** (T4-1) | `src/site/photos.py` (новый), publish-action hook в admin, `src/cli.py` (photos-backfill), тесты | шаблоны |
| **W4 frontend** (T4-2/4) | `templates/partials/_card.html`, `templates/apt.html`, Jinja-фильтр (регистрация в `app.py` — только фильтр), `static/js/filters.js`, `static/css/site.css`/`apt.css` (минимально) | `base.html`, admin |

`src/cli.py` трогают Wave 1 и Wave 4 — волны последовательны, конфликта нет. `base.html` не трогает
**никто** (нужда → эскалация кондуктору). `app.py` — только регистрация Jinja-фильтра (W4) и, при
необходимости, middleware-хук (W2); пересечение разводится очерёдностью волн.

---

## 4. Wave 0 — таблица решений (дефолты применяются; отклонение фиксировать в DECISIONS-T)

| # | Вопрос | Дефолт | Отклонённые альтернативы |
|---|---|---|---|
| **D1** | Где живёт аналитика | **Server-side в той же SQLite**, без cookies, без внешних скриптов, без баннеров | GA (адблоки, баннер, внешний JS), self-host Plausible/Umami (второй рантайм — против ADR «одна машина, один тулчейн») |
| **D2** | Атрибуция источника заявки | **first-touch utm+referrer в sessionStorage** (крошечный `utm.js`) + серверный фоллбэк same-page query; без cookies | cookie-first-touch (тянет consent-баннер) |
| **D3** | Ретеншн сырых событий | **90 дней** (агрегаты — вечно) | 30 / 180 |
| **D4** | Фуззи `/map` | **Тот же `fuzz_coords`-снэп (~100 м)**, что на `/apt` — консистентная политика приватности | круг на каждый пин (визуальный шум на кластерах), оставить точные (дыра) |
| **D5** | Судьба 8442 трупов | **В этапе — только dry-run отчёт + инструмент с архивом.** Физический purge — отдельный `go` Дмитрия после чтения отчёта | немедленное удаление |
| **D6** | Дедуп | **Nightly report-only**, Дарья курирует руками | авто-мерж (риск склейки разных квартир) |

Открытых блокирующих вопросов нет — этап стартует на дефолтах сразу после Wave 0.

---

## 5. DoD этапа

- **Privacy:** `/api/map.json` не содержит ни одной точной пары координат; e2e-гард `_assert_no_precise_coords` живёт в suite постоянно.
- **Analytics:** события пишутся (pageview/lead/subscribe) с атрибуцией; rollup+retention доказаны тестами; **сбой аналитики никогда не роняет страницу** (тест); kill-switch работает.
- **Dashboard:** `/admin/analytics` показывает 7/30д метрики, топ-квартиры с CTR, источники; прошёл `taste`+`impeccable`; скриншот в PR/handoff.
- **Фото:** card p95 ≤ 120 KB, detail p95 ≤ 350 KB; `width/height` везде (CLS≈0); LCP-дельта к baseline T0-2 записана в handoff.
- **Гигиена:** heal-гард активен; `purge-inactive --dry-run` отчёт получен; `stats` показывает active/inactive; dupes-report в nightly.
- **Review:** Wave 5 вердикт **ship**, все blocker'ы закрыты TDD-фиксами, findings-файл в repo.
- **Инженерия:** full gate зелёный (юниты > 832, e2e > 32 — точные числа в handoff); **ровно одна** миграция 0010; **ноль новых зависимостей**; `origin/main == main`.
- Документация Wave 6 п.10 выполнена; `SESSION_HANDOFF_2026-07-XX.md` написан.

---

## 6. Инварианты (повтор для агентов — читать перед каждой задачей)

- Публичный app пишет ТОЛЬКО `leads` + `subscriptions` + **`site_events` (append-only телеметрия)**.
  Расширение инварианта — осознанное решение этапа (D1), зафиксировано в контракте §0.
- Ноль площадочных/TG-CDN URL наружу. Ноль точных адресов/координат в любой публичной поверхности —
  **теперь включая `/map` и `/api/map.json`**.
- Без нового рантайма/сборки/зависимостей: FastAPI + Jinja + ванильный CSS + минимальный JS.
  Спарклайны — inline-SVG из шаблона, не chart-библиотека.
- Дизайн-закон Belgrade Editorial: токены, цифры `--mono` (JetBrains Mono), анти-slop; визуальные задачи
  проходят `taste` + `impeccable`.
- Кэш-бастинг новых css/js — `?v={{ asset_v }}`.
- Ровно одна миграция (0010). Новая колонка вне плана → эскалация кондуктору, не «по-тихому».
- Секреты (`SITE_ANALYTICS_SALT`) — только в серверном `.env`, никогда в коде/логах/гите.
- Коммиты `stage-t: <wave>/<task> — <кратко>`; full-suite gate на закрытии каждой волны.
