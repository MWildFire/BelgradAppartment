# otz-rent-finder — FEATURE PLAN (Stage R — «Resilience & Reach»)

> **Дата плана:** 2026-06-23 · **Автор:** Claude Code (Opus 4.8), по итогам сквозного аудита кодовой базы.
> **Для исполнителя:** план разбит на самодостаточные фичи-карточки. Каждую можно отдать отдельному
> агенту/сессии. Для пошагового исполнения используйте `superpowers:subagent-driven-development`
> (по фиче-агенту) или `superpowers:executing-plans` (батч с чекпойнтами). Шаги критичных фич
> (A1, A2) даны в TDD-формате с точными путями.
>
> **История предыдущих этапов** (H1, H1.1, W, W2) — в `HANDOFF.md` и git-истории. Этот файл
> перезаписывает прежний H1-план (тот этап закрыт и зафиксирован в HANDOFF §0.1).

**Цель этапа:** превратить «работает, пока площадки не меняют разметку» в «ломается громко и
самолечится». Закрыть прод-баг с битыми ссылками nekretnine, построить систему раннего оповещения
о поломке парсеров, и добавить набор продуктовых/надёжностных улучшений по убыванию ценности.

---

## 0. Контекст и текущее состояние (что уже есть — чтобы не дублировать)

Архитектура (полностью): `docs/SPEC.md`, `docs/web/ARCHITECTURE.md`, `HANDOFF.md`.

**Пайплайн:** Telethon-монитор читает @OtzRentBelgrad → `InboundPost` → matcher (pHash → CLIP → text
cross-check → LLM) → `Match(PENDING)` → доставка в Saved Messages / бот → Дарья постит в @RentBelgrad.
Скраперы (nekretnine.rs httpx+__NEXT_DATA__, 4zida.rs httpx+JSON-LD, halooglasi.com Playwright) наполняют
`Listing`. Публичный сайт `src/site/` (read-only) + админка `src/admin/` (curation).

**Что по надёжности УЖЕ сделано (H1/H1.1):**
- systemd `OnFailure=otz-notify-failure@%n` на всех юнитах + де-дуп алертов (cooldown ~10 мин).
- Ночной `otz nightly-refresh`: full-scrape · stale-sweep (3 дня) · heal битых строк · rolling
  detail-refresh · prune embeddings · `DailyMetrics` snapshot + **алерт при просадке active >20%** ·
  price-drop алерты · бюджет refetch (`NIGHTLY_REFETCH_BUDGET_SEC`).
- Сводка ночного прогона в бот: per-platform previewed/new/coverage%, гард «мёртвая площадка» (0
  previews → 🚨), гард низкого покрытия (<70% → 🚨).
- `photo_downloads.jsonl` + `otz photo-stats`; `llm_decisions.jsonl` + `otz llm-report`.
- Telethon session-revoked → exit 3 + один алерт, без крэш-лупа; QR-реавторизация.
- Фикстур-тесты парсеров (`tests/test_scrapers_*.py`) против сохранённого HTML.

**Чего НЕТ (и закрывается этим планом):**
1. ❌ Самолечения/обновления **stale URL** — re-seen listing не обновляет `url` (→ баг nekretnine 404).
2. ❌ Активного оповещения об **изменении разметки** до того, как сломается весь скрейп. Сейчас узнаём
   косвенно: через просадку active >20% (срабатывает с лагом в дни и только при массовой потере) или
   случайно. Нет live-smoke, нет fingerprint структуры, нет per-run parse-success-rate.
3. ❌ Проверки **живости ссылки** перед показом на сайте; межплощадочного дедупа; подписок на поиск
   (хотя `leads.kind="subscription"` уже зарезервирован в схеме).

---

## 1. Сводная таблица фич

| ID | Фича | Кат. | Приоритет | Сложн. | Оценка |
|----|------|------|-----------|--------|--------|
| **A1** | **Fix nekretnine 404 + самолечение stale-URL** | Надёжность | **P0** | M | 0.5–1 д |
| **A2** | **Защита от смены разметки: live-smoke + fingerprint + алерты** | Надёжность | **P0** | L | 2–3 д |
| A3 | Проверка живости ссылки перед публикацией + dead-link sweep | Надёжность | P1 | M | 1 д |
| A4 | Межплощадочный дедуп listings по pHash | Надёжность | P1 | M | 1 д |
| B1 | Per-parser parse-success-rate метрики + пороговые алерты | Наблюдаемость | P1 | S–M | 0.5 д |
| B2 | Synthetic «golden inbound» сквозной зонд | Наблюдаемость | P2 | M | 1 д |
| B3 | Страница `/health` в админке (статус площадок) | Наблюдаемость | P2 | S | 0.5 д |
| C1 | Бейдж «Снято» + авто-скрытие протухших + fallback фото | UX сайта | P1 | S | 0.5 д |
| C2 | Подписки на поиск (saved search → e-mail/Telegram) | UX сайта | P2 | M | 1–1.5 д |
| C3 | HTTP-кэш/ETag статики + lazy-loading фото | Перф/UX | P2 | S | 0.5 д |
| C4 | CTA «WhatsApp/Viber» + share-кнопки на карточке | UX сайта | P2 | S | 0.25 д |
| D1 | pHash banding-индекс для масштабирования matcher | Перформанс | P2 | L | 2 д |
| E1 | Новая площадка (sasomange.rs / cityexpert.rs) | Охват | P2 | L | 2 д/шт |
| F1 | Воронка лидов + отчёт спроса по районам | Аналитика | P2 | M | 1 д |
| G1 | Drill восстановления бэкапа + `PRAGMA integrity_check` | Эксплуатация | P1 | S | 0.5 д |
| G2 | Хардненинг basic-auth админки (rate-limit, lockout) | Безопасность | P2 | S | 0.25 д |

