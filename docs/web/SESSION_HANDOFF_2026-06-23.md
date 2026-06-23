# Session handoff — Stage R «Resilience & Reach» (2026-06-23)

> Полный handoff сессии Stage R. `main` @ **`20bd9a8`**, запушено в `origin` (в синхроне).
> Предыдущий handoff: [`SESSION_HANDOFF_2026-06-21.md`](SESSION_HANDOFF_2026-06-21.md) (Stage W2 + админка).
> План этапа: [`../../FEATURE_PLAN.md`](../../FEATURE_PLAN.md) (Stage R + аддендум X1/X2 + пост-ревью правки).
> Память: `~/.claude/projects/-Users-mvstrike-BelgradAppartment/memory/` → `stage-r-progress`, `project-ops`.

---

## 0. TL;DR

- **20 коммитов** (`c55a787..20bd9a8`), **88 файлов, +10867 / −391**, **741 тест зелёный** (было ~207 до Stage W).
- Тема этапа: превратить «работает, пока площадки не меняют разметку» → «ломается громко и самолечится» +
  автоматизация витрины сайта + продуктовые улучшения.
- **2 круга состязательного мультиагентного ревью** — все подтверждённые находки исправлены (вердикт: ship).
- ⚠️ **ВСЁ ЗАПУШЕНО, НО НЕ ЗАДЕПЛОЕНО.** Прод по-прежнему на alembic **0007**, старых юнитах, без новых таймеров.
  Нужны прод-действия (см. §5) — их выполняет человек, агент VPS не трогает.

---

## 1. Что сделано (по фичам)

### Надёжность скрейпинга и целостность данных
| ID | Коммит | Суть |
|----|--------|------|
| **A1** | `1dfd861` | nekretnine: каноничный numeric-URL `/oglasi/<id>/` (slug-независим — лечит баг 404). Re-seen самолечит протухший `url`; refetch 404/410 → `is_active=False`. CLI `heal-urls`. |
| **A2** | `32b22ac` | Live-smoke зонд `scrape-smoke` (таймер 6 ч) + структурный `fingerprint()` на каждый парсер + детектор дрейфа. Baseline в `data/state/parser_fingerprints.json`. halooglasi кладёт page-1 HTML в `_last_list_html`; слой fingerprint best-effort (не роняет зонд). |
| **B1** | `fe8463c` | Per-parser parse-success-rate (`details_attempted/parsed_ok/empty_shells` в `ScrapeResult`) + ночной алерт при деградации. `_EmptyShellError` типизирован. |
| **A3** | `a109f82` | `verify_active_links()` — HEAD-проверка активных листингов с матчем; 404/410 → деактивация (анти-бот 403/405 и 5xx НЕ деактивируют). Пул 4 воркера, общий с heal/rolling бюджет. CLI `check-links` + ночной шаг. |
| **A4** | `21b6fe9` | Миграция **0008** (`listings.dup_group_id` + `is_primary`). `src/matching/dedup.py` — union-find группировка по pHash (≥2 общих фото, Hamming ≤6). CLI `dupes-report` (read-only по умолчанию). Сайт прячет non-primary; матчер предпочитает primary только на точной ничьей. ⚠️ **Дедуп-джоб НЕ подключён в nightly** — запускать вручную `find_cross_platform_dupes(write=True)`. |

### P0 — авто-синхронизация витрины сайта (новые требования середины сессии)
| ID | Коммит | Суть |
|----|--------|------|
| **X1** | `507a7b9` + `3c4e288` + `3a309e7` | **Автопубликация:** при доставке матча в Telegram (PENDING→PUBLISHED) авто-создаётся публичная `SiteListing` (`is_public=True` + флаги ВНЖ/белый-картон). Хук в обоих путях доставки (`bot_delivery`/`delivery`), fault-isolated + kill-switch `AUTO_PUBLISH_ON_SEND`. Ночной backfill `auto_publish_new_listings`. Существующие правки Дарьи не затираются. `mark_posted` отрефакторен на общий хелпер. |
| **X2** | `507a7b9` + `3c4e288` | **Автоскрытие:** когда `verify_active_links` деактивирует листинг по 404/410, та же транзакция прячет его карточку (`is_public=False`, `is_featured` сброшен) через `hide_site_listings_for_listing`. Идемпотентно, kill-switch `NIGHTLY_AUTO_HIDE_SITE_LISTINGS`. **Авто-ре-экспозиции нет** (re-seen не возвращает карточку). readmodel-кэш инвалидируется in-proc; cross-proc — TTL `site_cache_ttl`=300. |

