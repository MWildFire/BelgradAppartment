# DATA CONTRACT — Stage S «Showcase & Shield»

> Дельта-контракт этапа S, зафиксирован в Wave 0 **до** Wave 1/2 — чтобы backend- и frontend-агенты
> работали параллельно без конфликтов. По привычке Stage W2 в Wave 6 вливается в `DATA_CONTRACT.md`.
> База: `DATA_CONTRACT.md` (+§8 W2). Решения: `DECISIONS-S.md`. Ноль миграций (alembic head остаётся **0009**).

---

## §CSS — Раздел владения CSS/файлами (Wave 1 ∥ Wave 2)

Реальность: один файл `src/site/static/css/site.css`. Чтобы параллельные агенты не конфликтовали:

| Агент | Владеет (пишет) | НЕ трогает |
|---|---|---|
| **W1 frontend** | `templates/partials/_card.html`, `static/css/site.css` (грид каталога + карточка + `.card-cta`), классы-обёртки в `catalog.html`/`index.html` | `apt.html`, `apt.css`, `base.html` |
| **W1 backend/data** | `src/site/format.py`, `data/districts.yaml`, `tests/site/test_format.py` | любой CSS/HTML |
| **W2 backend** | `src/site/maps.py` (новый), `src/post/location.py` (расширение `yandex_maps_url`), `src/site/routes.py` (контекст `/apt`), `src/site/readmodel.py` (coords/maps), `tests/site/test_maps.py` | CSS, `format.py`, `districts.yaml` |
| **W2 frontend** | `templates/apt.html` (блок карты + `{% block head %}` link на apt.css), `static/css/apt.css` (новый), `static/js/apt-map.js` (новый) | `site.css`, `_card.html`, `base.html` |

`base.html` в Wave 1–2 **никто не трогает** (его правит только Wave 5: PWA-manifest). Если задаче нужен `base.html` — эскалация кондуктору.

---

## §M — Карта и deep-links на `/apt/{id}` (Wave 2) — режим **ФУЗЗИ** (D1)

### Контекст `/apt/{id}` пополняется (backend → шаблон)
```python
# routes.py GET /apt/{site_listing_id} добавляет в context:
"coords": {"lat": float, "lon": float} | None   # None если у item нет lat/lon
"maps":   MapsLinks | None                       # None если coords is None
```
Шаблон рендерит блок карты **только если `coords`**. Иначе — fallback (см. ниже).

### Backend-хелпер `src/site/maps.py`
```python
@dataclass(frozen=True)
class MapsLinks:
    yandex: str   # neighborhood-centered, БЕЗ точного пина, порядок LON,LAT
    google: str   # neighborhood-centered, БЕЗ точного пина, порядок LAT,LON

def maps_links(lat: float | None, lon: float | None) -> MapsLinks | None:
    """Фуззи deep-links. None если координат нет."""
```

**Точные URL-шаблоны (ФУЗЗИ — центр района, без `pt`-маркера; порядок координат — частый баг, фиксируем тут):**
- **Yandex** (порядок **долгота,широта**), zoom 15, **без `pt=`**:
  `https://yandex.com/maps/?ll={lon}%2C{lat}&z=15`
  → реюз: расширить `src/post/location.py::yandex_maps_url` параметром `precise: bool = True`.
  При `precise=False` **не добавлять `pt=`** и использовать `z=15`. ТГ-постовый путь (Дарья) остаётся `precise=True` (с пином) — поведение не меняется. `maps_links` зовёт `yandex_maps_url(lat=, lon=, precise=False, zoom=15)`.
- **Google** (порядок **широта,долгота**), центр без пина:
  `https://www.google.com/maps/@{lat},{lon},15z`
  (вид по центру, без точечного маркера — консистентно с фуззи; официальный URL без API-ключа). Реализовать `google_maps_url(lat, lon, *, precise=False)` в `maps.py` (precise-вариант на будущее: `https://www.google.com/maps/search/?api=1&query={lat},{lon}`).

> Почему «центр без пина», а не точка: D1 = фуззи. И встроенная карта (круг ~150 м), и внешние deep-links НЕ должны раскрывать конкретное здание. Поэтому deep-links открывают район по центру координат на zoom 15 без маркера.

### Frontend — встроенная Leaflet-карта (фуззи-круг)
- Блок ~220–280px высотой в `apt.html`, виден только при `coords`.
- **Маркера-точки НЕТ.** Рисуем `L.circle([lat, lon], {radius: 150, ...})` (стиль токенами: заливка оксблад/навигатор с низкой opacity, тонкий stroke). Центр = координаты квартиры.
- Зум: `map.fitBounds(circle.getBounds(), {padding:[20,20]})` — авто-подгон под круг.
- Тайлы: OSM (D2), тот же провайдер, что в `static/js/map.js`. Leaflet — с `cdn.jsdelivr.net` (CSP разрешает), с SRI как в `map.html`.
- **Mobile-gotcha (обязательно):** `scrollWheelZoom: false`; на тач — `dragging: !L.Browser.mobile` ИЛИ overlay-гейт «нажмите, чтобы взаимодействовать» (Leaflet one-finger-drag иначе ловит скролл страницы). `tap`-настройки как в `map.js`.
- **Graceful degradation:** при выкл. JS остаются **текстовые кнопки-ссылки** Yandex/Google (работают без JS, `href` из `maps`). При `coords is None` — блока карты нет вовсе.