---

## 2. Порядок внедрения (фазы)

```
Фаза 1 — «Стоп кровотечению» (P0, обязательное)
   A1 → A2          # сначала чиним баг, затем не даём ему повториться незаметно
   (A1 и A2 независимы по файлам — можно параллельно двумя агентами)

Фаза 2 — «Надёжность вокруг данных» (P1)
   A3, A4, B1, G1   # живость ссылок, дедуп, метрики качества парсинга, backup-drill
   (параллелизуемо; A3 зависит от A1)

Фаза 3 — «Продукт и охват» (P2)
   C1 → C2 → C3 → C4   # UX сайта, конверсия
   B2, B3, F1, G2      # наблюдаемость и аналитика
   E1, D1              # охват/перф — по факту роста объёмов

```

**Правила исполнения (как в H1):** миграции Alembic — single-owner, одна ревизия на фичу; субагенты
не делают `git`/`ssh`/правки `.env`/действий на VPS; коммитит и деплоит оркестратор после
подтверждения; gate `.venv/bin/pytest tests/ -q` зелёный после каждой фичи; перед миграцией на
проде — backup БД (`scripts/deploy_sync.sh` + ручной dump).

---

# КАТЕГОРИЯ A — Надёжность скрейпинга и целостность данных

## ⭐ A1 — Fix nekretnine 404 + самолечение stale-URL  **[P0 · M · 0.5–1 д]**

### Проблема (первопричина подтверждена аудитом)
Квартира найдена на nekretnine.rs, но сохранённая ссылка отдаёт 404 («Stranica koju tražite ne
postoji…»). Цепочка:

1. `nekretnine._previews_from_payload` (`src/scrapers/nekretnine.py:369–372`) берёт URL из
   **slug-формы** `seo.url` (`/oglasi/468187/dvosobni-stan-obrenovac/`); numeric-fallback
   `/oglasi/{id}/` используется только если `seo.url` пуст.
2. `orchestrator._process_one` для **уже существующего** listing
   (`src/scrapers/orchestrator.py:181–192`) обновляет только `last_seen_at` (и реактивирует) —
   **`url` НЕ обновляется никогда**.
3. Площадка меняет slug объявления (или реструктуризирует URL). Объявление по-прежнему в выдаче,
   матчится по `external_id` → `last_seen_at` тикает, `is_active=True`. Но в БД лежит **старый
   slug-URL**, который теперь 404.
4. 404 при refetch (`refetch_listing_details`) ловится, но лишь бампит `details_refreshed_at` —
   `is_active` остаётся `True`. Битая ссылка уходит на сайт и в карточку матча.

**Numeric-форма `/oglasi/{id}/` slug-независима и проверенно работает** — фикстура детали
(`tests/test_scrapers_nekretnine.py`) использует именно `https://www.nekretnine.rs/oglasi/468187/`.

### Решение (4 части)
- **A1.1** — nekretnine: всегда хранить **канонический numeric-URL** `{BASE_URL}/oglasi/{id}/`
  (slug в `title`, не в `url`). Иммунитет к дрейфу слага на будущее.
- **A1.2** — orchestrator: в ветке re-seen **обновлять `row.url`**, если он изменился. Лечит
  существующие протухшие строки на следующем walk (preview теперь отдаёт canonical URL).
- **A1.3** — orchestrator: при HTTP **404/410** в `fetch_detail` (refetch/heal) — ставить
  `is_active=False` (а не просто бампить таймер). Подстраховка для реально снятых объявлений.
- **A1.4** — one-off CLI `otz heal-urls`: переписать уже сохранённые nekretnine slug-URL в
  canonical numeric прямо в проде (без ожидания walk).

### Файлы
- Modify: `src/scrapers/nekretnine.py` (`_previews_from_payload`, ~369–382)
- Modify: `src/scrapers/orchestrator.py` (`_process_one` re-seen ветка 181–192; `refetch_listing_details` 462–477)
- Modify: `src/cli.py` (новая команда `heal-urls`)
- Test: `tests/test_scrapers_nekretnine.py`, `tests/test_pipeline.py` (или новый `tests/test_orchestrator_reseen.py`)

### Шаги (TDD)

- [ ] **Шаг 1 — Падающий тест: canonical numeric URL в preview.**
  В `tests/test_scrapers_nekretnine.py`:
  ```python
  def test_parse_list_uses_canonical_numeric_url(nekretnine_list_html: str) -> None:
      previews = NekretnineScraper.parse_list(nekretnine_list_html)
      for p in previews:
          # URL должен быть slug-независимым: /oglasi/<digits>/ и НИЧЕГО после id
          assert re.fullmatch(r"https://www\.nekretnine\.rs/oglasi/\d+/", p.url), p.url
  ```