### Наблюдаемость, аналитика, эксплуатация, UX, охват
| ID | Коммит | Суть |
|----|--------|------|
| **G1** | `fbe8e28` | `scripts/restore_check.sh` — drill восстановления бэкапа (B2 → integrity_check → smoke-counts; `RESTORE_CHECK_LOCAL_TARBALL` для прогона без B2). |
| **G2** | `95a4fb8` | Per-IP lockout basic-auth админки (5 фейлов/15 мин → 15 мин cooldown, 429) + one-time default-password warning. |
| **B2** | `35b89fd` | `pipeline-probe` — синтетический golden-inbound зонд (детерминированный, pHash-only, фикстуры `tests/fixtures/golden/`). Ловит регрессии всего матч-пайплайна. |
| **B3** | `91d846c` | Админ-страница `/health`: по площадкам last-scrape / active-count + 7-дн тренд / deactivated-24ч / fingerprint-baseline; монитор-строка (last inbound + staleness). Статусы OK/⚠️/🚨. |
| **F1** | `f65dab1` | `leads-report` + блок на `/leads`: воронка new→contacted→closed + спрос по районам/бюджетным вилкам. |
| **C1** | `b388a54` | Бейдж «Снято» + rented скрыты по умолчанию (тогл «показать снятые», sort-to-bottom) + `<img onerror>` → placeholder. |
| **C3** | `b388a54` | ETag/304 на `/vnj` `/how` `/calculator` + Cache-Control на `/static` + lazy-фото. |
| ~~C4~~ | `b388a54` → `e5bdaf4` | **WhatsApp/Viber УДАЛЕНЫ** (по запросу) — остался Telegram-CTA + кнопка «Поделиться» (share.js). |
| **C2** | `0f4417a` | Миграция **0009** (`subscriptions` + `subscription_matches`). Сохранённый поиск → ночной дайджест при новом матче. `/subscribe` (CSRF+honeypot+ratelimit), `/unsubscribe` по токену. Общий предикат `listing_matches_filters()` для каталога и матчера подписок. E-mail через SMTP. Telegram через config-gated демон `subscription-bot` (**ВЫКЛ по умолчанию**). |
| **E1** | `ef4af25` | **cityexpert.rs** (`src/scrapers/cityexpert.py`) — чистый JSON API (`/api/Search?req=`, `/api/PropertyView/{id}/r`, slug-независимый re-fetch). `Platform.CITYEXPERT` — code-only (колонка VARCHAR, миграция не нужна). |

### Ревью и пост-ревью правки
| Коммит | Суть |
|--------|------|
| `0fe96aa` | **Круг 1:** идемпотентный ledger подписок (`ON CONFLICT DO NOTHING` + внутри fault-isolation), отказ Telegram-перепривязки к чужому чату, инвалидация кэша на toggle-public, `details_refreshed_at` при 404, батч-UPDATE дедупа, rate-limit /unsubscribe, лимит дайджеста 30, server_default в 0009. |
| `20bd9a8` | **Круг 2:** отклонение пустых критериев подписки, `math.isclose` в tie-break матчера (CLIP-скоры — float), CHECK-constraint канал/контакт в `subscriptions`, лог `sub_id` вместо токена, границы списков критериев. |

---

## 2. Новая поверхность (для интеграции/деплоя/следующего плана)

### Новые CLI-команды (`python -m src.cli <cmd>` / `otz <cmd>`)
- `heal-urls [--platform nekretnine] [--dry-run/--no-dry-run]` — переписать slug-URL в canonical.
- `scrape-smoke [--platform X]` — live-зонд (exit 1 при not-ok; алерт на not-ok/drift).
- `check-links [--platform X] [--limit N]` — sweep живости ссылок.
- `dupes-report [--write/--no-write] [--hamming] [--min-shared]` — отчёт межплощадочных дублей.
- `pipeline-probe` — golden-inbound зонд (exit 1 при провале).
- `leads-report [--days N] [--top N]` — воронка лидов + спрос.
- `notify-subscriptions [--since-hours N] [--dry-run]` — рассылка дайджестов подписок.
- `subscription-bot` — long-poll демон Telegram-привязки (gated `SUBSCRIPTION_BOT_ENABLED`).

### Новые env-флаги (`src/config.py`; все с безопасными дефолтами)
```
SCRAPE_SMOKE_ENABLED=1  SCRAPE_SMOKE_MIN_PREVIEWS=5
PARSE_RATE_MIN=0.7  PARSE_RATE_MIN_SAMPLE=10
NIGHTLY_VERIFY_LINKS=1  NIGHTLY_VERIFY_LINKS_LIMIT=1000
AUTO_PUBLISH_ON_SEND=1  NIGHTLY_AUTO_PUBLISH=1          # X1
NIGHTLY_AUTO_HIDE_SITE_LISTINGS=1                       # X2
NIGHTLY_NOTIFY_SUBSCRIPTIONS=1  SUBSCRIPTION_NOTIFY_SINCE_HOURS=24   # C2
DEDUP_MAX_HAMMING=6  DEDUP_MIN_SHARED_PHOTOS=2          # A4
SITE_TELEGRAM_BOT_USERNAME=""   SUBSCRIPTION_BOT_ENABLED=0          # C2 telegram (ВЫКЛ)
CITYEXPERT_LIST_URL=...  CITYEXPERT_API_BASE=https://cityexpert.rs  CITYEXPERT_CITY_ID=1  CITYEXPERT_RESULTS_PER_PAGE=12
```

