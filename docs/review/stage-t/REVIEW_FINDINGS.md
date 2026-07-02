# Stage T · Wave 5 — Итоги ревью

- **Этап:** Stage T «Insight & Integrity», Wave 5 (состязательное ревью + fix-линии)
- **Дата:** 2026-07-02
- **Ветка:** `feature/insight-and-integrity`
- **Миграция:** head 0010 (`20260702_0010_analytics.py`, +81 строк) — единственная новая миграция этапа
- **Новые зависимости:** нет (0 добавлено)

## Методология

10 линз read-only (L1-security, L2-privacy-tos, L3-data, L4-performance, L5-tests, L6-architecture,
L7-a11y, L8-taste, L9-ops, L10-docs) → дедупликация → состязательная верификация каждой находки
(repro/proof на боевом коде).

Воронка: **54 находки → 45 после дедупа → 40 подтверждено / 5 опровергнуто. Блокеров: 0.**

Из 40 подтверждённых: 14 major, 26 minor. Все major и все blocker'ы (0) закрыты TDD-фиксами в шести
fix-коммитах; 8 minor (докс-дрейф) отведены в докс-синхронизацию Wave 6; 3 minor — в бэклог.

## Подтверждённые находки (40)

Сортировка: major сверху, затем minor. `file:line` — точка привязки.