- [ ] **Шаг 2 — Прогон: FAIL** (`pytest tests/test_scrapers_nekretnine.py -k canonical -v`) — сейчас URL слаговый.
- [ ] **Шаг 3 — Реализация A1.1.** В `_previews_from_payload` заменить блок 369–372:
  ```python
  # Канонический numeric-URL — slug-независим (площадка периодически меняет slug,
  # из-за чего сохранённые slug-URL отдают 404 при живом объявлении). seo.anchor/title
  # держим отдельно как title.
  url = f"{BASE_URL}/oglasi/{external_id}/"
  title = real_estate.get("title") or seo.get("anchor")
  ```
  (Удалить чтение `seo.get("url")` для `url`; `seo` оставить только ради `anchor`.)
- [ ] **Шаг 4 — Прогон: PASS.** Также прогнать существующие тесты файла — `detail.url` остаётся как передан (numeric), не ломается.
- [ ] **Шаг 5 — Падающий тест: re-seen обновляет url.** Новый `tests/test_orchestrator_reseen.py`:
  ```python
  @pytest.mark.asyncio
  async def test_reseen_listing_refreshes_stale_url(seeded_listing):
      # seeded_listing: nekretnine #468187 со СТАРЫМ slug-url в БД
      await _process_one(FakeScraper(), "https://www.nekretnine.rs/oglasi/468187/",
                         "468187", download_photos=False)
      row = await fetch_listing("468187")
      assert row.url == "https://www.nekretnine.rs/oglasi/468187/"  # обновился
  ```
- [ ] **Шаг 6 — Прогон: FAIL.**
- [ ] **Шаг 7 — Реализация A1.2.** В `orchestrator._process_one`, re-seen ветка (после 185):
  ```python
  row.last_seen_at = datetime.now(timezone.utc)
  # Самолечение протухших ссылок: площадка могла сменить slug/URL. preview-URL — источник истины.
  if url and row.url != url:
      row.url = url
  if not row.is_active:
      row.is_active = True
  ```
- [ ] **Шаг 8 — Прогон: PASS.**
- [ ] **Шаг 9 — Падающий тест: 404 на refetch → is_active=False.** В тестах refetch:
  ```python
  @pytest.mark.asyncio
  async def test_refetch_404_marks_inactive(seeded_active_listing):
      # scraper.fetch_detail кидает httpx.HTTPStatusError(404)
      await refetch_listing_details(scraper_raising_404, limit=1)
      row = await fetch_listing(seeded_active_listing.external_id)
      assert row.is_active is False
  ```
- [ ] **Шаг 10 — Прогон: FAIL.**
- [ ] **Шаг 11 — Реализация A1.3.** В `refetch_listing_details` except-блоке (462–477) добавить
  до `touch_on_error`:
  ```python
  except Exception as e:
      stats.errors += 1
      status = getattr(getattr(e, "response", None), "status_code", None)
      if status in (404, 410):
          # Объявление снято с площадки — деактивируем, чтобы не показывать битую ссылку.
          await s.execute(
              update(Listing).where(Listing.id == row.id).values(is_active=False)
          )
          stats.deactivated = getattr(stats, "deactivated", 0) + 1
          logger.info("[refetch] #%s → %s, deactivated", row.id, status)
      elif touch_on_error:
          ...  # существующая логика бампа details_refreshed_at
  ```
  (Добавить поле `deactivated` в dataclass статистики refetch.)
- [ ] **Шаг 12 — Прогон: PASS.**
- [ ] **Шаг 13 — Реализация A1.4: `otz heal-urls`.** В `src/cli.py`:
  ```python
  @app.command("heal-urls")
  def heal_urls(platform: str = "nekretnine", dry_run: bool = True) -> None:
      """Переписать сохранённые slug-URL nekretnine в канонический /oglasi/<id>/."""
      # SELECT id, external_id, url WHERE platform=... AND url NOT LIKE '%/oglasi/<id>/'
      # для nekretnine: new = f"https://www.nekretnine.rs/oglasi/{external_id}/"
      # dry_run → печатать diff; иначе UPDATE. Логировать кол-во.
  ```
- [ ] **Шаг 14 — Тест `heal-urls`** на in-memory БД с парой slug-строк (dry-run печатает, `--no-dry-run` чинит).
- [ ] **Шаг 15 — Полный прогон `pytest tests/ -q` зелёный. Commit:**
  `fix(nekretnine): canonical numeric URLs + self-heal stale URL + 404→inactive`

### Прод-чеклист (оркестратор, после деплоя)
- backup БД → деплой → `otz-bg heal-urls --platform nekretnine --no-dry-run` → проверить
  на 3–5 ранее-битых ссылках, что теперь 200. Следующий nightly walk дочистит остальное через A1.2.

---

## ⭐ A2 — Защита от смены разметки площадок  **[P0 · L · 2–3 д]**

### Проблема
Площадки периодически меняют HTML/JSON-структуру (nekretnine уже мигрировал на Next.js в мае 2026 —
см. memory `nekretnine-nextjs-migration`). Когда парсер ломается, мы узнаём об этом **косвенно и с
лагом**: либо ночной алерт «active просел >20%» (срабатывает только при массовой потере и спустя дни),
либо случайно. Нужно: **узнавать о поломке в течение часов, до того как протухнет вся выдача**, и по
возможности самодиагностировать что именно сломалось.