### Поведение без координат (`coords is None`)
Карту и кнопки скрываем; показываем: «**Точный адрес уточняйте**» + район (`item.district`) + Telegram-CTA. Без площадочных/адресных утечек.

### ToS-инвариант (жёстко)
Кнопки/ссылки ведут **только на координатную область карты** (Yandex/Google), **НИКОГДА** на объявление площадки или TG-CDN. Это проверяется e2e (`_assert_no_forbidden_urls` уже есть в `tests/e2e/`). В PR-описании Wave 2 — явное напоминание.

### Тесты (TDD)
- `tests/site/test_maps.py`: `maps_links(lat, lon)` → корректные Yandex (LON,LAT, без `pt`) и Google (LAT,LON) URL; `None` при отсутствии координат; `yandex_maps_url(..., precise=True)` всё ещё даёт `pt=` (регресс ТГ-постов).
- e2e (`tests/e2e/site/`): на листинге с координатами (seed id=1) — блок карты есть, 2 кнопки с правильными href, `target=_blank rel=noopener`, ни одной площадочной ссылки; на листинге без координат (seed id=2) — блока карты нет, есть fallback-текст.

---

## §S1-3 — Нормализация района (Wave 1 backend/data)

- `format.py::human_district(raw)` уже маппит raw→кириллица через `data/districts.yaml` (reverse-lookup), незамапленное возвращает как есть.
- Задача: **добить незамапленные латинские районы** в `data/districts.yaml` (напр. `Dorcol`→`Дорћол`, `Profesorska kolonija - Hala Pionir`→человекочитаемое кириллическое, и пр. реально встречающиеся на витрине raw-значения). Снять с витрины латиницу.
- **Опечатки «Воџовац» НЕТ** (уже корректно `Вождовац`) — этой правки не делаем.
- Незамапленное и дальше показывать как есть + логировать (команда `districts-report` уже существует). Не падать.
- TDD: юнит-кейсы латиница→кириллица для новых маппингов + edge (None, пусто, уже-кириллица, незамапленное-passthrough) в `tests/site/test_format.py`.

---

## §IP — Реальный клиентский IP за прокси (контракт для Wave 3a S3-3, фиксируем здесь)

Сейчас IP извлекается как `request.client.host` в `src/site/deps.py:~94` (rate-limit `/lead`,`/subscribe`) и `src/admin/auth.py::_client_key` (G2-lockout). За Caddy (и далее за CF) этого **недостаточно** — все клиенты схлопнутся в один upstream-IP, rate-limit/lockout перестанут различать клиентов.

### Контракт: единый хелпер `client_ip(request) -> str`
Порядок (config-gated):
1. Если `TRUST_CLOUDFLARE` (новый флаг `src/config.py`, **дефолт False**) и есть заголовок `CF-Connecting-IP` → берём его (реальный клиент за CF-edge).
2. Иначе, если запрос пришёл от доверенного апстрима (Caddy на 127.0.0.1) и есть `X-Forwarded-For` → правый-доверенный элемент XFF.
3. Иначе `request.client.host`.

- Используется **обоими**: site (`deps.py` rate-limit lead+subscribe) и admin (`auth.py` G2-lockout). Вынести в общий модуль (напр. `src/site/clientip.py` или расширить `src/config`), переиспользовать в admin.
- Контракт: «реальный клиентский IP, не CF-edge и не Caddy-127.0.0.1».
- **TDD (S3-3):** при `TRUST_CLOUDFLARE=1` + `CF-Connecting-IP: X` → ключ = `X` (не peer, не XFF); при `TRUST_CLOUDFLARE=0` → старое поведение (XFF-from-Caddy / peer); спуфинг `CF-Connecting-IP` при выключенном флаге игнорируется.
- ⚠️ Заметка ревью (L1/L3): убедиться, что `otz-site`/`otz-admin` uvicorn видят реальный XFF от Caddy (proxy-headers), иначе шаг 2 не сработает и текущий rate-limit мог keyиться на 127.0.0.1. S3-3 закрывает всю цепочку.

---

## §0 — Инварианты (повтор из плана, для агентов)
- Публичный app пишет ТОЛЬКО `leads` + `subscriptions`. Ноль площадочных/TG-CDN URL наружу.
- Без нового рантайма/сборки: FastAPI + Jinja + ванильный CSS (custom properties) + минимальный JS. Leaflet уже подключён (CDN+SRI).
- Дизайн-закон: токены/палитра «Belgrade Editorial», цена/площадь/комнаты — `--mono` (JetBrains Mono), анти-slop. Визуальные задачи проходят ревью `taste` + `impeccable`.
- Кэш-бастинг: новые CSS/JS — через `?v={{ asset_v }}` (asset_v = sha1 всех .css/.js, `app.py:_asset_version`).
- Ноль миграций (alembic head = 0009). Колонка → эскалация в Wave 0, не «по-тихому».
- Коммиты: `stage-s: <wave>/<task> — <кратко>`. Full-suite gate (`pytest -q` + `RUN_E2E=1 … tests/e2e -q`) на закрытии волны.
