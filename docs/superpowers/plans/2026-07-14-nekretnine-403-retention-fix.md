# План «Liveness & Resilience» — nekretnine-403 · снятие с сайта по живости на площадках

> Дата: 2026-07-14 · База: `main` @ **`d43ce20`** (= прод, Stage T, alembic head **0010**), рабочее дерево чистое.
> Ветка: **`fix/liveness-and-403`** (worktree от `main`).
> **Исполнитель: Claude Opus 4.8** (Claude Code) · Кондуктор: Дмитрий. Методология: obra/superpowers,
> TDD везде, subagent-driven по ownership §5. **Ноль новых зависимостей** (эскалация — только D6).
> **Максимум одна миграция (0011, опциональна по D4).**
> Вход: два репорта 13.07 — smoke `NEKRETNINE 403` + failed unit (одно событие, P0) и retention-нотис
> «пора убрать (42)» (P1). Диагноз — Приложение 1; ревью состояния проекта — Приложение 2 (контекст, не задачи).

---

## 0. Контекст для исполнителя (прочитать ДО старта, в этом порядке)

1. `HANDOFF.md` **§0.4 «Критичные правила»** и §0.-6 (текущий статус) — обязательно.
2. Этот план целиком, включая Приложение 1 (диагноз — там факты с file:line).
3. Точечно по задачам: `src/scrapers/base.py`, `src/scrapers/nekretnine.py`, `src/scrapers/smoke.py`,
   `src/scrapers/orchestrator.py:368-547` (stale-sweep / A3 verify-links / X2), `src/site/retention.py`,
   `src/admin/routes.py:64-141`, `src/cli.py::_run_nightly_steps` (~:1490-1575, паттерн fault-isolated шагов),
   `src/matching/dedup.py`, `src/config.py`.

**Суть одним абзацем.** (P0) nekretnine.rs начал отдавать 403 на detail-страницы запросам с VPS — сама
страница жива (проверено 14.07 с не-VPS сети: 200, объявление обновлено 12.06). Это блок IP/фингерпринта,
а не смена разметки; каскад — новые объявления площадки не получают детали. (P1) Решение Дмитрия 14.07:
**возраст карточки на сайте — не причина снимать**; карточка живёт, пока квартира существует хотя бы на
одной площадке (dup-группа A4), снимается только когда подтверждённо пропала со всех. 21-дневный нотис
упраздняется как action-driver.

**Роли.** Агент: код + тесты в worktree, локально. Шаги с пометкой 🧑 — VPS/прод/деньги/решения: выполняются
Дмитрием или совместно, **только по явному go**. Агент готовит для них команды и интерпретирует вывод.

---

## 1. Железные правила (нарушение = стоп и вопрос Дмитрию)