### Решение — четыре слоя защиты
1. **Live-smoke зонд (`otz scrape-smoke`)** — каждые 6 ч по таймеру: на каждой площадке тянет
   list-страницу, проверяет `≥ MIN_PREVIEWS` превью, берёт **первое живое превью**, делает
   `fetch_detail`, проверяет полный набор полей (price/area/photos/coords/title). Падение →
   ненулевой exit → `OnFailure` алерт. Самовыбор живого объявления (не хардкод id) защищает от
   ложных тревог из-за снятого канарейка-объявления.
2. **Fingerprint структуры** — стабильный отпечаток формы данных (набор ключей в
   `__NEXT_DATA__ results[0]`, ключи `serverListData`, наличие `@type` в JSON-LD 4zida). Храним
   last-known-good в `data/state/parser_fingerprints.json`; **дрейф → WARN-алерт** «структура
   изменилась» (мягче, чем hard-fail; ловит изменения, которые ещё парсятся, но скоро сломают).
3. **Per-run parse-success-rate** (см. B1) — доля деталей, распарсенных с непустым payload. Резкое
   падение rate → алерт «вероятно сменилась разметка». Агрегирует уже существующий сигнал
   «empty detail payload» (`orchestrator.py:196–201`).
4. **Fallback-стратегии** — для nekretnine: если `__NEXT_DATA__` отсутствует/изменился, пробовать
   legacy-селекторы + явный алерт. Для всех: «all-None shell» уже не вставляется (есть guard) —
   агрегируем частоту таких шеллов в метрику слоя 3.

### Файлы
- Create: `src/scrapers/smoke.py` — зонд + fingerprint
- Modify: `src/scrapers/base.py` — добавить `MIN_PREVIEWS`, `fingerprint(payload) -> dict`, `required_detail_fields`
- Modify: каждый из `nekretnine.py` / `cetiri_zida.py` / `halooglasi.py` — реализовать `fingerprint`
- Modify: `src/cli.py` — команда `scrape-smoke`
- Create: `deploy/otz-scrape-smoke.service` + `deploy/otz-scrape-smoke.timer`
- Modify: `scripts/vps_install.sh` — установка таймера + `OnFailure=otz-notify-failure@`
- Create: `tests/test_scrape_smoke.py` — fingerprint/drift логика против фикстур
- Modify: `src/config.py` — `SCRAPE_SMOKE_MIN_PREVIEWS`, `SCRAPE_SMOKE_ENABLED`, путь fingerprint-стейта
- Modify: `docs/PHASE5_DEPLOY.md` — описать таймер и интерпретацию алертов

### Шаги (TDD-каркас)

- [ ] **Шаг 1 — Контракт в `base.py`.** Добавить:
  ```python
  class Scraper(ABC):
      MIN_PREVIEWS: int = 5
      REQUIRED_DETAIL_FIELDS: tuple[str, ...] = ("price_eur", "area_m2", "photo_urls")
      @classmethod
      def fingerprint(cls, list_html_or_payload: str) -> dict[str, Any]:
          """Стабильный отпечаток структуры выдачи (ключи/типы, не значения).
          Default: пусто; каждый парсер переопределяет."""
          return {}
  ```
- [ ] **Шаг 2 — Падающий тест fingerprint (nekretnine).** В `tests/test_scrape_smoke.py`:
  ```python
  def test_nekretnine_fingerprint_stable(nekretnine_list_html):
      fp = NekretnineScraper.fingerprint(nekretnine_list_html)
      assert fp["source"] == "__NEXT_DATA__"
      assert "id" in fp["result_keys"] and "seo" in fp["result_keys"]
  ```
- [ ] **Шаг 3 — FAIL → реализовать `NekretnineScraper.fingerprint`** (вытащить `__NEXT_DATA__`,
  вернуть `{"source": "__NEXT_DATA__", "result_keys": sorted(results[0].keys()), "n": len(results)}`).
  Аналогично для 4zida (JSON-LD `@type`/item-keys) и halooglasi (`serverListData`/`Ads[0]`-keys).
- [ ] **Шаг 4 — Тест drift-детектора.** `compute_drift(old_fp, new_fp) -> list[str]` (пропавшие/новые
  ключи). Тест: удаление ключа `seo` из result_keys → drift непустой.
- [ ] **Шаг 5 — Реализация `smoke.py`:**
  ```python
  @dataclass
  class SmokeResult:
      platform: str; ok: bool; n_previews: int
      detail_ok: bool; missing_fields: list[str]; drift: list[str]; error: str | None

  async def run_smoke(scraper: Scraper) -> SmokeResult:
      # 1) list_recent(max_pages=1) → assert len >= scraper.MIN_PREVIEWS
      # 2) fetch_detail(previews[0].url) → assert все REQUIRED_DETAIL_FIELDS не пусты
      # 3) fingerprint(list_html) vs last-known-good из parser_fingerprints.json → drift
      # ok = (n>=MIN) and detail_ok; drift НЕ роняет ok (только WARN)
  ```
  Загрузка/сохранение `data/state/parser_fingerprints.json` (обновлять good-fingerprint только при `ok`).
- [ ] **Шаг 6 — CLI `scrape-smoke`:**
  ```python
  @app.command("scrape-smoke")
  def scrape_smoke(platform: str | None = None) -> None:
      results = [run_smoke(s) for s in selected_scrapers(platform)]
      # печать таблицы; при любом not ok или drift → бот-нотис (через bot_delivery.send_text_via_bot)
      # exit(1) если есть not ok → systemd OnFailure
  ```
