# ТЗ: Автоматизация поиска квартир для канала @RentBelgrad

**Назначение документа:** входной спек для Claude Code. Описывает архитектуру, модули, контракты данных и план разработки.

> **Каналы.** Дарья публикует кандидатов в свой канал **@RentBelgrad**. Понравившиеся посты из других каналов она копит в архив **@OtzRentBelgrad**. В системе:
> - **@OtzRentBelgrad** — источник (`SourceChannel`, то, что монитор читает).
> - **@RentBelgrad** — таргет публикации (`TG_TARGET_CHANNEL` в .env).

---

## 1. Бизнес-задача

Дарья ведёт ТГ-канал @RentBelgrad. Сейчас она вручную:
1. Мониторит другие ТГ-каналы с квартирами в Белграде и копит понравившиеся посты в свой архив @OtzRentBelgrad.
2. Находит на трёх площадках (nekretnine.rs, 4zida.rs, halooglasi.com) **то же самое объявление**, чтобы убедиться, что цена/площадь корректные и квартира реально существует.
3. Берёт фото из ТГ-канала-источника (без вотермарков площадки), копирует данные с площадки, оформляет пост по шаблону, публикует в @RentBelgrad.

**Цель автоматизации:** сократить ручную работу до одного действия — «опубликовать / отклонить» — на каждый кандидат.

---

## 2. Архитектурное решение

### Формат: Python-сервис на VPS + минимальная веб-админка

**Почему не чистый скрипт:**
- Нужен фоновый мониторинг ТГ-каналов в реальном времени (long-running process).
- Нужна точка ручной модерации (matched apartments queue).
- Нужна история, чтобы не публиковать дубли.

**Почему не SaaS / cloud functions:**
- Telegram user session (Telethon) требует постоянного процесса.
- Scraping три раза в день дешевле и проще на собственном VPS.
- Стоимость: ~$5–7/мес (Hetzner CX11, Timeweb, Beget).

### Компоненты

```
┌─────────────────────┐    ┌─────────────────────────────────┐
│  Telegram monitor   │───▶│   Inbound queue (apartments     │
│  (Telethon, user    │    │   из чужих каналов)             │
│  session)           │    └────────────┬────────────────────┘
└─────────────────────┘                 │
                                        ▼
┌────────────────────────────────────────────────────────┐
│  Matcher: для каждого inbound объявления               │
│  1. Считает pHash всех фото                            │
│  2. Ищет в БД listings с этих 3 площадок               │
│  3. Если матч → создаёт candidate                      │
└────────────────────────┬───────────────────────────────┘
                         │
┌─────────────────────┐  │  ┌─────────────────────────────┐
│  Scrapers           │──┴─▶│  Listings DB                │
│  (3 площадки,       │     │  (фото-hashes, цена,        │
│  раз в N минут)     │     │  площадь, район, ссылка)    │
└─────────────────────┘     └─────────────────────────────┘
                                        │
                                        ▼
                              ┌─────────────────────┐
                              │  Web admin (FastAPI │
                              │  + HTMX/Alpine)     │
                              │  - Очередь матчей   │
                              │  - Превью поста     │
                              │  - Опубликовать /   │
                              │    отклонить        │
                              └──────────┬──────────┘
                                         │
                                         ▼
                              ┌─────────────────────┐
                              │  Publisher          │
                              │  (Bot API → канал)  │
                              └─────────────────────┘
```

---

## 3. Стек технологий