| # | Sev | Линза | file:line | Описание | Резолюция |
|---|-----|-------|-----------|----------|-----------|
| 0 | major | L1-security | `src/site/analytics.py:143` | Пустая/незаданная `SITE_ANALYTICS_SALT` при enabled-by-default делает visitor-хэши обратимыми — нет fail-closed гарда | `2a574d6` |
| 4 | major | L2-privacy-tos | `src/config.py:200` | Дефолт соли `""` при `site_analytics_enabled=True` — визитор-хэши верифицируемы оффлайн и связываемы между днями, ломается §A.1 «несвязываемость» | `2a574d6` |
| 7 | major | L3-data | `src/site/analytics.py:178` | Crafted `Referer` (усечённый IPv6) роняет `urlsplit` в `_ref_domain` — день навсегда не роллапится, raw не ротируется | `2a574d6` |
| 8 | major | L6-architecture | `src/cli.py:916` | Нет догона пропущенных дней: несагрегированный день навсегда выпадает из аналитики, его raw не ротируется | `2a574d6` |
| 29 | major | L9-ops | `src/site/analytics.py:258` | Партиал-роллап «сегодня» ставит `_rolled` → retention необратимо удаляет raw дня с пропущенным re-rollup; ручного re-rollup нет | `2a574d6` |
| 1 | major | L1-security | `deploy/Caddyfile:57` | `photo_manifest.json` публично на `/photos/` — машинночитаемый индекс ВСЕГО inbound-корпуса, включая непубличные листинги | `765b439` |
| 3 | major | L2-privacy-tos | `src/site/photos.py:61` | Тот же манифест: пишется в корень публичной раздачи → общедоступный индекс всех inbound-фото (отклонённые/снятые/приватные) | `765b439` |
| 14 | major | L5-tests | `src/site/photos.py:124` | `process_photo` игнорирует EXIF-ориентацию (вариант повёрнут, w/h манифеста перепутаны); тест-набор без EXIF/RGBA/CMYK — дефект невидим | `765b439` |
| 9 | major | L4-performance | `src/site/cache.py:66` | Неограниченный рост catalog-кэша по attacker-controlled ключам (нет maxsize/sweep) — memory-DoS публичного GET /catalog | `8cb9967` |
| 10 | major | L4-performance | `src/site/templates/index.html:53` | «Квартира недели» отдаёт сырой оригинал (до ~1.4 MB) без photo_v/srcset + `loading=lazy` на вероятном мобильном LCP | `c36044a` |
| 11 | major | L8-taste | `src/site/templates/apt.html:82` | Подпись «+N фото» на пятом тайле галереи закрашена absolutely-positioned фото/якорем — счётчик скрытых фото не виден | `c36044a` |
| 21 | major | L7-a11y | `src/site/static/css/site.css:271` | Фильтр «Комнат» неоперабелен с клавиатуры и невидим SR: `.pill input{display:none}` (WCAG 2.1.1 / 4.1.2) | `c36044a` |
| 22 | major | L7-a11y | `src/site/templates/partials/_results.html:3` | HTMX-своп вкладывает дубликат `div#results` и уничтожает aria-live-регион счётчика на каждый своп — объявление ненадёжно | `c36044a` |
| 28 | major | L8-taste | `src/site/templates/partials/_subscribe_form.html:30` | Форма «Сохрани поиск» на /catalog наполовину не заверстана: groove-fieldset, UA-радио, мёртвые классы, необработанный `.toggle` | `c36044a` |
| 2 | minor | L1-security | `src/admin/templates/base.html:7` | htmx (unpkg) и Chart.js (jsdelivr) без SRI и без CSP — компрометация CDN даёт JS в аутентифицированной сессии | `e562146` |
| 23 | minor | L7-a11y | `src/admin/templates/analytics.html:192` | Таблицы «Топ-квартиры»/«Источники» без `<caption>`/`aria-labelledby` — для SR анонимные таблицы | `e562146` |
| 24 | minor | L7-a11y | `src/admin/templates/base.html:52` | Цветовые бейджи админки проваливают AA-контраст (yellow 2.50:1, green 2.93:1, red 4.22:1) | `e562146` |
| 5 | minor | L2-privacy-tos | `src/site/leads.py:103` | POST /lead не ограничивает длину `districts`/`rooms` — рукописный POST пишет в leads блобы произвольного размера | `8cb9967` |
| 6 | minor | L2-privacy-tos | `src/site/subscriptions.py:109` | POST /subscribe лимитирует лишь количество districts/rooms, не длину элементов — criteria-JSON произвольного размера | `8cb9967` |
| 13 | minor | L4-performance | `src/site/routes.py:425` | «/» и «/apt/{id}» обходят TTL-кэш → по 2–3 полных `_load_rows`-гидрации всего набора на каждый запрос | `8cb9967` |
| 12 | minor | L4-performance | `src/site/templates/partials/_card.html:31` | `sizes` карточки не совпадает с брейкпоинтами сетки (620/980) — пере-загрузка на 620–700px, недо-резолюция на 701–980px | `c36044a` |
| 25 | minor | L7-a11y | `src/site/static/js/filters.js:71` | «Сбросить фильтры» не сбрасывает район и не появляется, когда активен только район | `c36044a` |
| 26 | minor | L7-a11y | `src/site/static/css/site.css:327` | Индикатор «+N фото» в галерее /apt закрыт absolutely-positioned изображением — зрячий пользователь не видит | `c36044a` |
| 27 | minor | L7-a11y | `src/site/templates/catalog.html:9` | Каталог/карта/калькулятор/how/vnj/unsubscribe без `<h1>` — outline страниц начинается с h2 | `c36044a` |
| 16 | minor | L5-tests | `tests/test_cli_analytics_nightly.py:45` | Семейство полуночных флаков: SUT и ассерт независимо берут wall-clock дату — прогон через UTC-полночь даёт ложное красное | `2a574d6` |
| 19 | minor | L6-architecture | `src/site/photos.py:179` | Мердж манифеста защищён только `threading.Lock` — кросс-процессный lost-update реален (admin-publish и monitor-delivery в разных процессах) | `765b439` |
| 31 | minor | L9-ops | план `…insight-and-integrity.md:148` | Шаг 6 раннбука (photos-backfill) без фонового запуска; манифест пишется только в конце — обрыв ssh оставляет варианты без манифеста | `765b439` (инкрементальный флаш каждые 100) |
| 17 | minor | L5-tests | `tests/test_cli_purge_inactive.py:191` | Не покрыты: дозапись архива gzip-member'ом при повторном прогоне в тот же день и TOCTOU-гард перед delete | `bc4b6b3` |
| 30 | minor | L9-ops | план `…insight-and-integrity.md:164` | Rollback-раздел плана неисполним для nightly: `SITE_ANALYTICS_ENABLED=false` не глушил rollup/retention, per-step флага не было | `2a574d6` (kill-switch глушит nightly-шаг) |
| 32 | minor | L9-ops | `scripts/restore_check.sh:130` | Restore-drill не смоук-проверяет таблицы Stage T/W2 (site_events/site_stats_daily/subscriptions) | докс-синхрон Wave 6 (F7) |
| 33 | minor | L10-docs | `docs/BACKLOG.md:10` | Пункт #3 называет масштаб purge «~8442», хотя диагностика зафиксировала 26 400 — число устарело в 3× | докс-синхрон Wave 6 (F7) |
| 34 | minor | L10-docs | `docs/web/DATA_CONTRACT-T.md:169` | §C обещает в архиве «полные строки», а код намеренно обнуляет `photo_embeddings` — контракт рассинхронизирован | докс-синхрон Wave 6 (F7) |
| 35 | minor | L10-docs | `.env.example:79` | `.env.example` отстаёт от `config.py` на 19 ключей Settings (kill-switch'и nightly, cityexpert, dedup, smoke, subscription-bot) | докс-синхрон Wave 6 (F7) |
| 36 | minor | L10-docs | план `…insight-and-integrity.md:171` | Ownership-матрица §3/§OWN не отражает фактический набор изменённых файлов (_subscribe_form, catalog/index/_results, js, db.py) | докс-синхрон Wave 6 (F7) |
| 37 | minor | L10-docs | `.env.example:159` | Комментарий к `SITE_ANALYTICS_SALT` описывает неверную формулу (одиночный sha256 вместо двухступенчатой суточной соли) | докс-синхрон Wave 6 (F7) |
| 38 | minor | L10-docs | `docs/web/ARCHITECTURE.md:47` | ADR утверждает «узкий writer ТОЛЬКО для leads», хотя аллоулист = leads+subscriptions+site_events | докс-синхрон Wave 6 (F7) |
| 39 | minor | L10-docs | `README.md:11` | README: «на трёх площадках» (их четыре — cityexpert) и «Веб-админка read-only» (админка давно пишет) | докс-синхрон Wave 6 (F7) |
| 15 | minor | L5-tests | `tests/e2e/conftest.py:37` | Комбинированный `RUN_E2E=1 pytest tests/` падает order-зависимостью pytest-asyncio после playwright | **бэклог** (см. ниже) |
| 18 | minor | L6-architecture | `src/site/leads.py:70` | Дубль utm-санитайза в `leads.py`/`analytics.py` (ref_domain уже консолидирован в `2a574d6`) | **бэклог** (см. ниже) |
| 20 | minor | L6-architecture | `src/admin/publish.py:134` | Слоистость pipeline→admin: Pillow-генерация в admin-функции, исполняемой процессом доставки | **бэклог** (см. ниже) |

### Соответствие fix-коммитов

| Коммит | Линия | Закрытые находки |
|--------|-------|------------------|
| `2a574d6` | F1-analytics | [0] [4] fail-closed соль; [7] общий `ref_domain` с ValueError-гардом; [8] [29] догон `unrolled_days()` + `_rolled` только дням `<today`; [30] kill-switch глушит nightly-шаг; [16] анти-полночь в тестах |
| `765b439` | F2-photos | [1] [3] манифест в РОДИТЕЛЕ photos_root + Caddy `hide`; [14] EXIF-transpose + RGBA/LA/P/CMYK; [19] кросс-процессный `fcntl.flock`; [31] инкрементальный флаш backfill каждые 100 |
| `c36044a` | F3-frontend | [21] pill visually-hidden; [22] outerHTML-своп + стабильный `#results-status` вне свопа; [25] reset района; [27] h1 на всех страницах; [10] featured detail-вариант + eager + fetchpriority; [11] [26] «+N» поверх тайла (z-index+скрим); [12] честные sizes; [28] вёрстка subscribe-формы по токенам |
| `8cb9967` | F4-cache-valid | [9] TTLCache `maxsize=512` + эвикция; [13] кэш «/» и «/apt/{id}»; [5] [6] лимиты длин districts/rooms |
| `e562146` | F5-admin | [2] SRI sha384 htmx/chart.js; [23] `aria-labelledby` таблицам; [24] AA-контраст бейджей |
| `bc4b6b3` | F6-tests | [17] тесты gzip-append и TOCTOU-гарда purge-inactive |

Докс-линия F7 (idx 32-39) выполняется в рамках докс-синхронизации Wave 6 (обновление
BACKLOG/DATA_CONTRACT/.env.example/ARCHITECTURE/README + план Wave 6), отдельным коммитом этапа.

## Опровергнуто (5)

1. **L3-data · `src/cli.py:919` — «Nightly rollup loop abort: падающий „вчера“ пропускает „сегодня“».**
   Механика воспроизводится (один `try` на оба дня), но это осознанное решение по контракту:
   `DATA_CONTRACT-T.md §N` определяет `rollup(вчера, сегодня)` как ОДИН джоб, изоляция требуется
   между джобами, а не внутри. Заявленные последствия завышены/ложны. **Refuted.**

2. **L6-architecture · `_card.html:26` — «Динамический вызов Jinja-фильтра `'photo_v' is filter` маскирует потерю регистрации».**
   Факт-паттерн есть (динамика + `map` в `_card.html`/`apt.html`), но ключевое обвинение —
   «при потере регистрации ни один тест не упадёт, `is filter` спроектирован это скрывать» —
   фактически неверно: осознанный fallback, регистрация покрыта. **Refuted.**

3. **L6-architecture · `src/cli.py:2979` — «cli.py вырос на +477 строк: ~230 строк бизнес-логики purge вопреки паттерну этапа».**
   Числа воспроизводятся, но ядро — «размещение непоследовательно, вопреки собственному паттерну
   этапа» — опровергнуто как осознанное задокументированное решение (ownership-матрица прямо
   отводит purge в cli.py). **Refuted.**

4. **L7-a11y · `analytics.html:114` — «aria-label спарклайна перечисляет до 90 чисел».**
   Ключевое «доказательство» ложно: `ANALYTICS_WINDOWS = (7, 30)`, окна 90 нет; `?days=90` не
   проходит валидацию и падает в `DEFAULT_DAYS=7`. Заявленный repro «label из 90 чисел»
   невоспроизводим. **Refuted.**

5. **L7-a11y · `published.html:30` — «Бейдж просмотров несёт смысл через emoji 👁 и title, недоступный с клавиатуры».**
   Строка воспроизводится, но цепочка разваливается: правило «no emoji» действует ТОЛЬКО в публичном
   сайте (`src/site/templates`), не в admin; заявленное нарушение вне скоупа, санкционировано планом. **Refuted.**

## В бэклог

Три minor-находки осознанно отведены в бэклог с обоснованием:

- **[15] · L5-tests · `tests/e2e/conftest.py:37` — order-зависимость комбинированного `RUN_E2E=1 pytest tests/`.**
  Фикс `92d66ea` (run_coro в worker-треде) вылечил сиды e2e-фикстур, но корневая причина — sync
  playwright-драйвер оставляет «running» event loop в main-треде, а pytest-asyncio поднимает Runner
  там же — остаётся для async-тел тестов вне e2e. Документированный гейт — **два раздельных запуска**
  (юниты и e2e), комбинированный прогон невозможен by design; в CI-практике зелено. Полная изоляция
  (pytest-forked/xdist group либо отдельный playwright-loop) — отдельная тема, риск/польза в пользу
  бэклога.

- **[18] · L6-architecture · `src/site/leads.py:70` — дубль utm-санитайза.**
  Конструктивная часть (общий `_ref_domain` с ValueError-гардом) уже консолидирована в `2a574d6`,
  чем закрыто расхождение поведения из [7]. Оставшаяся консолидация utm-санитайза (`clean_utm` ↔
  `_clean_utm`) в единый `attribution.py` — рефакторинг без функционального дефекта; риск/польза в
  пользу бэклога.

- **[20] · L6-architecture · `src/admin/publish.py:134` — слоистость pipeline→admin.**
  `auto_publish_site_listing` живёт в UI-пакете `src/admin`, но исполняется пайплайном
  (telegram/scrapers); Stage T добавил внутрь Pillow-генерацию. Дрейф пре-существующий (Stage W2 X1),
  этап его усугубил, но это архитектурный рефактор (вынос publish-домена в нейтральный модуль), не
  bug — Wave 6+.

## ВЕРДИКТ

**SHIP.**

- Блокеры: **0** — закрыты.
- Major: **14** — все закрыты TDD-фиксами (6 fix-коммитов: `2a574d6`, `765b439`, `c36044a`,
  `8cb9967`, `e562146`, `bc4b6b3`).
- Гейт **зелёный**: юниты **1055** / e2e **52** — гоняются раздельно (комбинированный прогон
  невозможен by design, см. [15]).
- Новых зависимостей: **0**.
- Миграций: **одна** — `0010_analytics`.
- Остаток: 8 minor докс-дрейфа (F7) — докс-синхронизация Wave 6; 3 minor ([15][18][20]) — бэклог.