- [ ] **Шаг 7 — systemd unit + timer.** `deploy/otz-scrape-smoke.service` (`ExecStart=… scrape-smoke`,
  `OnFailure=otz-notify-failure@%n.service`), `.timer` `OnCalendar=*-*-* 00/6:30:00` (каждые 6 ч,
  со сдвигом от ночного 03:00). Прописать в `vps_install.sh`.
- [ ] **Шаг 8 — Тесты smoke против фикстур** (мокнутый scraper, отдающий fixture-HTML): ok-кейс,
  too-few-previews-кейс, missing-detail-field-кейс, drift-кейс.
- [ ] **Шаг 9 — Контракт-тест парсеров (CI-гард рефакторингов):** для каждой площадки —
  `parse_detail(fixture)` отдаёт ВСЕ `REQUIRED_DETAIL_FIELDS` непустыми. Ловит регрессии парсера
  на этапе CI, не дожидаясь прода.
- [ ] **Шаг 10 — `pytest tests/ -q` зелёный. Commit:**
  `feat(scrapers): live smoke probe + structure fingerprint + drift alerts`

### Прод-чеклист
- Установить таймер, дождаться первого прогона, убедиться что записался baseline-fingerprint и
  пришёл (или НЕ пришёл — при ok тишина) корректный нотис. Симулировать поломку (временно
  сломать селектор на копии) → проверить алерт.

### Замечания по дизайну
- **Не спамить:** позитивные прогоны молчат; алерт только на not-ok или drift, де-дуп через
  существующий cooldown-механизм notify-failure.
- **Канарейка = первое живое превью**, не хардкод id → нет ложных тревог при снятии конкретного
  объявления.
- halooglasi требует Playwright/`xvfb-run` — smoke для неё тяжелее; вынести в тот же `xvfb-run`
  враппер, что и monitor (см. `PHASE5_DEPLOY §3`).

---

## A3 — Проверка живости ссылки перед публикацией + dead-link sweep  **[P1 · M · 1 д]**

### Проблема
Даже с A1 объявление может быть снято между ночным walk и показом на сайте. Публичная карточка не
должна вести на 404. Нужна лёгкая проверка живости + периодический sweep битых ссылок.

### Решение
- **A3.1** — `otz check-links [--platform X] [--limit N]`: HEAD-запрос по `url` активных listings,
  у которых есть PENDING/PUBLISHED match (т.е. потенциально на сайте). 404/410 → `is_active=False`
  + лог; включить в ночной прогон как отдельный шаг с бюджетом.
- **A3.2** — На сайте: для «Квартиры недели» и featured-карточек — фоновая (не блокирующая рендер)
  проверка не нужна; вместо этого опираемся на `is_active` + бейдж «Снято» (C1). Read-only сайт не
  ходит наружу в visitor-path (ToS + латентность).

### Файлы
- Modify: `src/scrapers/orchestrator.py` (новая `verify_active_links()`)
- Modify: `src/cli.py` (`check-links` + шаг в `nightly-refresh`)
- Test: `tests/test_link_verify.py`

### Шаги
- [ ] Тест: `verify_active_links` помечает `is_active=False` при 404 HEAD, оставляет при 200.
- [ ] Реализация с `httpx.AsyncClient(follow_redirects=True)`, семафор 4, таймаут 10с, только
  listings с match. 301/302 на canonical — это ОК (200 после редиректа).
- [ ] Шаг в `nightly-refresh` под флагом `NIGHTLY_VERIFY_LINKS` (общий бюджет с heal/rolling).
- [ ] Commit: `feat(scrapers): verify active links, deactivate dead listings`

---

## A4 — Межплощадочный дедуп listings по pHash  **[P1 · M · 1 д]**

### Проблема (из `docs/SPEC.md §8`)
Одна квартира публикуется на 3 площадках разными ценами/слагами. Matcher может «найти» её на менее
удачной площадке (битый slug, худшее фото). Дедуп даёт выбор лучшего представителя.

### Решение
- `find_cross_platform_dupes()`: группирует **активные** listings по близости pHash-набора (Hamming
  ≤ порога между ≥2 фото) — переиспользует CPU-скан из `matching/candidates.py`/`matcher.py`.
- Не мёржит автоматически: помечает группу `dup_group_id` (новая nullable-колонка, миграция) +
  выбирает «primary» (живая ссылка, больше фото, есть координаты). Matcher и сайт предпочитают primary.
- Отчёт `otz dupes-report` для ручной проверки.

### Файлы
- Migration: `alembic/versions/00XX_listing_dup_group.py` (`listings.dup_group_id`, `is_primary`)
- Modify: `src/db/models.py`, `src/matching/` (предпочтение primary), `src/cli.py`
- Test: `tests/test_dedup.py`

### Шаги
- [ ] Миграция (single-owner) + тест up/down на копии.
- [ ] Тест: две listings с почти одинаковым pHash-набором → одна группа, выбран primary с большим кол-вом фото.
- [ ] Реализация группировки (asyncio.to_thread, как в matcher).
- [ ] Site/matcher: при равенстве предпочитать `is_primary`.
- [ ] Commit: `feat(matching): cross-platform listing dedup by pHash`

---

# КАТЕГОРИЯ B — Наблюдаемость и алертинг

## B1 — Per-parser parse-success-rate метрики + пороговые алерты  **[P1 · S–M · 0.5 д]**