| Слой | Технология | Обоснование |
|---|---|---|
| Язык | Python 3.11+ | Лучшая экосистема для ML/scraping/Telegram |
| Telegram (чтение) | Telethon | Нужен user session — боты не видят чужие каналы |
| Telegram (публикация) | python-telegram-bot или aiogram | Публикация от имени бота-админа канала |
| Scraping | Playwright + httpx | halooglasi и 4zida нагружены JS; nekretnine можно httpx + selectolax |
| Image hashing | `imagehash` (pHash) | Для near-duplicate detection — быстро, ~64 бит на фото |
| Image embeddings (fallback) | `open_clip` (ViT-B/32) | Если pHash промахнулся — семантическое сходство |
| БД | SQLite (на старте) → PostgreSQL | SQLite + WAL хватит на первые месяцы |
| Web admin | FastAPI + Jinja2 + HTMX | Минимум JS, всё рендерится на сервере |
| Очереди | APScheduler | Cron-задачи для скраперов; для MVP не нужен Celery/Redis |
| Хостинг | Hetzner CX22 (€4.5/мес) | Достаточно 2 vCPU / 4 GB RAM |
| Reverse proxy | Caddy | Автоматический HTTPS |
| Process manager | systemd | Стандарт для долгоживущих процессов |

---

## 4. Структура репозитория

```
otz-rent-finder/
├── pyproject.toml
├── README.md
├── .env.example
├── docker-compose.yml          # для локальной разработки
├── alembic/                    # миграции БД
├── src/
│   ├── config.py               # pydantic-settings, читает .env
│   ├── db/
│   │   ├── models.py           # SQLAlchemy 2.x
│   │   └── session.py
│   ├── telegram/
│   │   ├── monitor.py          # Telethon, слушает каналы-источники
│   │   └── publisher.py        # публикация поста в @RentBelgrad
│   ├── scrapers/
│   │   ├── base.py             # абстрактный Scraper
│   │   ├── nekretnine.py
│   │   ├── cetiri_zida.py      # 4zida
│   │   └── halooglasi.py
│   ├── matching/
│   │   ├── phash.py            # perceptual hash + сравнение
│   │   ├── clip_embed.py       # fallback на CLIP
│   │   └── matcher.py          # оркестратор
│   ├── post/
│   │   ├── template.py         # генератор текста по шаблону
│   │   ├── hashtags.py         # маппинг district→hashtag, price→bucket
│   │   └── location.py         # координаты → Yandex Maps URL
│   ├── admin/
│   │   ├── app.py              # FastAPI app
│   │   ├── routes.py
│   │   └── templates/          # Jinja
│   └── jobs/
│       ├── scheduler.py        # APScheduler
│       └── tasks.py            # scrape_all, expire_listings, ...
├── tests/
└── scripts/
    ├── backfill_listings.py    # первичный заполнитель БД
    └── auth_telethon.py        # одноразовая авторизация юзер-сессии
```

---

## 5. Модель данных

```python
# src/db/models.py (упрощённо)

class SourceChannel(Base):
    """Каналы-источники, которые мониторим."""
    id: int
    tg_id: int              # ID канала в Telegram
    username: str           # @some_rent_channel
    is_active: bool

class InboundPost(Base):
    """Сырой пост из канала-источника."""
    id: int
    source_channel_id: int
    tg_message_id: int
    raw_text: str
    photo_paths: list[str]  # локальные пути к скачанным фото
    photo_phashes: list[str]
    received_at: datetime
    status: enum             # NEW | MATCHED | NO_MATCH | DUPLICATE | REJECTED

class Listing(Base):
    """Объявление с одной из 3 площадок."""
    id: int
    platform: enum           # NEKRETNINE | CETIRI_ZIDA | HALOOGLASI
    external_id: str         # ID на платформе
    url: str
    price_eur: int
    area_m2: float
    rooms: float             # 1, 1.5, 2, 2.5...
    district: str            # raw, как на сайте
    address_raw: str | None
    lat: float | None
    lon: float | None
    photo_urls: list[str]
    photo_phashes: list[str]
    scraped_at: datetime
    is_active: bool          # перепроверяем — не снято ли

class Match(Base):
    """Кандидат на публикацию: inbound × listing."""
    id: int
    inbound_post_id: int
    listing_id: int
    confidence: float        # 0..1
    matched_photo_count: int
    created_at: datetime
    status: enum             # PENDING | PUBLISHED | REJECTED
    rejection_reason: str | None

class PublishedPost(Base):
    """История публикаций — для дедупликации и аналитики."""
    id: int
    match_id: int
    tg_message_id: int       # ID в @RentBelgrad (target channel)
    post_text: str
    published_at: datetime
```

