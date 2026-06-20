# otz-rent-finder

Автоматизация поиска квартир для Telegram-канала **@RentBelgrad**.

Полное ТЗ — в [`docs/SPEC.md`](docs/SPEC.md).

## Что это

Сервис, который:
1. Читает Дарьин архив **@OtzRentBelgrad** через Telethon-userbot (туда она копит посты с понравившимися квартирами из других каналов).
2. На каждый новый пост в @OtzRentBelgrad — автоматически обновляет listings на трёх площадках (nekretnine.rs, 4zida.rs, halooglasi.com) с throttle 15 мин.
3. Матчит фото inbound-поста с объявлениями на площадках по pHash.
4. Если нашлось совпадение — присылает получателям в Telegram-бот (`TG_DELIVERY_MODE=bot`) готовый шаблон поста + фото + ссылку на listing на площадке.
5. Дарья проверяет, копирует и сама постит в **@RentBelgrad** (бот в канал НЕ публикует — она контролирует процесс).
6. Веб-админка read-only: просмотр очереди, истории доставки, источников, listings.

Отдельно от пайплайна есть **публичный веб-каталог** (этап W) — см. ниже.

## Установка

Используется [uv](https://docs.astral.sh/uv/) для управления зависимостями.

```bash
# Установить зависимости
uv sync

# Установить Playwright-браузеры (для halooglasi)
uv run playwright install chromium

# Подготовить .env
cp .env.example .env
# заполнить ключи

# Применить миграции
uv run alembic upgrade head

# Первичный скрап всех площадок
uv run otz scrape-all

# Одноразовая авторизация Telethon-сессии
uv run python scripts/auth_telethon.py

# Запустить веб-админку
uv run uvicorn src.admin.app:app --host 127.0.0.1 --port 8000
```

## Структура

```
src/
├── config.py          # pydantic-settings
├── db/                # SQLAlchemy 2.x async + session
├── scrapers/          # nekretnine, 4zida, halooglasi
├── matching/          # pHash + CLIP fallback
├── telegram/          # monitor (Telethon) + publisher (Bot API)
├── post/              # генератор поста по шаблону
├── admin/             # FastAPI + Jinja (+ publish-to-site контролы, этап W)
├── site/              # публичный веб-каталог (FastAPI + Jinja, этап W)
├── jobs/              # pipeline process_new_inbound
└── cli.py             # `otz <command>`
```

## CLI

```bash
# скрейпинг
otz scrape-all              # один проход по всем скраперам (форсированный)
otz refresh                 # то же, но с throttle 15 мин (как при NewMessage trigger)
otz refresh --force         # сбросить throttle marker и запустить скрап

# Telegram + matcher (всё в одном)
otz auth                    # одноразовая авторизация Telethon
otz add-source <url|user>   # зарегистрировать канал-источник
otz monitor                 # backfill watermark + live режим (всегда работает в production)
otz backfill --limit N      # одноразовый backfill истории

# доставка / отладка
otz deliver-all             # повторно отправить все PENDING матчи получателям бота
otz rematch-all --hamming N # сбросить статусы и пересчитать matcher с другим порогом
otz match <inbound_id>      # one-off матчинг конкретного inbound
otz stats                   # текущее состояние БД
otz admin                   # запустить веб-просмотр (FastAPI)
```

## Публичный веб-каталог (этап W)

Второе FastAPI+Jinja-приложение (`src/site/`) — публичный каталог для клиентов @RentBelgrad. Читает
**ту же SQLite read-only** и показывает только **отобранные Дарьей** квартиры (опубличенные в канале),
никогда не линкует на площадки-конкуренты. Главная, каталог с фильтрами (ВНЖ + белый картон на видном
месте), карточка квартиры, карта (Leaflet), калькулятор аренды, форма заявки (→ таблица `leads` + нотис
Дарье через бота), избранное в `localStorage`. На проде слушает `127.0.0.1:8001` за Caddy (авто-TLS);
фото отдаёт сам Caddy из `data/inbound` (см. [`deploy/`](deploy/): `otz-site.service`, `Caddyfile`,
`env.additions.txt`). Обоснование архитектуры — [`docs/web/ARCHITECTURE.md`](docs/web/ARCHITECTURE.md).

```bash
# применить миграции (head 0006 заводит site_listings + leads)
uv run alembic upgrade head

# локальный запуск (хост/порт из SITE_BIND_* в .env, по умолчанию 127.0.0.1:8001)
.venv/bin/uvicorn src.site.app:app --reload --port 8001
# открыть http://127.0.0.1:8001/   (/healthz, /catalog, /calculator, /api/map.json)

# e2e (Playwright против реального uvicorn+chromium) — только под флагом:
RUN_E2E=1 uv run pytest tests/e2e/site -q
```

Новые env-ключи (`SITE_*`) — в [`.env.example`](.env.example) / [`deploy/env.additions.txt`](deploy/env.additions.txt).
Нотис о заявке переиспользует `TG_BOT_TOKEN` / `TG_BOT_RECIPIENTS` (отдельных ключей не нужно).

## Статус

См. план в [`docs/SPEC.md`](docs/SPEC.md), раздел «Этапы разработки».