### Описание
Расширить `DailyMetrics` (или новую `ScrapeRunMetrics`) полями `details_attempted`,
`details_parsed_ok`, `empty_shells`. После каждого скрейпа считать `parse_rate = ok/attempted`;
если `< PARSE_RATE_MIN` (напр. 0.7) при достаточной выборке → 🚨 «парсер деградирует / вероятно
сменилась разметка». Это слой 3 защиты A2, но как часть штатного прогона (не только smoke).

### Файлы
- Modify: `src/scrapers/orchestrator.py` (счётчики в `ScrapeResult`), `src/cli.py` (порог-алерт в nightly),
  опц. миграция для персиста метрик. Test: `tests/test_scrape_metrics.py`.

### Шаги
- [ ] Тест: при 8 ok из 10 → rate 0.8 (нет алерта); 3 из 10 → алерт.
- [ ] Прокинуть счётчики через `ScrapeResult` → строка в ночной сводке + standalone-алерт при просадке.
- [ ] Commit: `feat(scrapers): parse-success-rate metric + degradation alert`

## B2 — Synthetic «golden inbound» сквозной зонд  **[P2 · M · 1 д]**

### Описание
Закрепить «золотой» inbound (известный пост с известным фото, которое ДОЛЖНО матчиться на реальный
listing) и зонд `otz pipeline-probe`: прогоняет matcher на golden inbound, проверяет что матч найден
и confidence ≥ X. Ловит регрессии всего пайплайна (pHash/CLIP/thresholds/БД), а не только парсера.
Запуск по таймеру (раз в день) или вручную после деплоя.

### Файлы
- Create: `tests/fixtures/golden/inbound/` (фото + ожидаемый external_id), `src/jobs/probe.py`, CLI `pipeline-probe`.
- Шаги: зафиксировать golden-пару → зонд сверяет результат matcher → exit/alert. Commit:
  `feat(jobs): synthetic golden-inbound pipeline probe`.

## B3 — Страница `/health` в админке  **[P2 · S · 0.5 д]**

### Описание
В `src/admin/` роут `/health`: таблица по площадкам — last successful scrape, active_count (тренд 7
дней из `DailyMetrics`), last smoke-result, parse-rate, кол-во deactivated за сутки; статус
монитора (last inbound received_at). Один экран «всё ли живо» для Дарьи/оператора.

### Файлы
- Modify: `src/admin/routes.py` (+ `templates/health.html`). Test: `tests/test_admin_health.py`.
- Commit: `feat(admin): /health dashboard (scraper + monitor status)`.

---

# КАТЕГОРИЯ C — UX публичного сайта

## C1 — Бейдж «Снято» + авто-скрытие протухших + fallback фото  **[P1 · S · 0.5 д]**

### Описание
`src/site/format.py:is_rented()` уже определяет «снято» (is_active + текстовые маркеры). Сейчас
rented всегда показываются (`readmodel`: `show_rented` зарезервирован). Сделать:
- Видимый бейдж «Снято» на карточке (каталог + деталь) когда `is_rented`.
- Default-фильтр каталога: rented опускаются вниз/скрываются (тогл «показать снятые»).
- Fallback-изображение когда фото 404 (admin `/photo/...` уже перебирает расширения; на сайте —
  `onerror` → placeholder, чтобы битое фото не ломало сетку).

### Файлы
- Modify: `src/site/readmodel.py` (сортировка/фильтр rented), `templates/*` (бейдж + `<img onerror>`),
  `static/` (placeholder). Test: `tests/site/test_readmodel.py`.
- Commit: `feat(site): rented badge, hide-stale default, photo fallback`.

## C2 — Подписки на поиск (saved search → e-mail/Telegram)  **[P2 · M · 1–1.5 д]**

### Описание
Схема уже готова: `leads.kind="subscription"` зарезервирован. Клиент сохраняет фильтр (район/цена/
комнаты/ВНЖ) → при появлении нового публичного listing под критерии — уведомление на e-mail (Resend,
`src/site/email.py`) или Telegram. Растит удержание и конверсию.

### Файлы
- Migration: таблица `subscriptions` (criteria JSON, contact, channel, is_active) или переиспользовать
  `leads` + новая `subscription_matches`. Modify: `src/site/` (форма подписки на /catalog), новый
  `src/jobs/notify_subscriptions.py` (шаг в nightly: новые listings × активные подписки → нотисы,
  идемпотентно через `subscription_matches`). Test: `tests/test_subscriptions.py`.
- Шаги: миграция → форма+валидация (honeypot+ratelimit, как leads) → matcher подписок → нотисы →
  unsubscribe-ссылка (токен). Commit: `feat(site): saved-search subscriptions with notifications`.

## C3 — HTTP-кэш/ETag статики + lazy-loading фото  **[P2 · S · 0.5 д]**

### Описание
`/vnj`, `/how`, `/calculator` рендерятся заново на каждый запрос. Добавить `ETag`/`Last-Modified`
(+ 304) на статические страницы; `loading="lazy"` + `width/height` на `<img>` в каталоге (CLS/скорость);
`Cache-Control` на `/static` (asset-version уже есть в `app.py`). Низкий риск, заметный эффект.

### Файлы
- Modify: `src/site/routes.py` (ETag middleware/helper), `templates/*`. Test: `tests/site/test_caching.py`.
- Commit: `perf(site): ETag/304 for static pages + lazy images`.