---

## 6. Логика модулей

### 6.1 Telegram monitor (`src/telegram/monitor.py`)

- Telethon user session, авторизованная под номером Дарьи или отдельным аккаунтом.
- Подписан на каналы из таблицы `SourceChannel`.
- Обработчик `NewMessage`:
  1. Извлекает текст и media.
  2. Скачивает фото в `data/inbound/<post_id>/`.
  3. Считает pHash каждого фото.
  4. Сохраняет `InboundPost`.
  5. Триггерит `matcher.find_match(inbound_id)`.

**Список каналов-источников** — конфигурируется через админку (на старте пусть будут хардкодом в seed-скрипте, Дарья позже добавит).

### 6.2 Скраперы (`src/scrapers/*`)

Базовый интерфейс:

```python
class Scraper(ABC):
    @abstractmethod
    async def list_recent(self, max_pages: int = 5) -> list[ListingPreview]: ...
    
    @abstractmethod
    async def fetch_detail(self, url: str) -> ListingDetail: ...
```

**Расписание:** каждые 30 минут — `list_recent`, для новых URL — `fetch_detail`. Каждые 6 часов — перепроверка активных listings (если 404 / «продано» → `is_active=False`).

**Что вытаскиваем с каждой площадки:**

- **nekretnine.rs**: достаточно httpx + selectolax. URL в задании уже фильтрует по «vlasnik» (собственник) и цене ≥600. Парсим карточки списка → ссылки → детальная страница.
- **4zida.rs**: использует Next.js. Можно дёргать их же JSON API (видно в Network), либо Playwright. Предпочтительно — найти JSON endpoint, он стабильнее.
- **halooglasi.com**: SSR, но с защитой от ботов (Imperva). Скорее всего нужен Playwright со stealth-плагином и реалистичными задержками. URL в задании содержит `oglasivac_nekretnine_id_l=387237` — это какой-то конкретный риелтор? Уточнить у Дарьи, не ошибка ли — если ошибка, убрать фильтр.

**Антибот:**
- Раз в 30 мин — не агрессивно, не должно ловить капчу.
- User-Agent ротация (fake-useragent).
- Если halooglasi всё-таки банит — proxy через Webshare / Bright Data (~$5/мес за residential pool).

**Координаты:** все три сайта показывают карту → значит координаты есть. Парсим из HTML / JSON (`lat`, `lng` поля).

### 6.3 Матчинг (`src/matching/matcher.py`)

```python
async def find_match(inbound_id: int) -> Match | None:
    inbound = get_inbound(inbound_id)
    
    # 1. Жёсткий фильтр по pHash (Hamming distance ≤ 6)
    candidates = []
    for photo_hash in inbound.photo_phashes:
        listings = db.query_phash_similar(photo_hash, max_hamming=6)
        candidates.extend(listings)
    
    if not candidates:
        # 2. Fallback: парсим цену/площадь из текста inbound regex'ом,
        #    фильтруем listings по price ±0 и area ±5,
        #    считаем CLIP-similarity. Если > 0.9 → матч.
        candidates = clip_fallback(inbound)
    
    # 3. Группируем: сколько фото inbound совпало с одним listing
    best = max_by_matched_photos(candidates)
    if best and best.matched_count >= 2:  # минимум 2 фото совпало
        return create_match(inbound, best, confidence=...)
    
    return None
```

**Параметры порогов** (вынести в config):
- `PHASH_MAX_HAMMING = 6` — для downscaled JPEG'ов
- `MIN_MATCHED_PHOTOS = 2`
- `AREA_TOLERANCE_M2 = 5`
- `CLIP_SIMILARITY_THRESHOLD = 0.90`

### 6.4 Генератор поста (`src/post/`)

**Шаблон** (см. `template.py`):

```python
TEMPLATE = """Цена: {price} евро + залог + комиссия риэлтора 50% 
Площадь: {area} м²  
{availability_line}{location_line}
{hashtags}

Если квартира, которая вам приглянулась, уже сдана или вы не нашли подходящий вариант в нашем канале, напишите @Darya_kralj, и мы обязательно подберем квартиру под ваши персональные предпочтения."""
```