### Миграции
- **0008** `listing_dup_group` — `listings.dup_group_id` (nullable) + `is_primary` (bool, default 1).
- **0009** `subscriptions` — таблицы `subscriptions` (+CHECK канал/контакт, UNIQUE token) и `subscription_matches`
  (UNIQUE (subscription_id, site_listing_id) — идемпотентность дайджеста). Head = **0009**.

### Новые модули
`src/scrapers/smoke.py`, `src/scrapers/cityexpert.py`, `src/matching/dedup.py`, `src/jobs/probe.py`,
`src/jobs/notify_subscriptions.py`, `src/site/subscriptions.py`, `src/telegram/subscription_bot.py`,
`src/telegram/subscription_delivery.py`, `src/admin/leads_report.py` + шаблоны `health.html`,
`partials/_subscribe_form.html`, `_subscribe_ok.html`, `unsubscribe.html`, `static/js/share.js`, `static/img/placeholder.svg`.

### Новые systemd-юниты (heredoc в `scripts/vps_install.sh`)
- `otz-scrape-smoke.service` + `.timer` (`OnCalendar=*-*-* 00/6:30:00`, `OnFailure=otz-notify-failure@`).
- `otz-subscription-bot.service` — **НЕ enable по умолчанию** (демон подписок).

---

## 3. Изменения поведения, которые надо знать оператору/Дарье
1. **Сайт теперь наполняется автоматически.** Каждый матч, ушедший в Telegram-канал, сам появляется на сайте
   (`is_public=True`). Ручное курирование на `/published` всё ещё работает и **переопределяет** авто-логику
   (toggle-public, флаги, featured — не затираются). Kill-switch: `AUTO_PUBLISH_ON_SEND=0`.
2. **Снятые на площадке (404/410) сами скрываются** с сайта (ночью, после `verify_active_links`). Обратно НЕ
   возвращаются автоматически — только ручным toggle-public.
3. **Каталог по умолчанию прячет «снятые»** (rented) — есть тогл «показать снятые».
4. **Подписки на поиск** — публичная форма на `/catalog`; e-mail работает сразу (нужен SMTP), Telegram-канал
   подписок виден только при заданном `SITE_TELEGRAM_BOT_USERNAME` и требует запущенного демона.
5. WhatsApp/Viber кнопок больше нет — только Telegram + «Поделиться».

---

## 4. Состояние прода и деплой-чеклист (⚠️ НЕ выполнено — за человеком)
Прод сейчас: alembic **0007**, старый `vps_install`, без новых таймеров. Чтобы выкатить Stage R:
1. **Backup БД** (ручной dump + `scripts/deploy_sync.sh`), затем проверить `scripts/restore_check.sh`.
2. Деплой кода → `cd /opt/otz && .venv/bin/alembic upgrade head` (применит **0008 + 0009**).
3. Прогнать `scripts/vps_install.sh` (или вручную доустановить юниты) → `systemctl enable --now otz-scrape-smoke.timer`.
4. Разовый прод-heal A1: `otz-bg heal-urls --platform nekretnine --no-dry-run` → проверить 3-5 ранее-битых ссылок (200).
5. (Опц.) Подключить дедуп A4 в nightly или гонять `dupes-report --write` вручную.
6. (Опц.) Telegram-подписки: задать `SITE_TELEGRAM_BOT_USERNAME` + `SUBSCRIPTION_BOT_ENABLED=1` + `systemctl enable --now otz-subscription-bot`.
7. Дождаться первого `scrape-smoke` → убедиться, что записался baseline-fingerprint и нет ложного алерта.

---

## 5. Открытые пункты / семена для следующего Feature Plan
- **D1 (отложено, YAGNI):** pHash banding/LSH-индекс для матчера — когда объёмы вырастут до 100k+.
- **A4 wiring:** дедуп-джоб не в nightly — решить, подключать ли автоматически и как сайт выбирает primary
  при конфликте с ручной курацией Дарьи (сейчас primary = больше фото/координаты, может разойтись с `is_public`).
- **Вторая новая площадка** вместо sasomange (нерабочей) — кандидат не выбран. cityexpert уже в коде.
- **Telegram-подписки** — демон готов, но не развёрнут; нужен бот-аккаунт + решение об эксплуатации.
- **parse-rate / smoke-result не персистятся** — `/health` показывает что есть; если нужен тренд, нужна таблица.
- **readmodel-кэш cross-process:** инвалидация только in-proc; сайт обновляется по TTL (5 мин). При желании
  мгновенной инвалидации между процессами — нужен сигнал (например, файл-маркер или pub/sub).
- **Авто-ре-экспозиция:** сейчас её нет by design; если Дарья захочет авто-возврат ожившего листинга — фича.
- C2 цифры: подписки используют `subscription_notify_since_hours=24` — на свежем проде без бэкфилла первая
  ночь не разошлёт «весь каталог» (что хорошо), но и старые листинги под критерии не попадут.

---

**Итог:** Stage R закрыт, в `main`, отревьюен (2 круга), но **не задеплоен**. Следующий шаг — прод-выкатка по §4,
затем новый Feature Plan с учётом §5.
