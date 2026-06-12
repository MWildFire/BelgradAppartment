# otz-rent-finder — handoff документ

Цель этого документа — дать новому инстансу Claude (или человеку) исчерпывающий контекст для продолжения работы над проектом.

---

## 0. ТЕКУЩИЙ СТАТУС + ПЛАН РАЗРАБОТКИ (handoff 2026-06-12, вечер — этап H1)

> Эта секция — рабочий handoff последней сессии. Остальной документ — общий runbook.
> Память Claude по проекту: `~/.claude/projects/-Users-mvstrike-BelgradAppartment/memory/`
> (project-ops, nekretnine-nextjs-migration, site-pagination-caps).

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
5. **Алерты systemd**: шаблон `otz-notify-failure@.service` + `OnFailure=` у monitor/nightly/daily-summary юнитов; у монитора явные `StartLimitIntervalSec=600`/`StartLimitBurst=5` (иначе OnFailure не сработал бы никогда). Smoke: `systemctl start otz-notify-failure@smoke.service`. Docs: PHASE5_DEPLOY §5.4.
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

### 0.2 Проверить утром 13.06 (после первого ночного прогона с H1)

- [ ] **Ночная сводка за 13.06** (таймер 03:14 UTC): «Обновлено (active)» ~20-21k; у всех трёх
      площадок «покрытие N% из M»; НОВЫЕ строки: «heal: исправлено X / осталось Y»,
      «деталей обновлено N, цен изменилось M», прунинг embeddings, метрики. Без 🚨-строк шагов.
      Время прогона: ожидаем +20-80 мин к обычному (heal 500 + rolling 200 fetch'ей).
- [ ] **Drop-алерт НЕ должен сработать** в первую ночь (daily_metrics была пуста — сравнивать
      не с чем). Если сработал — баг, смотреть `daily_metrics`.
- [ ] **Price-drop сообщения**: ≤10, цены/ссылки осмысленные. Первые ночи трогают самые
      протухшие listings — пик сейчас, дальше спадёт. Если шумно для Дарьи — поднять
      `PRICE_DROP_MIN_PCT` в `.env`.
- [ ] **Диск/WAL после первого прунинга**: `ls -lh /opt/otz/data/otz.db*` — WAL мог разово
      вырасти на сотни МБ; при случае ручной VACUUM (§15.7).
- [ ] `monitor.log`: матчи по-прежнему за секунды; нет «database is locked» в окно 03:00-05:00;
      heal-ошибок много в первую ночь — норм (мёртвые строки), должно падать ночь к ночи.
- [ ] `llm_decisions.jsonl` появился после первого LLM-матча; `otz llm-report --days 1` парсит.
- [ ] **W0-5 (не сделано, требует согласования с Дарьей):** `otz-bg rematch-all` после
      бэкфилла обложек — перепроверит старые NO_MATCH, СГЕНЕРИТ ДОСТАВКИ Дарье. Предупредить её.

### 0.3 Бэклог (после H1, приоритизировано)

**Закрыто в H1:** весь P1 (heal, гонка дедупа, индексы, rolling re-fetch) и весь P2
(column-select, прунинг, кэш match-all, to_thread) из прошлого handoff'а.

**Осталось / новое из ревью H1:**
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
- Авторизация — `scripts/auth_logger.py` (одноразовая, требует SMS code)
- Можно вызывать concurrently с monitor.service — это разные session-файлы

### 5.4 Re-authorization (если sessions сломались)
```bash
# monitor session:
ssh -t root@90.156.254.159 'sudo -u otz bash -c "cd /opt/otz && .venv/bin/python scripts/auth_telethon.py"'

# logger session:
ssh -t root@90.156.254.159 'sudo -u otz bash -c "cd /opt/otz && .venv/bin/python scripts/auth_logger.py"'
```

---

## 6. БД — SQLite (WAL mode) на VPS

**Путь:** `/opt/otz/data/otz.db` (owner otz:otz).

**Таблицы (упрощённо):**
- `source_channels` — @OtzRentBelgrad (id=1), watermark `last_processed_message_id`
- `inbound_posts` — каждый альбом из source, с `photo_paths`, `photo_phashes`, `photo_embeddings` (CLIP float16 BLOB)
- `listings` — все scraped квартиры с 3 платформ. `is_active=True/False` управляется `mark_stale_inactive`. Ключевые поля: `external_id`, `url`, `price_eur`, `area_m2`, `rooms`, `district`, `address_raw`, `lat`, `lon`, `photo_urls`, `photo_phashes`, `photo_embeddings`
- `matches` — `(inbound_post_id, listing_id)` + `confidence`, `matched_photo_count`, `method` (`phash` / `clip` / `clip+llm`), `status` (PENDING/PUBLISHED/REJECTED)
- `published_posts` — `(match_id, tg_message_id, post_text)` — фактическая доставка

**Миграции:** Alembic в [alembic/](alembic/). Применять: `cd /opt/otz && .venv/bin/alembic upgrade head`.

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
```

Шаблон: [.env.example](.env.example).

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
└── admin/                          # FastAPI read-only UI (опционально)

scripts/
├── vps_install.sh                  # one-shot bootstrap для Ubuntu 24.04
├── deploy_sync.sh                  # rsync laptop → VPS
├── auth_telethon.py                # auth для otz_monitor.session
└── auth_logger.py                  # auth для otz_logger.session

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
