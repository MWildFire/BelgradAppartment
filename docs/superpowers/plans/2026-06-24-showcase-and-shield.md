# Stage S — «Showcase & Shield» (Витрина и Щит) — Feature Plan

> Статус: **draft → lock после Wave 0** · Дата: 2026-06-24 · Ветка: **`feature/showcase-and-shield`**
> База: `main` @ **`20bd9a8`** (origin **в синхроне** — caveat «stale origin» из W2/R снят, ветвимся от него).
> Методология: **obra/superpowers** — Conductor + git-worktrees + file-ownership table + gate-протокол (TDD red→green,
> свежий имплементер + состязательный ревьюер на каждую задачу, атомарные коммиты, full-suite gate на каждую волну).
> Предыдущий этап: Stage R «Resilience & Reach» (`SESSION_HANDOFF_2026-06-23.md`).
> Дизайн — источник правды: `design/` + анти-slop-закон (раздел 0 `design/DESIGN_PLAN.md`) + ревью-скиллы `taste`, `impeccable`.
> Этот файл — мастер этапа; по привычке Stage R его можно продвинуть в корневой `FEATURE_PLAN.md`.

---

## Карта запросов → волны

| Запрос Дмитрия | Волна | Суть |
|---|---|---|
| **1.** Кнопка «Оставить заявку» уродливая + карточки неровные | **Wave 1 — Showcase A** | CTA → настоящая кнопка; равная высота карточек; CTA прибит к низу; line-clamp заголовка; нормализация района в кириллицу. |
| **2.** Карта с точкой на карточке квартиры + кнопки Yandex/Google Maps | **Wave 2 — Showcase B** | Leaflet-мини-карта на `/apt/{id}` + deep-links «Открыть в Yandex Maps» / «Открыть в Google Maps», встроено органично. |
| **4.** Cloudflare — доступность с мобильных, любых сетей, любых стран | **Wave 3 — Shield** | CF перед `rentbelgrad.com`: anycast-edge прячет RU-origin, Origin CA cert + Full(Strict), origin за CF-IP allowlist, кэш/WAF, CF-Connecting-IP для rate-limit. |
| **3.** Большое параллельное мультиагентное код-ревью + дизайн-ревью + фиксы | **Wave 4 — Audit** | Веер из ~10 ревью-линз (read-only) → триаж → параллельные фиксы (TDD) → re-review + full-suite gate. |
| Остальные фичи на мой вкус | **Wave 5 — Taste** | PWA+offline+мобильный bottom-nav · динамический OG-share-image · JSON-LD + CF Web Analytics · полировка `/map`. (Stretch: EN-локаль.) |
| — | **Wave 6 — Deploy** | rsync-выкатка, smoke-матрица (вкл. проверку глобальной доступности), доки, мерж. |

**Параллелизм волн:** Wave 1 и Wave 2 идут **параллельно** (разные файлы, см. ownership-таблицы). Wave 3 — частично подготовка агентом (repo/config), частично совместная выкатка (твои руки + раннбук). Wave 4 запускается **после** мержа 1–2 в ветку этапа (чтобы ревьюить итоговый код, включая новые карточки/карту). Wave 5 — после 1–4. Wave 6 — финал.

---

## 0. Контекст и инварианты (для свежей сессии Claude Code)