## C4 — CTA «WhatsApp/Viber» + share на карточке  **[P2 · S · 0.25 д]**

### Описание
Кнопка «Написать в Telegram» уже есть (F5/W2). Добавить WhatsApp (`wa.me`) / Viber deep-link и
кнопку «поделиться» (Web Share API + копировать ссылку) на `/apt/{id}`. Конфиг-URL в `config.py`
(`SITE_WHATSAPP_URL`). Без бэкенда.

### Файлы
- Modify: `templates/apt.html`, `src/config.py`, `src/site/app.py` (Jinja global). Commit:
  `feat(site): WhatsApp/Viber CTA + share button`.

---

# КАТЕГОРИЯ D — Производительность

## D1 — pHash banding-индекс для масштабирования matcher  **[P2 · L · 2 д]**

### Описание
Сейчас matcher делает линейный CPU-скан всех active listings на каждый inbound (быстро для ~20–30k,
но O(N) и растёт). При росте до 100k+ внедрить **banding/LSH-индекс** по pHash: разбить 64-битный
хэш на полосы, хранить inverted-index полоса→listings, искать кандидатов по совпадению ≥1 полосы.
Снижает скан до кандидатов-подмножества. Делать **только когда объёмы это оправдают** (YAGNI — сейчас
не нужно; план фиксирует подход).

### Файлы
- Modify: `src/matching/candidates.py` (band-index), опц. таблица `phash_bands`. Test: `tests/test_phash_index.py`.
- Commit: `perf(matching): pHash banding index`.

---

# КАТЕГОРИЯ E — Охват (новые площадки)

## E1 — Новая площадка (sasomange.rs или cityexpert.rs)  **[P2 · L · 2 д/шт]**

### Описание
Больше источников listings → выше шанс матча. `base.Scraper` — чистый интерфейс
(`list_recent`/`fetch_detail`); добавить новый класс + `Platform`-enum + фикстуры + парсер-тесты +
fingerprint (A2) + smoke (A2). cityexpert.rs (агентство, JSON API) и sasomange.rs — кандидаты.
Уточнить у Дарьи приоритет площадки.

### Файлы
- Migration: расширить `Platform` enum (sqlite — проверить хранение enum как string). Create:
  `src/scrapers/<platform>.py`, `tests/fixtures/<platform>/`, `tests/test_scrapers_<platform>.py`.
  Modify: orchestrator (регистрация), config (list-url). Commit: `feat(scrapers): add <platform>`.

---

# КАТЕГОРИЯ F — Аналитика

## F1 — Воронка лидов + отчёт спроса по районам  **[P2 · M · 1 д]**

### Описание
`leads` уже копит заявки (`status`: new/contacted/closed, `kind`, `districts`, `budget_eur`). Дать
Дарье аналитику: воронка new→contacted→closed (конверсия), топ запрашиваемых районов и бюджетных
вилок (несовпадение спроса/предложения = что искать). CLI `otz leads-report` + блок на `/leads` или
`/metrics` админки.

### Файлы
- Modify: `src/admin/routes.py` (агрегаты на `/leads`), `src/cli.py` (`leads-report`). Test:
  `tests/test_leads_report.py`. Commit: `feat(analytics): leads funnel + district demand report`.

---

# КАТЕГОРИЯ G — Эксплуатация и безопасность

## G1 — Drill восстановления бэкапа + integrity check  **[P1 · S · 0.5 д]**

### Описание
Бэкап в B2 есть (`PHASE5_DEPLOY §3`), но **восстановление никогда не проверялось**. Добавить
`scripts/restore_check.sh`: скачать последний бэкап, развернуть в temp, `PRAGMA integrity_check`,
прогнать smoke-запросы (counts). Запускать вручную ежемесячно (или nightly-шаг раз в неделю с
алертом при сбое). Бэкап без проверенного restore = не бэкап.

### Файлы
- Create: `scripts/restore_check.sh`. Modify: `docs/PHASE5_DEPLOY.md`. Commit:
  `chore(ops): backup restore drill + integrity check`.

## G2 — Хардненинг basic-auth админки  **[P2 · S · 0.25 д]**

### Описание
`admin/auth.py` блокирует дефолтный пароль (хорошо), но нет rate-limit/lockout на перебор. Добавить
per-IP счётчик неудачных попыток (как `lead_rate_limiter`) + временный lockout; убрать повторное
логирование critical на каждый запрос (флаг `_default_password_warned` сейчас не гасит повтор).

### Файлы
- Modify: `src/admin/auth.py`. Test: `tests/test_admin_auth.py`. Commit:
  `feat(admin): basic-auth rate-limit + one-time default-password warning`.

---

## 3. Само-ревью плана (по чеклисту writing-plans)

- **Покрытие требований пользователя:** ОБЯЗАТЕЛЬНО-1 (nekretnine 404) → **A1** (первопричина
  подтверждена кодом, 4 части + heal-команда). ОБЯЗАТЕЛЬНО-2 (защита от смены разметки) → **A2**
  (live-smoke + fingerprint + parse-rate + fallback + таймер/алерт), усилено **B1/B2/B3**.
  «Свои идеи» → A3, A4 (надёжность), C1–C4 (UX/конверсия), C2 использует уже зарезервированную
  `leads.kind=subscription`, D1 (перф), E1 (охват), F1 (аналитика), G1/G2 (эксплуатация/безопасность).