**Поля:**
- `price` — из Listing.
- `area` — целое число из Listing.area_m2.
- `availability_line` — если в тексте inbound есть «свободна с/free from ...» — извлекаем regex'ом, иначе пусто.
- `location_line` — `Локация: https://yandex.com/maps/?ll={lon},{lat}&z=17&pt={lon},{lat}` или, если есть адрес — `?text={url-encoded address}`. Короткие ссылки `yandex.com/maps/-/...` руками не генерируются — это требует UI-действия. На MVP — длинные ссылки. Опционально: интеграция с https://clck.ru/ для укорачивания.
- `hashtags` — собираются из трёх источников:
  - **Ценовая корзина** — `hashtags.py`:
    ```python
    def price_bucket(price: int) -> str:
        for cap in [600, 800, 1000, 1200, 1500, 2000, 3000]:
            if price <= cap: return f"#до{cap}"
        return "#свыше3000"
    ```
  - **Район** — маппинг сербских названий → латиница без диакритик:
    `Звездара → #Zvezdara`, `Савски венац → #Savskivenac`, `Палилула → #Palilula`, `Нови Београд → #NoviBeograd` и т.д. Создать YAML-файл `data/districts.yaml` со всеми 17 общинами Белграда + популярными микрорайонами (Bežanijska kosa, Beograd na vodi, Mirijevo и т.д.) — Дарья дополнит.
  - **Тип квартиры** — по количеству комнат:
    - 1 комната, ≤ 30 м² → `#студия`
    - 1.5 (1 спальня + гостиная) → `#евродвушка`
    - 2.5 → `#евротрешка`

### 6.5 Web admin (`src/admin/`)

Минимальный UI:

- **`/queue`** — список Match со статусом PENDING. Каждый — карточка:
  - Слева: фото из inbound + ссылка на источник.
  - Справа: данные с площадки (цена/площадь/район/ссылка), готовый текст поста в read-only textarea.
  - Кнопки: «Скопировать пост» (JS clipboard), «Отклонить», ссылки «площадка» / «источник».
- **`/published`** — история отправок в Saved Messages.
- **`/sources`** — управление каналами-источниками.
- **`/listings`** — справочник для дебага.

Админка **read-only** — единственная мутация UI это «Отклонить» (REJECTED). Публикацией в канал занимается сама Дарья: она копирует готовый текст из Saved Messages и постит в @RentBelgrad.

**Аутентификация:** basic auth (один логин-пароль для Дарьи). За Caddy + HTTPS на production.

### 6.6 Delivery (`src/telegram/delivery.py`)

Авто-доставка матча в Saved Messages Дарьи через **тот же Telethon-userbot**, что слушает канал-источник. Bot API не используется — Дарья не даёт боту админский доступ к @RentBelgrad.

Когда `process_new_inbound` нашёл матч (`MatchResult.status == "matched"`):
1. Берём фото из `inbound.photo_paths` (макс 10 — лимит media group).
2. Готовим caption: краткий header (платформа + ссылка + сколько фото совпало) + сгенерированный шаблон поста.
3. `await client.send_file(me, file=[photos], caption=text)` — отправляем в Saved Messages.
4. Сохраняем `PublishedPost` (msg_id из Saved Messages для дедупликации), ставим `Match.status = PUBLISHED` (семантически — «доставлено»).

Идемпотентно: повторный вызов на том же match_id возвращает существующий `PublishedPost` без повторной отправки (если не передан `force_resend=True`).

CLI `otz deliver-all` — однократный re-trigger для PENDING матчей (после downtime).

---

## 7. Этапы разработки

### Phase 0 — Подготовка (Дарья + Дмитрий, без кода)
- [x] Получить `api_id`/`api_hash` на my.telegram.org для Telethon.
- [x] Подтвердить роли каналов: @OtzRentBelgrad = источник (там Дарья копит понравившиеся посты), @RentBelgrad = таргет (туда она руками постит готовый шаблон из Saved Messages).
- [x] Подтвердить, что фильтры halooglasi `oglasivac_nekretnine_id_l=387237` / `sprat_order_i_from=8` — это просто базовая ссылка, фильтры убраны.
- [ ] (опц.) Купить VPS, домен — для Phase 5.