**Где проект сейчас.** Прод: `root@90.156.254.159:/opt/otz`, `rentbelgrad.com` (Caddy + Let's Encrypt), `admin.rentbelgrad.com` (basic-auth + per-IP lockout G2). Три юнита active: `otz-site` (`127.0.0.1:8001`), `otz-admin` (`:8000`), `otz-monitor`. alembic head **0009**. 741 теста зелёных. ~45 публичных листингов. Сайт **сам наполняется** (X1: матч→Telegram→авто-`SiteListing`; X2: 404/410→авто-скрытие).

**Worktree.** `EnterWorktree` теперь безопасно берёт `origin/main` (в синхроне с local `main` @ `20bd9a8`). **Gotcha сохраняется:** `data/districts.yaml` — gitignored/untracked, копировать в worktree из основного чекаута; в нём же опечатка-алиас **«Воџовац» → «Вождовац»** (чиним в S1-3). `.venv` — симлинк на venv основного чекаута.

**Инварианты этапа (guardrails для всех агентов):**
- **ToS / read-only.** Публичный app пишет ТОЛЬКО `leads` + `subscriptions`. Никаких площадочных/TG-CDN URL наружу. Карта-фича линкует на **координатную точку** Yandex/Google Maps, **НЕ** на объявление площадки. Фото — только локальные `/photos/<relpath>` через Caddy `file_server`.
- **Без нового рантайма/сборки.** Остаёмся на FastAPI + Jinja + **ванильный CSS (custom properties, не Tailwind)** + минимальный JS. Leaflet уже подключён. PWA service-worker и OG-картинка (Pillow) — это всё, никакого Node/npm/Astro (см. ADR `ARCHITECTURE.md`).
- **Дизайн-закон в силе.** Анти-AI-slop (раздел 0 дизайн-плана), палитра/типографика/геометрия «Belgrade Editorial», цена/площадь/комнаты — **моноширинным** (`--mono`, JetBrains Mono). Визуальные задачи проходят ревью-скиллами `taste` + `impeccable`.
- **Admin наружу не выставляется сверх текущего.** За CF — bypass-кэш + (CF Access ИЛИ IP-allowlist) + сохранить app-side basic-auth и G2-lockout.
- **Кэш-бастинг.** Любой новый CSS/JS подключать через существующий `asset_v` (`/static/…?v={{ asset_v }}`). HTML не кэшируется → новые URL ассетов распространяются сразу.
- **Цель по схеме БД: ноль миграций.** Все фичи этапа (карточки/карта/CF/PWA/OG/JSON-LD/i18n) — без изменения схемы. Если какая-то задача потребует колонку — это эскалация в Wave 0, не «по-тихому».
- **Коммиты:** `stage-s: <wave>/<task> — <кратко>`. Full-suite gate (`pytest -q` + `RUN_E2E=1 … tests/e2e -q`) на закрытии каждой волны.

---

## Wave 0 — Spec-check & Decisions (Conductor, без параллельной сборки)

**Цель:** зафиксировать открытые продуктовые решения, подтвердить доступы, выпустить контракт-дельту. Результат — `docs/web/DECISIONS-S.md` + `docs/web/DATA_CONTRACT-S.md`.

### S0-1 — Решения (заполнить `DECISIONS-S.md`)
| # | Вопрос | Дефолт (если Дмитрий не возразит) |
|---|---|---|
| **D1** | Точность точки на `/apt/{id}` | **Точный пин** (консистентно с уже публичным `/map`). Альт: «фуззи» — круг ~150 м без точечного маркера (приватность здания). Текст адреса по-прежнему НЕ показываем (только район). |
| **D2** | Тайлы Leaflet | **OSM standard** (как на `/map`). Если на `/map` уже другой провайдер — переиспользовать тот же, не плодить. |
| **D3** | CF-аккаунт и регистратор | Нужен Cloudflare-аккаунт + доступ к регистратору **Spaceship** для смены NS. Если CF-аккаунта нет — завести (free-плана достаточно). |
| **D4** | Admin за CF | Проксировать `admin.rentbelgrad.com` тоже (одним wildcard Origin-cert), **bypass-кэш** + IP-allowlist/CF Access; basic-auth + G2 остаются. Альт: оставить admin вне CF (grey-cloud) — но тогда origin-IP светится через admin-сабдомен, что обнуляет «сокрытие origin». Дефолт — проксировать. |
| **D5** | EN-локаль (Wave 5 stretch) | **Отложить** полную i18n; в этом этапе только заложить скелет (каталог строк + переключатель), перевод — отдельный этап. RU остаётся дефолтом. |

### S0-2 — Диагностика реальной недоступности (де-риск Wave 3)
Перед CF — зафиксировать, ЧТО именно ломается «с мобильных/из других стран», чтобы CF был обоснованным фиксом, а не догадкой. Прогнать с мобильного (не-RU SIM) и/или через прокси-локации: `dig rentbelgrad.com`, `curl -I https://rentbelgrad.com`, проверить TLS-цепочку, таймауты, наличие RKN/AS-блокировок origin-подсети `90.156.254.0/24`. Записать вывод в `DECISIONS-S.md`. **Ожидание:** RU-origin → переменная доступность из части сетей; CF-anycast это закрывает. Но проверяем фактически.

### S0-3 — Контракт-дельта (`DATA_CONTRACT-S.md`)
Зафиксировать сейчас, до Wave 1/2, чтобы backend- и frontend-агенты не пересеклись:
- **`/apt/{id}` контекст пополняется:** `maps: MapsLinks | None` (см. ниже) + `coords: {lat, lon} | None`. Шаблон рендерит мини-карту только если `coords`.
- **Новый backend-хелпер** `src/site/maps.py::maps_links(lat, lon) -> MapsLinks` (или расширить `src/post/location.py`):
  ```python
  @dataclass(frozen=True)
  class MapsLinks:
      yandex: str   # точка, порядок LON,LAT
      google: str   # точка, порядок LAT,LON
  ```
  **Точные шаблоны URL (порядок координат — частый баг, фиксируем тут):**
  - Yandex (**долгота,широта**): `https://yandex.ru/maps/?ll={lon}%2C{lat}&z=17&pt={lon}%2C{lat}` (для не-RU можно `yandex.com`). В `src/post/location.py` уже есть Yandex-builder для ТГ-постов — переиспользовать, не дублировать.
  - Google (**широта,долгота**): `https://www.google.com/maps/search/?api=1&query={lat}%2C{lon}` (официальный, без API-ключа).
- **Поведение без координат** (`coords is None`): карту и точечные ссылки скрываем; показываем «Точный адрес уточняйте» + район + Telegram-CTA.
- **IP за прокси** (для Wave 3, но контракт фиксируем здесь): функция извлечения клиентского IP читает **`CF-Connecting-IP`** (иначе `X-Forwarded-For` от доверенного Caddy→CF), config-gated `TRUST_CLOUDFLARE=1`. Затрагивает rate-limit `/lead` + `/subscribe` и G2-lockout админки. Контракт: «реальный клиентский IP, не CF-edge».

**DoD Wave 0:** `DECISIONS-S.md` + `DATA_CONTRACT-S.md` закоммичены; D1/D3 подтверждены Дмитрием (остальное — дефолты); диагностика S0-2 записана.

---

## Wave 1 — Showcase A: карточки + CTA (визуал) [запрос #1]

**Проблема (со скриншота):** «Оставить заявку на эту квартиру» — **голый текст-ссылка**, не кнопка (уродливо), и он **вытекает за пределы** белого контейнера карточки на бежевый фон. Карточки **неровные**: заголовок в 1 vs 2 строки даёт разную высоту, CTA «плавает» на разной вертикали, грид не выровнен. Плюс районы показаны разнобоем — часть кириллицей («Стари град»), часть латиницей («Profesorska kolonija», «Dorcol») — нарушение нормализации из `DATA_CONTRACT §5`.

### Ownership-таблица
| Агент | Владеет файлами |
|---|---|
| **frontend** | `src/site/templates/partials/_card.html`, `src/site/static/css/*` (грид каталога + карточка + кнопка CTA), точечно `catalog.html`/`index.html` (классы-обёртки) |
| **backend/data** | `src/site/format.py` (district-нормализация), `data/districts.yaml` (маппинг + опечатка), `tests/site/test_format.py` |

(Файлы не пересекаются → агенты идут параллельно внутри волны. Если оба касаются одного css-файла — frontend владеет всем CSS; backend CSS не трогает.)

### Задачи
- **S1-1 (frontend) — CTA → настоящая кнопка.** «Оставить заявку на эту квартиру» внутри карточки, стилизована токенами (заливка/outline по дизайн-системе), состояния `:hover/:focus-visible/:active`, видимый focus-ring (a11y), вся карточка-ссылка vs кнопка — развести клик-таргеты (кнопка ведёт на `/apt/{id}#lead` или на per-listing форму). Не вытекает за контейнер. TDD: e2e-проверка, что CTA внутри bounding-box карточки и кликабельна.
- **S1-2 (frontend) — равная высота + прибитый CTA.** Грид карточек: одинаковая высота строки (CSS grid + flex-колонка внутри карточки, `margin-top:auto` на CTA). Заголовок — `line-clamp: 2` с фикс. `min-height` под 2 строки, чтобы 1- и 2-строчные карточки совпадали. Фото — фикс. `aspect-ratio` (помнить баг Stage R: `display:block` на photo-ссылке, иначе `aspect-ratio` игнорируется). TDD/e2e: на наборе с 1- и 2-строчными заголовками bottom-кромки карточек и Y-координаты CTA совпадают; ноль горизонтального overflow на 320–1920px.
- **S1-3 (backend/data) — нормализация района в кириллицу.** Незамапленные латинские районы («Dorcol»→«Дорћол», «Profesorska kolonija - Hala Pionir»→человекочитаемо) добить в `data/districts.yaml`; починить опечатку **«Воџовац»→«Вождовац»**. `format.py`: незамапленное — логировать в `districts-report` (команда уже есть), показывать как есть. TDD: юнит-маппинг латиница→кириллица + покрытие edge-кейсов.

**Дизайн-ограничения:** токены/палитра/моно-цена в силе; анти-slop; финальный визуал — ревью `taste` + `impeccable` (часть гейта задачи).

**DoD Wave 1:** карточки выровнены (равная высота, CTA-кнопка в низу, line-clamp), CTA — стилизованная кнопка с состояниями и focus-ring, ноль overflow 320–1920px, районы кириллицей (незамапленные залогированы), `taste`/`impeccable` — pass, новые e2e зелёные, full-suite gate.

---

## Wave 2 — Showcase B: карта на карточке квартиры + deep-links [запрос #2]

**Цель:** на `/apt/{id}` — компактная Leaflet-карта с одной точкой на координатах квартиры + органичные кнопки «Открыть в Yandex Maps» / «Открыть в Google Maps». Данные уже есть (`SiteListingView.lat/lon`); добавляем рендер + deep-links.

### Ownership-таблица
| Агент | Владеет файлами |
|---|---|
| **backend** | `src/site/maps.py` (новый) или правка `src/post/location.py`, `src/site/routes.py` (контекст `/apt/{id}`), `src/site/readmodel.py` (прокинуть coords/maps), `tests/site/test_maps.py` |
| **frontend** | `src/site/templates/apt.html` (блок карты), `src/site/static/js/apt-map.js` (новый), `src/site/static/css/*` (apt-секция, **не** грид каталога из Wave 1 — разнести по секциям/файлам, чтобы не конфликтовать с frontend-агентом Wave 1; волны 1 и 2 идут параллельно — согласовать: один CSS-файл для каталога, отдельный для apt) |

### Задачи
- **S2-1 (backend) — `maps_links(lat, lon)`.** Реализовать по контракту S0-3 (точные URL-шаблоны, порядок LON/LAT для Yandex и LAT/LON для Google). Переиспользовать Yandex-builder из `src/post/location.py`. Прокинуть `maps`/`coords` в контекст `/apt/{id}`. `None`, если координат нет. TDD: юнит на оба URL + порядок координат + None-кейс.
- **S2-2 (frontend) — Leaflet-мини-карта.** В `apt.html` блок ~220–280px высотой, центр = пин квартиры, один маркер, фикс. зум (~16–17). **Mobile-gotcha (обязательно):** `scrollWheelZoom: false` (не перехватывать скролл страницы); на тач — либо `dragging: !L.Browser.mobile`, либо overlay-гейт «нажмите, чтобы взаимодействовать» (Leaflet one-finger-drag иначе ловит скролл). Тайлы — из D2. Graceful-degradation: при `coords is None` блок карты скрыт; при выкл. JS остаются текстовые ссылки-кнопки (работают без JS). TDD/e2e: карта рендерится с правильным маркером; при None — блока нет, есть fallback-текст.
- **S2-3 (frontend) — кнопки Yandex/Google.** Под картой, органично в дизайн-систему (вторичные/outline кнопки, иконки опц.), `target="_blank" rel="noopener"`. ToS-напоминание в PR-описании: ссылки ведут на **координатную точку карты**, не на объявление площадки. TDD/e2e: href совпадают с `maps.yandex`/`maps.google`, открываются в новой вкладке.

**DoD Wave 2:** на `/apt/{id}` с координатами — карта с точкой + 2 рабочие кнопки; без координат — корректный fallback; mobile-скролл не залипает; ToS-инвариант соблюдён (ни одной площадочной ссылки); e2e зелёные; full-suite gate.

---

## Wave 3 — Shield: Cloudflare global edge [запрос #4]

**Зачем CF:** RU-origin (`90.156.254.0/24`) → переменная доступность из части мобильных сетей/стран. CF anycast-edge: клиент коннектится к ближайшему PoP (доступен ~отовсюду), origin-IP скрыт, TLS на edge, кэш, DDoS-защита. Это **и есть** фикс «доступно с любых сетей/стран».

Волна состоит из **(3a) подготовки в репозитории** (делает агент заранее) и **(3b) живой выкатки по раннбуку** (твои руки, каждый шаг подтверждается).

### 3a — Подготовка (agent-doable, в ветке)
| Задача | Файлы |
|---|---|
| **S3-1** Caddy → **Origin CA cert mode** для проксируемых хостов. Убрать зависимость от публичного ACME (HTTP-01 ломается под CF-прокси). Per-site `tls /etc/caddy/origin/cert.pem /etc/caddy/origin/key.pem`. | `deploy/Caddyfile`, `deploy/cloudflare/README.md` |
| **S3-2** Скрипт **origin-firewall = только CF-IP** на 80/443 (`ufw allow from <CF ranges>`, deny остального), тянет актуальные диапазоны с cloudflare.com/ips. | `scripts/cf_ufw_allowlist.sh` |
| **S3-3** **CF-Connecting-IP** в извлечении клиентского IP (по контракту S0-3): rate-limit `/lead`+`/subscribe`, G2-lockout админки, логи. Config-gated `TRUST_CLOUDFLARE`. **Критично:** без этого все клиенты схлопываются в один CF-IP-бакет → rate-limit и lockout не работают. | `src/site/*.py` (extract-ip), `src/admin/*` (G2), `src/config.py`, тесты |
| **S3-4** Документация выкатки + cache/WAF-политика. | `deploy/cloudflare/RUNBOOK.md` |

TDD для S3-3: при `TRUST_CLOUDFLARE=1` и заголовке `CF-Connecting-IP: X` — rate-limit/lockout кейятся на `X`, не на peer; при выкл. — старое поведение.

### 3b — Живая выкатка (раннбук, совместно, каждый ssh/шаг подтверждается)
1. **Origin CA cert.** CF → SSL/TLS → Origin Server → Create Certificate (`rentbelgrad.com` + `*.rentbelgrad.com`). Положить cert/key на VPS (`chmod 600`), переключить Caddy на них (S3-1), `caddy reload`, проверить локально `curl -I https://127.0.0.1` через Host-заголовок.
2. **Домен в CF.** Add site `rentbelgrad.com` (free). CF выдаст 2 nameserver'а.
3. **Смена NS у Spaceship** на CF-NS. Дождаться `Active` (минуты–часы). ⚠️ Точка невозврата по DNS — но откат = вернуть старые NS.
4. **DNS-записи в CF:** A `rentbelgrad.com` → `90.156.254.159` **оранжевое облако (proxied)**; A `admin` → тот же IP, **proxied** (D4). SSL/TLS mode = **Full (Strict)** (Origin-cert доверен CF в Strict).
5. **Edge-настройки:** Always Use HTTPS = on; Min TLS 1.2; Brotli on; HTTP/3 on; HSTS — включить **после** проверки (осторожно, необратимо на max-age).
6. **Cache Rules:**
   - `/static/*`, `/photos/*` → **Cache Everything**, длинный Edge TTL (ассеты фингерпринтованы `asset_v`; фото иммутабельны) — уважать origin `Cache-Control`.
   - HTML (`/`, `/catalog`, `/apt/*`, `/vnj`, `/how`, `/calculator`) → **Bypass** (featured/leads-динамика; в app уже TTL-кэш). `/api/map.json` → короткий edge-TTL (~60с) ок.
   - `admin.rentbelgrad.com/*` → **Bypass cache** + (CF Access ИЛИ WAF IP-allowlist). `POST /lead`, `POST /subscribe`, `/healthz` → не кэшировать.
7. **Origin lock.** Применить `scripts/cf_ufw_allowlist.sh` — 80/443 только от CF-диапазонов (прямой заход на IP блокируется → origin спрятан).
8. **CF Web Analytics** = on (бесплатно, cookieless → баннер cookie не нужен, совпадает с «без аккаунтов»).
9. **Проверка глобальной доступности:** с не-RU мобильной сети и/или через прокси-локации — `https://rentbelgrad.com` = 200, TLS-цепочка ок, OG-превью, формы. `dig` показывает CF-IP, не origin. Прямой `curl` на `90.156.254.159:443` снаружи — **отказ** (lock работает).

**Откат:** grey-cloud (снять прокси) → трафик идёт мимо CF; либо вернуть NS у Spaceship. Origin-lock временно ослабить, если нужно отладить напрямую.

**DoD Wave 3:** `rentbelgrad.com` за CF (Full Strict, Origin-cert), origin заблокирован на не-CF, rate-limit/lockout кейются на реальном IP (S3-3 верифицирован), admin не кэшируется и закрыт, глобальная доступность подтверждена с внешней сети, CF Web Analytics включена.

---

## Wave 4 — Большое параллельное код+дизайн-ревью [запрос #3]

**Подход (superpowers):** веер из ~10 **свежих read-only ревьюеров**, каждый со своей линзой, по всему проекту (не только новый код). Каждый пишет findings-файл → Conductor сводит в единый триаж → параллельные fix-агенты по ownership-таблице (TDD) → состязательный re-review + full-suite gate. Вердикт этапа: **ship**.

### 4a — Веер ревью (параллельно, read-only, каждый → свой файл в `docs/review/stage-s/`)
| Линза | Фокус |
|---|---|
| **L1 Security** | CSRF на всех POST, honeypot, basic-auth/G2, секреты не в логах/гите, заголовки (CSP/anti-clickjacking), SSRF/инъекции. |
| **L2 Data-integrity / ToS** | read-only-изоляция публичного app; ни одной площадочной/TG-CDN ссылки наружу; **heal-urls guard + 8442 до-Next.js-трупов** (§5 R-handoff: только `is_active=1` + проверка резолва canonical; one-off чистка/архив). |
| **L3 Concurrency / SQLite** | WAL, busy_timeout, scoped-writer только `leads`/`subscriptions`, гонки дедупа/доставки, readmodel-кэш cross-proc. |
| **L4 Scraper-resilience** | fingerprint/drift, parse-rate-алерты, cityexpert, sasomange-замена, дедуп A4 (не в nightly — решить). |
| **L5 Frontend a11y** | WCAG AA: контраст, focus-visible, клавиатура, alt, тач-таргеты ≥44px, формы/лейблы. |
| **L6 Design-fidelity / анти-slop** | соответствие `design/` + раздел 0 анти-slop + `taste`/`impeccable`: типографика, моно-цена, ритм, отсутствие «AI-шаблонности». |
| **L7 Responsive** | 320–1920px ноль overflow на всех страницах (в т.ч. новые карточки/карта/PWA-nav). |
| **L8 Performance / caching** | `asset_v`, lazy-фото, ETag/304, CF-кэш-правила, размер бандла, N+1 в readmodel, Lighthouse mobile. |
| **L9 Deps / supply-chain** | пины версий, аудит уязвимостей, лишние зависимости. |
| **L10 Ops / deploy / tests** | systemd-юниты, OnFailure-storm-модель, бэкап/restore-drill, покрытие тестами (дыры), флакость e2e. |

### 4b — Триаж (Conductor → `docs/review/stage-s/REVIEW_FINDINGS.md`)
Свести все findings в одну таблицу: **blocker / should / nice**, дедуп, владелец-файл, оценка. Blocker'ы чинятся в этом этапе обязательно; nice — в backlog (семена для следующего плана).

### 4c — Параллельные фиксы (ownership-таблица над findings, TDD red→green)
Группировать фиксы по непересекающимся файлам → fan-out fix-агентов. Каждый фикс: репро-тест (red) → фикс (green) → состязательное ре-ревью → атомарный коммит.

### 4d — Re-review + gate
Второй круг состязательного ревью (как 2 круга в Stage R), все подтверждённые blocker'ы закрыты, full-suite зелёный.

**DoD Wave 4:** `REVIEW_FINDINGS.md` с триажем; все blocker'ы исправлены и покрыты тестами; 2-й круг ревью — вердикт ship; full-suite gate; nice-to-have перенесены в §«семена».

---

## Wave 5 — Фичи на мой вкус (curated)

Подобрано под продукт (каталог аренды для релокантов), тему этапа (полировка + охват) и инварианты (read-only, без аккаунтов, mobile-first, без нового рантайма). **Committed** — делаем; **stretch** — если влезает в бюджет волны.

- **S5-1 (committed) — PWA + offline-shell + мобильный bottom-nav.** `manifest.webmanifest` (имя/иконки/тема из токенов, `display: standalone`), `sw.js` (cache-first для статики, network-first для HTML, offline-fallback-страница), iOS add-to-homescreen (`apple-touch-icon` + мета). Bottom-nav (Каталог/Карта/Калькулятор/Telegram) ≤ брейкпоинт — усиливает Wave 1 responsive и тему «mobile из любой сети». Чисто ванильный JS. **Взаимодействие с CF/asset_v:** SW кэширует по URL; фингерпринтованные ассеты безопасны; HTML network-first → featured обновляется.
- **S5-2 (committed) — динамический OG-share-image на `/apt/{id}`.** Pillow-композиция 1200×630 (первое фото + цена + район + бейдж ВНЖ + вордмарк), отдаётся на `/apt/{id}/og.png`, кэшируется CF + ETag (или пре-генерация при публикации). OG-мета указывает на него → богатые превью при шеринге ссылки в Telegram/WhatsApp. Без браузера/Node — Pillow уже в стеке (image-hashing). Приватность: район, без улицы (консистентно D1).
- **S5-3 (committed) — JSON-LD + CF Web Analytics.** Structured data (`Apartment`/`Residence` + `Offer` с ценой EUR/availability; `Organization`/`WebSite`) на релевантных страницах — rich-results, в пару к уже готовым sitemap/robots/OG. Без PII/улицы. CF Web Analytics включается в Wave 3 (cookieless) — здесь только сослаться.
- **S5-4 (committed, small) — полировка `/map`.** Цвет маркеров по цено-бакету + тогл-фильтр «ВНЖ» прямо на карте — связывает новую per-card карту (Wave 2) с глобальной. Малый объём, тот же Leaflet.
- **S5-5 (stretch) — EN-локаль (RU default + EN-переключатель).** Минимальный i18n: каталог строк (`data/i18n/{ru,en}.yaml`), язык через cookie/префикс, RU по умолчанию. Большая поверхность (каждая строка шаблона) — i18n исторически отложен (ADR). В этом этапе: **только скелет + переключатель**; полный перевод — отдельный этап, если Дмитрий не скажет «делаем целиком». Усиливает «reach» (релоканты часто читают EN).

**DoD Wave 5:** PWA устанавливается (Android+iOS), офлайн-shell работает, bottom-nav на мобиле; OG-картинка отдаётся и кэшируется, превью богатое; JSON-LD валиден (Rich Results Test); `/map`-маркеры по бакетам + ВНЖ-тогл; (stretch) EN-скелет переключается. Всё — без миграций, e2e зелёные, full-suite gate.

---

## Wave 6 — Deploy + docs + merge

1. **Локально зелено:** `pytest -q` + `RUN_E2E=1 … tests/e2e -q`.
2. **Бэкап БД** на VPS: `sqlite3 .backup` → `data/backups/otz-pre-stageS-$(date +%F-%H%M).db`, `integrity_check=ok`, записать дорелизные счётчики.
3. **Деплой:** `scripts/deploy_sync.sh` (rsync; `.venv`/`data`/`.env` исключены). **Миграций нет** (если Wave-задачи не завели схему — проверить alembic head всё ещё **0009**). Новые пакеты в venv: Pillow если ещё нет (S5-2) — `pip install pillow`.
4. **Рестарт** `otz-site`/`otz-admin` (monitor не трогаем без нужды), чистый старт в логах.
5. **CF-выкатка** — по Wave 3 (если ещё не сделана), на уже зелёном origin.
6. **Smoke-матрица:** карточки выровнены + CTA-кнопка; `/apt/{id}` карта+пин+кнопки Yandex/Google (и без-координат fallback); **глобальная доступность** через прокси/мобилу (200, CF-IP в `dig`, прямой заход на origin-IP заблокирован); OG-превью в Telegram; PWA install + offline; bottom-nav; `/map`-маркеры; `/health` = 401 (не 500); rate-limit/lockout на реальном IP; admin не кэшируется и закрыт; `otz-monitor NRestarts=0`.
7. **Доки (W*-4 паттерн):** `docs/SPEC.md` (Stage S в этапы), `SESSION_HANDOFF_2026-06-24.md`, влить `DATA_CONTRACT-S.md` → `DATA_CONTRACT.md`, `DECISIONS-S.md`, обновить `README.md`, `.env.example` (новые `TRUST_CLOUDFLARE` и пр.).
8. **Мерж:** `finishing-a-development-branch` — `feature/showcase-and-shield` → `main` (ff), push в origin, чистка worktrees.

**Rollback по слоям:** код = `git revert` + `deploy_sync.sh`; CF = grey-cloud/откат NS; origin-lock = временно ослабить ufw; e-mail/leads уже non-fatal. Схема — миграций нет, откатывать нечего.

---

## Риски и митигация

| Риск | Митигация |
|---|---|
| CF-прокси ломает ACME Caddy | **Origin CA cert + Full(Strict)** (S3-1) — без публичного ACME для проксируемых хостов. |
| Rate-limit/lockout схлопываются за CF | **CF-Connecting-IP** (S3-3) — обязательная задача, с тестом. |
| Точный пин раскрывает здание | D1: дефолт = точный (как уже на `/map`); альт «фуззи-круг ~150 м» — решение Дмитрия в Wave 0. |
| Leaflet ловит тач-скролл на мобиле | `scrollWheelZoom:false` + dragging-гейт/overlay (S2-2). |
| Латинские районы на витрине | S1-3 нормализация + опечатка «Воџовац». |
| Origin светится через admin-сабдомен | D4: admin тоже proxied + IP-allowlist/Access. |
| Дедуп A4 расходится с ручной курацией Дарьи | L4 в ревью: решить wiring и выбор primary. |
| SW кэширует устаревший HTML | network-first для HTML, фингерпринт ассетов (S5-1). |
| Скоуп Wave 4/5 раздувается | blocker-only в этом этапе; should/nice → семена. EN-локаль = stretch. |

---

## Что мне нужно от тебя (человеческие руки)

1. **Cloudflare + Spaceship.** Подтвердить, что есть CF-аккаунт (или заведём free) и доступ к регистратору **Spaceship** для смены NS. Сами шаги в дашборде пройдём по `RUNBOOK.md` (Wave 3b) — каждый подтверждаешь.
2. **D1 — точность пина:** точный (дефолт) или фуззи-круг ~150 м?
3. **Deploy `go`** на Wave 6 (каждый ssh-шаг подтверждается, как в Stage R/W).

Всё остальное (D2/D4/D5 и пр.) идёт по дефолтам выше — возрази, если что-то иначе.

---

## Definition of Done — Stage S

- [ ] Карточки выровнены (равная высота, CTA-кнопка прибита к низу, line-clamp), CTA — стилизованная кнопка с состояниями/focus-ring, ноль overflow 320–1920px, районы кириллицей.
- [ ] `/apt/{id}`: Leaflet-карта с точкой квартиры + кнопки «Открыть в Yandex Maps» / «Открыть в Google Maps» (порядок координат корректен), корректный fallback без координат, mobile-скролл не залипает, ToS-инвариант (ноль площадочных ссылок).
- [ ] CF перед `rentbelgrad.com` (Full Strict, Origin-cert), origin заблокирован на не-CF-IP, глобальная доступность подтверждена с внешней/мобильной сети, rate-limit/lockout на реальном IP, admin закрыт и не кэшируется, CF Web Analytics включена.
- [ ] Большое параллельное код+дизайн-ревью проведено (~10 линз), триаж записан, все blocker'ы исправлены и покрыты тестами, 2-й круг — вердикт ship.
- [ ] Taste-фичи: PWA+offline+bottom-nav, динамический OG-image, JSON-LD, полировка `/map` (EN-локаль — по решению).
- [ ] Ноль миграций (alembic head = 0009), full-suite + e2e зелёные, задеплоено, доки обновлены, ветка влита в `main` и запушена.

---

### Старт для Claude Code
1. Superpowers установлен; создать worktree ветки `feature/showcase-and-shield` от `main` @ `20bd9a8`; скопировать `data/districts.yaml` в worktree.
2. Прочитать: `ARCHITECTURE.md` → `DATA_CONTRACT.md` (+`§8`) → `SESSION_HANDOFF_2026-06-23.md` → этот план.
3. Wave 0: заполнить `DECISIONS-S.md` (подтвердить D1/D3) + `DATA_CONTRACT-S.md` + диагностика S0-2.
4. Запустить Wave 1 ∥ Wave 2 (ownership-таблицы), затем Wave 4 (ревью), Wave 3 (CF, совместно), Wave 5, Wave 6.