- **Без плейсхолдеров:** критичные A1/A2 даны кодом и точными путями/строками; P1/P2 — концепт +
  файлы + тест-точки + commit-сообщение (детализуются агентом при исполнении фичи).
- **Согласованность имён:** `is_active`, `details_refreshed_at`, `_process_one`,
  `refetch_listing_details`, `DailyMetrics`, `leads.kind` — сверены с реальным кодом
  (`orchestrator.py`, `models.py`, `config.py`).
- **Открытый вопрос к Дарье (для E1/C2):** приоритет новой площадки и канал подписок (e-mail vs
  Telegram) — уточнить перед Фазой 3.

---

**Готово к исполнению.** Рекомендуемый старт — Фаза 1 (**A1 → A2**), двумя параллельными агентами
(файлы не пересекаются).

---

# ДОПОЛНЕНИЕ (2026-06-23) — Авто-синхронизация сайта (P0) + решения по Фазе 3

Добавлено по требованию пользователя после старта исполнения. Реализуется поверх Фаз 1–2.

## ⭐ X1 — Автопубликация на сайт при постинге в Telegram **[P0]**

**Проблема:** сегодня квартира появляется на публичном сайте только после ручной курации в админке
(`get_or_create_site_listing` через кнопки `/published`). Нужно: как только матч отправлен ботом в
канал (`Match.status` PENDING→PUBLISHED + создан `PublishedPost`), карточка должна авто-появляться на
сайте.

**Дизайн (по итогам мультиагентного маппинга архитектуры):**
- Общий хелпер `auto_publish_site_listing(session, match_id)` в `src/admin/publish.py` — оборачивает
  идемпотентный `get_or_create_site_listing`, применяет `parse_relocation_flags` только при создании
  (устраняет дублирование с `routes.py:mark_posted_action`). `mark_posted_action` рефакторится на него.
- Хук в `send_match_via_bot` (`src/telegram/bot_delivery.py`, сразу после `m.status` → PUBLISHED) +
  зеркально в userbot-пути (`src/telegram/delivery.py`). Обёрнут в `try/except` + kill-switch
  `auto_publish_on_send` (default True) — **автопубликация не должна ронять доставку**.
- Ночной backfill-шаг `auto_publish_new_listings(since_hours, limit)` (safety-net для матчей до релиза
  или где inline-хук не сработал), за флагом `nightly_auto_publish`.
- Идемпотентность: unique `site_listings.match_id` + `(row, created=False)` гарантируют, что ручные
  правки Дарьи (выключенный `is_public`, флаги, featured, note) НИКОГДА не затираются.

## ⭐ X2 — Автоудаление с сайта при деактивации на площадке **[P0]**

**Проблема:** `readmodel._load_rows` фильтрует только по `is_public` и `is_primary` — НЕ по
`is_active`. Деактивированное объявление (404 на площадке) сохраняет публичную карточку с битой ссылкой.

**Дизайн:**
- Хук в `verify_active_links` (`src/scrapers/orchestrator.py`) — в ТОЙ ЖЕ транзакции, что флипает
  `is_active=False` на 404/410, ставить `is_public=False` (и `is_featured=False`) на `SiteListing`,
  чей `Match.listing_id` == деактивированному. Атомарно, идемпотентно (`WHERE is_public=True`).
- Поле `LinkVerifyStats.site_listings_hidden` + строка в ночной сводке. Алерт при аномально большом
  числе скрытых.
- За флагом `nightly_auto_hide_site_listings` (default True). **Скоуп строго 404/410** — НЕ
  stale-sweep (10-дневная эвристика: сбой скрейпера мог бы массово ложно скрыть каталог).
- Хелпер `hide_site_listings_for_listing(session, listing_id)` — чтобы refetch/stale могли подключиться
  позже без дублирования.
- **Ре-экспозиция не авто:** re-seen → `is_active=True` НЕ возвращает карточку; только ручной
  toggle-public (защита от мигания).
- **Инвалидация readmodel-кэша** при смене `is_public` (кэш каталога не ключуется по is_public —
  иначе изменение видно только после TTL).

**Принятые дефолты (обратимы Дарьей; см. список вопросов в сессии):** автопубликовать все PUBLISHED;
скрывать жёстко только на 404/410; не авто-ре-экспозиция; сбрасывать featured при скрытии; ночной
дайджест вместо пер-итем спама; без гейта по источнику в v1.

## Решения по Фазе 3 (от пользователя)
- **Батч C1, C3, B3, F1, G2, B2** — сделаны все. **C2** — оба канала (e-mail Resend + Telegram).
- **Финал:** полное мультиагентное ревью всего кода (безопасность/перф/корректность/edge-cases) — выполнено.

## Пост-ревью правки (2026-06-23, по запросу пользователя)
- **C4 — WhatsApp/Viber УДАЛЕНЫ.** Единственный мессенджер-CTA — Telegram. Кнопка «Поделиться»
  (Web Share API) оставлена. Конфиг `site_whatsapp_url`/`site_viber_url` и Jinja-глобалы удалены.
- **E1 / sasomange.rs — НЕ делаем, замены нет.** `sasomange.rs/c/stanovi-iznajmljivanje` теперь
  301-редиректит на новостной раздел `biznis.kurir.rs/nekretnine` — каталога аренды там нет.
  Реализован только **cityexpert.rs** (чистый JSON API). sasomange-кода в проекте нет.