### Phase 1 — MVP scraping (1–2 дня в Claude Code)
- [ ] Скаффолд проекта, БД, миграции.
- [ ] Скрапер `nekretnine.rs` (простейший).
- [ ] Скрапер `4zida.rs`.
- [ ] CLI-команда `scrape-all` — наполняет БД.
- [ ] pHash всех фото в БД.

### Phase 2 — Скрапер halooglasi (1 день)
- [ ] Playwright + stealth.
- [ ] Если ловит капчу — добавить proxy.

### Phase 3 — Telegram monitor + matcher (1–2 дня)
- [ ] Telethon auth flow.
- [ ] Monitor пишет InboundPost'ы.
- [ ] Matcher c pHash-only логикой (без CLIP).
- [ ] CLI-тест: «прогон inbound из последних 100 постов чужого канала».

### Phase 4 — Web admin + publisher (1–2 дня)
- [ ] FastAPI + Jinja templates.
- [ ] `/queue` с превью и кнопками.
- [ ] Publisher.

### Phase 5 — Деплой
- [ ] Dockerfile + docker-compose.
- [ ] systemd-юниты.
- [ ] Caddy + HTTPS.
- [ ] Бэкап SQLite раз в день в S3 (Backblaze B2 — копейки).

### Phase 6 — Доработки (по результатам недели работы)
- [ ] CLIP fallback, если pHash промахивается слишком часто.
- [ ] Yandex Maps shortener.
- [ ] Уведомления Дарье в личку, когда появился новый кандидат.
- [ ] Аналитика: % published / rejected, средняя цена и т.д.

---

## 8. Риски и открытые вопросы

| Риск | Митигация |
|---|---|
| halooglasi банит по IP | Residential proxy (~$5/мес) |
| Фото в канале-источнике пересняты / отзеркалены | CLIP-fallback (Phase 6) |
| Дублирование: одна квартира опубликована на 3 площадках разными ценами | Дедупликация listings по pHash *между* площадками |
| Yandex Maps shortlinks | На MVP — длинные URL, потом интеграция с clck.ru |
| Telegram user session «протухает» / просит пароль | Логирование + алерт в личку при FloodWait / SessionRevoked |
| ToS площадок | Не публикуем результаты скрапинга наружу, используем как метаданные → серая зона, не критично |
| Что если в inbound нет фото (только текст)? | Skip — для MVP только photo-based matching |

### Что нужно уточнить у Дарьи перед стартом:

1. **Фильтр halooglasi** — `oglasivac_nekretnine_id_l=387237` это конкретный риелтор или ошибка? Если конкретный — почему только он?
2. **`sprat_order_i_from=8`** в halooglasi URL — фильтр «этаж от 8»? Это намеренно?
3. **Список каналов-источников** — какие именно мониторим?
4. **Что делать с inbound без фото** — игнорировать или сохранять для ручной обработки?
5. **«Без водяных знаков»** — подтвердить, что речь о вотермарках площадки на фото площадки (мы их избегаем, беря фото из ТГ), а не об удалении вотермарков с фото ТГ-каналов.
6. **Бюджет на инфру** — VPS €5/мес ок? Proxy ещё $5 при необходимости.

---

## 9. Что отдать Claude Code на старте

1. Этот документ как `docs/SPEC.md` в репозитории.
2. Команды:
   - «Сделай Phase 1: скаффолд проекта, модели БД, миграции, скрапер nekretnine.rs.»
   - «Покрой scrapers базовыми pytest-тестами на фикстурах сохранённого HTML.»
   - «Сделай Phase 3: monitor + matcher с pHash.»
3. Скаффолд лучше делать с использованием `uv` для зависимостей и `ruff` + `mypy` для качества.
4. Все секреты — в `.env`, никогда в коде.

---

**Готово к передаче в Claude Code.**
