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
├── admin/             # FastAPI + Jinja
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

## Статус

См. план в [`docs/SPEC.md`](docs/SPEC.md), раздел «Этапы разработки».