- Деплой = `./scripts/deploy_sync.sh [--restart]`, **НЕ git** (на VPS нет репо). `.env` на сервере — только руками. `data/` на VPS не трогать.
- **НИКОГДА не rsync'ить из worktree** (`.venv`-симлинк, инцидент 20.06) — деплой только из основного чекаута после merge.
- LLM-строку `claude-haiku-4-5` не менять. Новые зависимости — только через решение D6.
- Юниты и e2e гонять **РАЗДЕЛЬНО** (BACKLOG #15): `.venv/bin/python -m pytest tests/ -q`, затем `RUN_E2E=1 .venv/bin/python -m pytest tests/e2e/site -q`.
- Гейт каждой волны: unit-пол из Wave 0 + все новые тесты зелёные; следующая волна не стартует до зелёного гейта.
- Коммиты: `fix-liveness: <волна>/<задача> — <кратко>`; атомарно, каждый коммит несёт свой red→green тест.
- **Fail-safe доминирует:** при любой неоднозначности живости карточка НЕ снимается.
- Если факт в этом плане противоречит коду — прав код: исправь план-файл (отдельным коммитом) и продолжай.

---

## 2. Решения (дефолты применяются без блокировки; менять — только через Дмитрия)

| # | Вопрос | Дефолт |
|---|---|---|
| D1 | Auto-hide сразу или warn-first? | `SITE_LIVENESS_AUTO_HIDE=false` в коде (нотис-only). Включение на проде через `.env` после ~недели чистых нотисов — 🧑 |
| D2 | Включать ли A4-тег в nightly? | Да: `NIGHTLY_DUPES_WRITE` (дефолт `true`). `find_cross_platform_dupes(write=True)` идемпотентен (reset+re-tag активных), report-шаг уже в nightly |
| D3 | Cap авто-снятий за ночь | `SITE_LIVENESS_MAX_HIDES=10` (по образцу price-drop cap); сверх капа → в кандидаты нотиса |
| D4 | Pin «держать на сайте» | Делаем: миграция 0011, `site_listings.keep_on_site BOOL NOT NULL DEFAULT false` + тогл в админке. Если Дмитрий скажет «без миграции» — скип T3-5, матрица без pin |
| D5 | Судьба 21-дневного механизма | Команда `site-retention-notice` и nightly-вызов удаляются; `overdue_listings`/`format_notice` заменяются liveness-аналогами; возрастной бейдж в админке остаётся как FYI (текст «пора убрать» → нейтральный «N дн. на сайте») |
| D6 | 403 не лечится прокси/заголовками? | Эскалация отдельным решением: `http2` (dep `h2`) / curl_cffi / Playwright-фоллбэк для деталей nekretnine. В этот этап НЕ входит |
| D7 | Способ таргет-перепроверки URL | HEAD с семантикой A3 (`_DEAD_LINK_STATUSES = {404, 410}` = мёртв; всё прочее = inconclusive). GET не городить |
| D8 | Формат liveness-нотиса | Топ-10 строк + «…и ещё N»; секции «🪦 сняты» / «⚠️ кандидаты»; молчит, когда пусто |

---

## 3. Волны

### Wave 0 — фиксация базы (агент, ~30 мин)

- [x] `git fetch`; если `origin/main` отстаёт от `main` — `git push origin main` **до** worktree (болячка W2/S/T). → `main` уже == `origin/main` @ `d43ce20`, push не нужен.
- [x] Worktree `fix/liveness-and-403` от `main` @ `d43ce20`; `uv sync`. → создан в `.claude/worktrees/`, `uv sync` ok.
- [x] Прогнать оба сьюта раздельно; **вписать фактический пол сюда:** unit = `1055 passed` (53 skipped), e2e = `52 passed`. (14.07, совпало с ожиданием.)
- [x] Прочитать файлы из §0. Сверить ключевые факты Приложения 1 с кодом. → сверено: `fetch_detail` голый (`nekretnine.py:249-252`), `_fetch_page` с ретраями (`:254-282`), smoke detail-проба (`smoke.py:139-149`), лживый `n_previews=0` в except (`:185-195`), `_make_client` без браузерных заголовков (`base.py:81-94`), фото-клиент (`orchestrator.py:295`) и verify-links клиент (`:517`) прокси не уважают — всё подтверждено.

### Wave 1 — хардненинг скрейпера и smoke (агент, TDD; 3 параллельных сабагента по §5)

| ID | Задача | Суть | TDD (red → green) |
|---|---|---|---|
| **T1-1** | Retry в `fetch_detail` | `nekretnine.py:249-252`: сейчас голый `get + raise_for_status`. Дать семантику `_fetch_page` (`:254-282`): `fetch_attempts=3`, backoff `retry_backoff_sec × attempt`, 429 → backoff ×5 (мин 10s×attempt). **4xx-детерминированные (403/404) не ретраить** — поднимать сразу. Контракт «поднимает исключение при неудаче» сохранить (`orchestrator._process_one:238` и smoke на него завязаны) | Мок-транспорт по образцу существующих тестов в `tests/test_scrapers_nekretnine.py`: 429→200 со 2-й попытки = успех; 500×3 → raise; 403 → raise немедленно, без sleep; 404 → raise немедленно |
| **T1-2** | Smoke: устойчивость detail-пробы | `smoke.py:139-149`: обернуть `fetch_detail` в try/except **внутри** цикла по превью — исключение = неудача этого превью, идём к следующему (до `_DETAIL_PROBE_COUNT`). Все пробы упали → `detail_ok=False`, `error=<последняя ошибка>`, но зонд НЕ падает целиком | 1-е превью raises 403, 2-е отдаёт полный detail → `ok=True`; все три raise → `ok=False`, `error` содержит «403», `n_previews` = фактическому числу превью |
| **T1-3** | Честный `n_previews` в except-ветке | `smoke.py:185-195`: краш до/вне detail-пробы (например, `list_recent`) сейчас репортит `n_previews=0` даже когда лист отработал — сохранить фактическое значение, если previews уже получены (алерт «превью=0» 13.07 был враньём) | Тест: `list_recent` ок (N превью), краш дальше → `SmokeResult.n_previews == N` |
| **T1-4** | Браузерные заголовки | `base.py:82-94` `_make_client`: добавить `Accept: text/html,…`, `sec-ch-ua*`, `Sec-Fetch-Site/Mode/Dest`, `Upgrade-Insecure-Requests: 1`. `fetch_detail` (nekretnine) шлёт `Referer: <list_url>` | Снапшот заголовков клиента; перехват запроса мок-транспортом: у detail-запроса есть `Referer` |
| **T1-5** | Proxy plumbing в боковые клиенты | `SCRAPER_PROXY_URL` уважают только скраперы. Прокинуть `proxy=settings.scraper_proxy_url or None` в фото-загрузчик (`orchestrator.py:295`) и verify-links клиент (`orchestrator.py:517`) — иначе при IP-блоке фото/HEAD продолжат ходить с VPS-IP | Тест на фабрику/создание клиента: при заданном `scraper_proxy_url` клиент собирается с proxy (вынести создание в мокаемый хелпер, если нужно) |

**Гейт Wave 1:** unit-пол + новые зелёные; `ruff check` чистый.

> ✅ **Wave 1 СДЕЛАНА (14.07).** Коммиты: T1-1 `f93e20b`, T1-2+T1-3 `8248116`, T1-4 `4404e6b`, T1-5 `1290977`.
> Гейт: **unit 1067 passed** (1055 флор + 12 новых), 53 skipped. Реальных lint-проблем в диффе нет.
> ⚠️ **Находка тулинга (в BACKLOG, Wave 5):** `uv.lock` плавает `ruff>=0.6.0` → синк подтянул **ruff 0.15.21**,
> под которым baseline @ `d43ce20` даёт ~900 ошибок (RUF001/002/003 на кириллице/×/→ по всему репо + новые
> правила UP017 и пр.). Гейт «ruff чистый» проверялся как «нет НОВЫХ реальных проблем в диффе» (unicode-стиль и
> version-drift исключены). Надо запинить ruff в lock либо прогнать разовую unicode-чистку.

### Wave 2 — 🧑 диагностика и обход 403 на VPS (параллельно волнам 1–4; код от неё не зависит)

- [ ] 🧑 **B1.** `ssh root@90.156.254.159 'journalctl -u otz-scrape-smoke -n 200 --no-pager; tail -100 /var/log/otz/scrape-smoke.log'` — с какого прогона (таймер `00/6:30` UTC) начались 403; были ли зелёные после.
- [ ] 🧑 **B2.** curl-матрица с VPS (агент интерпретирует): list-URL и detail-URL × {UA скрапера, браузерный UA + полные заголовки}; смотреть код ответа + тело 403 (сигнатура WAF) + `Server`/`cf-ray`-заголовки. Тот же detail-URL с локальной машины Дмитрия — контроль (14.07 был 200).
- [ ] 🧑 **B3.** Развилка: **IP-блок** (лист тоже деградирует / WAF-страница) → арендовать EU-прокси, `SCRAPER_PROXY_URL=...` в `/opt/otz/.env`, `systemctl start otz-scrape-smoke.service`, смотреть таблицу. **Фингерпринт** (браузерные заголовки с VPS дают 200) → достаточно T1-4; если нет — эскалация D6.
- [ ] 🧑 **B4.** Критерий выхода: 8 подряд зелёных прогонов smoke (48ч) + coverage nekretnine в nightly-сводке на историческом уровне + новые listings/сутки восстановились.

### Wave 3 — liveness-ядро (агент, TDD)

**Контракт (C0) — статус члена dup-группы** (группа = все `listings` с тем же `dup_group_id`; `dup_group_id IS NULL` → группа из одного матчёванного listing):

| Статус | Условие |
|---|---|
| `ACTIVE` | `is_active=1` |
| `DEAD` | `is_active=0` **и** таргет-HEAD его URL вернул 404/410 (D7) |
| `INCONCLUSIVE` | `is_active=0`, но HEAD дал 403/405/5xx/таймаут/сетевую ошибку — состояние неизвестно |

**Решение по публичной карточке** (для каждой `site_listings.is_public=1`, через `match_id → listing`):

| Условие | Действие |
|---|---|
| `keep_on_site=1` (pin, D4) | KEEP, в отчёте «pinned» |
| Хоть один член `ACTIVE` | KEEP (возраст не важен) |
| Хоть один `INCONCLUSIVE` (и ни одного `ACTIVE`) | KEEP + строка в «⚠️ кандидаты (не подтверждено)» |
| **Все** члены `DEAD` | HIDE при `SITE_LIVENESS_AUTO_HIDE=1` и капе D3 (через `hide_site_listings_for_listing` — атомарность/кэш уже внутри, `orchestrator.py:430-458`); иначе → «🪦 кандидаты (подтверждено)» |

Гард «площадка нездорова этой ночью» в v1 сознательно НЕ строим: таргет-HEAD даёт fail-safe per-URL
(заблокированная площадка = 403 = INCONCLUSIVE = KEEP). Зафиксировано как упрощение.

| ID | Задача | Суть | TDD |
|---|---|---|---|
| **T3-1** | Конфиг | `src/config.py`: `site_liveness_enabled=True` (kill switch шага), `site_liveness_auto_hide=False` (D1), `site_liveness_max_hides=10` (D3), `nightly_dupes_write=True` (D2). Синхрон: `.env.example`, `deploy/env.additions.txt` | Дефолты + env-override читаются |
| **T3-2** | A4-тег в nightly | `cli.py:875-897 _write_dupes_report`: `write=False` → `write=settings.nightly_dupes_write`; строка сводки показывает режим (`report-only` / `write`) | Адаптировать `tests/test_cli_dupes_nightly.py`: флаг true → вызов с `write=True`; false → как раньше |
| **T3-3** | `src/site/liveness.py` | Новый модуль (эволюция `retention.py`): `async def site_liveness_sweep(*, now=None, client=None, dry_run=False) -> LivenessReport` — резолв групп, статусы по C0, решения по матрице, HEAD-хелпер с A3-семантикой (клиент инжектируемый, как в `verify_active_links`). `LivenessReport`: `kept_active/kept_pinned/candidates_unconfirmed[]/candidates_confirmed[]/hidden[]/errors` | Матрица целиком: активный сосед держит · все DEAD → hidden (auto-hide on) · все DEAD → candidate (off) · INCONCLUSIVE держит · pin держит · cap: 11 мёртвых → 10 hidden + 1 candidate · dry_run не мутирует · группа-одиночка · пустой прогон |
| **T3-4** | Нотис + CLI + nightly-хук | `format_liveness_notice` (D8; фоллбэк района → `address_raw`/`title` — фикс «—» из скрина). CLI `otz site-liveness [--dry-run]`. В `_run_nightly_steps` шаг ставится **вместо** retention-вызова (`cli.py:1524-1532`), тот же fault-isolated паттерн (try/except → 🚨-строка, сводка всегда получает строку). Отправка — `send_text_via_bot`, молчит при пустоте | Нотис-формат (топ-10+счётчик, секции, пустота = не слать); nightly-шаг не роняет прогон при исключении; kill switch глушит шаг |
| **T3-5** | Pin (D4) | Миграция **0011** `site_listings.keep_on_site` (bool, NOT NULL, server_default false; batch mode как 0006–0010, downgrade полноценный) + модель + тогл в `_site_controls.html` (CSRF-паттерн соседних кнопок) | Up/down roundtrip; тогл флипает поле; sweep уважает pin (уже в T3-3) |
| **T3-6** | Ретирование 21-дневного (D5) | Удалить `site-retention-notice` (`cli.py:1406-1415`), `overdue_listings`/`format_notice`/`send_overdue_notice`; `tests/test_retention.py` → `tests/test_site_liveness.py`. В админке `SITE_RETENTION_DAYS`-бейдж переименовать в нейтральный «N дн. на сайте» (логика age_days остаётся, FYI) | Grep-гард: `site-retention-notice` больше нет в src/; тесты переехали |

**Гейт Wave 3:** unit-пол + новые; alembic up/down на пустой и засеянной БД.

> ✅ **Wave 3 СДЕЛАНА (14.07), кроме admin-UI-хвостов.** Коммиты: T3-1+T3-2 `64de43d`, T3-5-схема `111cda3`,
> T3-3 `bd641a6`, T3-4+T3-6 `2528cfa`. Гейт: **unit 1088 passed** (1055 флор + 12 Wave1 + 21 Wave3), 53
> skipped; alembic up/down/roundtrip на пустой и засеянной БД (тесты 0010/0011) зелёные; `src/site/liveness.py`
> ruff-чист. Побочно: `test_migration_0010` привязан к абсолютным ревизиям (был head/-1, ломался от 0011).
> **Перенесено в Wave 4 (admin-facing):** T3-5-UI (тогл pin в `_site_controls.html`) и T3-6-бейдж (переименование
> «пора убрать» → нейтральный «N дн. на сайте»). Ядро (config/dupes-write/liveness-модуль/нотис/CLI/nightly/
> миграция/удаление retention.py) — готово.

### Wave 4 — админка (агент, TDD; после Wave 3 — общие файлы)

| ID | Задача | Суть | TDD |
|---|---|---|---|
| **T4-1** | Фильтр «Пропавшие» + починка окна | `/published` (`routes.py:71-141`): новый `window=gone` — выборка **от `site_listings`** (`is_public=1`, join match→listing), НЕ от `published_at` (сейчас дефолтное окно «3 недели» прячет старые карточки — UX-баг из диагноза). В gone-режиме показывать разбивку по C0-статусам из отчёта sweep (лайт-вариант: `is_active` матчёванного + счётчик живых в группе) | gone-фильтр возвращает карточку, опубликованную 30 дн назад (в дефолтном окне её нет); счётчики группы корректны |
| **T4-2** | Bulk-снятие | Чекбоксы на карточках + `POST /published/bulk-unpublish` (список `site_listing_id`, CSRF, confirm-подтверждение в UI, лог количества). Это ручной инструмент warn-first-режима (D1) | POST без CSRF → 403; с CSRF снимает ровно выбранные (is_public=0, is_featured=0), идемпотентно |
| **T4-3** | Живость на карточке | Бейдж «жива на 4zida» / «мертва везде (не подтверждено)» в `published.html` из данных T4-1 | Рендер-тест обоих состояний |

**Гейт Wave 4:** unit-пол + новые; e2e-сьют зелёный (отдельным прогоном).

### Wave 5 — доки + merge (агент)

- [ ] Доки: `HANDOFF.md` (новая секция §0.-7: что сделано + как включать auto-hide), `README.md` (абзац retention→liveness), `.env.example`/`deploy/env.additions.txt` (новые ключи), `docs/BACKLOG.md` (внести D6-эскалацию как пункт; закрыть строки, которые закрыл этап), отметки [x] в этом файле.
- [ ] Полный гейт: unit + e2e раздельно, `ruff check`.
- [ ] Merge ff в `main`, `git push origin main`, снести worktree (`finishing-a-development-branch`).

### Wave 6 — 🧑 деплой + разовый триаж 42 (совместно, по go)

- [ ] 🧑 Бэкап БД → `./scripts/deploy_sync.sh --restart` **из основного чекаута** → `alembic upgrade head` (0011, если D4 в силе) → рестарт юнитов по правилам §1.
- [ ] 🧑 **Порядок важен:** сначала Wave 2 B3/B4 (иначе все nekretnine-члены = INCONCLUSIVE и триаж слепой).
- [ ] 🧑 `otz-bg site-liveness --dry-run` → отчёт Дарье: X подтверждённо мертвы (снять bulk-ом из T4-2 или включить auto-hide), Y живы (остаются, действий не нужно), Z inconclusive.
- [ ] 🧑 Через ~неделю чистых нотисов: `SITE_LIVENESS_AUTO_HIDE=1` в `/opt/otz/.env` (D1).

---

## 4. Definition of Done

- [ ] Все волны отмечены; гейты зелёные (unit ≥ пол Wave 0 + новые, e2e = пол Wave 0); ruff чистый.
- [ ] Smoke: 8 подряд зелёных прогонов после Wave 2 (критерий B4).
- [ ] Ежедневный «пора убрать (N)» больше не приходит; вместо него liveness-нотис по D8 (молчит при пустоте).
- [ ] 42 карточки триажированы (Wave 6); фильтр «Пропавшие» соответствует реальности.
- [ ] `HANDOFF.md`/`README`/`.env.example`/`BACKLOG` синхронизированы с кодом.

## 5. Ownership (для параллельных сабагентов)

| Волна | Поток | Файлы |
|---|---|---|
| 1 | A: T1-1+T1-2+T1-3 | `nekretnine.py`, `smoke.py`, их тесты |
| 1 | B: T1-4 | `base.py` + тест |
| 1 | C: T1-5 | `orchestrator.py` (только создание клиентов) + тест |
| 3 | A: T3-1+T3-2 | `config.py`, `cli.py` (dupes-строки), `.env.example`, `deploy/env.additions.txt` |
| 3 | B: T3-3+T3-4+T3-6 | `src/site/liveness.py` (new), `retention.py` (удаление), `cli.py` (nightly-хук/CLI), `tests/test_site_liveness.py` |
| 3 | C: T3-5 | `alembic/versions/0011…`, `models.py`, `_site_controls.html` + тесты |
| 4 | последовательно | `admin/routes.py`, `templates/published.html`, `_site_controls.html`, `test_admin_publish.py` |

Пересечения `cli.py` между потоками A и B Wave 3 — развести по времени (A коммитит первым) или отдать одному сабагенту.

---

## Приложение 1 — диагноз (наблюдения 13–14.07, вход для Wave 1–2)

### P0 · 403 на detail nekretnine

| Факт | Источник |
|---|---|
| Упал `fetch_detail` (голый `raise_for_status`, без ретраев — в отличие от `_fetch_page`) | `nekretnine.py:249-252` |
| Лист отработал (превью были); «превью=0» в алерте — артефакт except-ветки | `smoke.py:185-195` |
| Одно упавшее превью роняет весь зонд (нет per-preview try/except) | `smoke.py:139-149` |
| Объявление 1172423 живое, с не-VPS сети 200 (обновлено 12.06, €3200) — 14.07 | live-проверка |
| ⇒ блок таргетированный: IP VPS (RU-хостинг 90.156.254.159) или фингерпринт httpx; рифма — BACKLOG #2 | вывод |
| Каскад: тот же `fetch_detail` кормит скрап и ночной refetch/heal → новые объявления без деталей/фото; stale-sweep защищён coverage-гардом, массовой деактивации не будет | `orchestrator.py:238,792` |
| Оба алерта 13.07 = одно событие: exit 1 → unit failed → OnFailure (by design) | `cli.py:3182-3204` |

### P1 · retention 42 → живость

| Факт | Источник |
|---|---|
| Все 42 — «21 дн.» ⇒ волна массовой публикации ~21–22.06 пересекла порог разом (проверить SQL-ом `site_listings.created_at`) | вывод |
| A3 verify-links (nightly, cap 1000): HEAD активных с PENDING/PUBLISHED-матчем; 404/410 → `is_active=0`; 403/5xx/таймаут = inconclusive, fail-safe | `orchestrator.py:461-547` |
| X2 auto-hide (`NIGHTLY_AUTO_HIDE_SITE_LISTINGS=1`): прячет карточку атомарно, но вызывается **только** из verify-links и смотрит на **один** матчёванный listing — dup-группу не видит | `orchestrator.py:430-458,540` |
| stale-sweep X2 не вызывает → `is_public` рассинхрон (спасает C1-фильтр каталога) | `orchestrator.py:368-400` |
| `dup_group_id` в проде пуст: nightly-шаг жёстко `write=False` | `cli.py:884`, HANDOFF §0.-5 |
| UX-баг: дефолтное окно `/published` «3 недели» по `published_at` прячет старые карточки (видны только в «все», limit 300) | `routes.py:68,84-96` |
| `SITE_RETENTION_DAYS` продублирован («mirror»-комментарий) | `admin/routes.py:66`, `site/retention.py:25` |

## Приложение 2 — ревью состояния проекта (14.07.2026; контекст, не задачи)

**Snapshot:** `main` @ `d43ce20` = прод (Stage T, деплой 02.07), дерево чистое, alembic 0010. ~17.3k LOC `src/` + ~23.4k LOC тестов, 1009 тест-функций. 12 дней без коммитов — эксплуатационный режим.

**Сильное:** наблюдаемость реально работает (оба инцидента поймали свои же зонды: A2 smoke + OnFailure, retention-нотис); дисциплина этапов (Wave 5: 54 находки → 40 подтверждено, 0 blocker, вердикт SHIP); приватность (fuzz-координаты, hash-only аналитика, fail-closed соль); осторожная эксплуатация (warn-first, dry-run purge, бэкапы, coverage-гарды).

**Риски (по убыванию):**

| # | Риск | Действие |
|---|---|---|
| 1 | Единственный RU-IP для скрейпинга и витрины; 403 — первый звонок; admin светит тот же IP | Этот план (Wave 1–2) + BACKLOG #1 (Cloudflare) + #2 (не-RU замеры) |
| 2 | Ротация секретов не подтверждена: Resend-ключ и `ADMIN_PASSWORD` светились в чате 21.06 | 🧑 проверить/ротировать, 15 мин |
| 3 | Гигиена БД: 26.4k inactive ждут боевого purge + VACUUM; файл 1.4G | BACKLOG #3, по go |
| 4 | Alert-эргономика: 2 сообщения на инцидент; ежедневный полный список retention | Закрывается T3-4 (D8) |
| 5 | Архдолг: publish-логика в admin-пакете (#20), `cli.py` ~3.3k строк, дубль utm (#18) | Не срочно |
| 6 | CSP `unsafe-inline` (#5) · внешний uptime (#9) | Дешёвые следующие шаги |
| 7 | `HANDOFF.md` 934 строки, §0.3 задублирован | Причесать при следующем handoff |

**Порядок после этого этапа:** ротация секретов → BACKLOG #9 uptime → #1 Cloudflare → #3 purge.
