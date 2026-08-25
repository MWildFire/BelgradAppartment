# План «Nekretnine Zero-Regression» — list snapshot + ISP proxy без изменений Render Finder Bot

> Дата: 2026-08-24. База на момент составления: `main` @ `99b7b2214a7f930b2dc64fc12ccf247240bb9989`.
> Рабочее дерево **нечистое**: в нём уже есть 10 намеренных незакоммиченных файлов proxy/production-hardening
> (перечень в §0). Их нельзя сбрасывать, заменять версией из `HEAD` или считать частью этой новой задачи
> без просмотра `git diff`.
>
> **Цель:** перейти с metered residential traffic на list-snapshot ingestion и два Serbia ISP proxy,
> сохранив на **100%** наблюдаемое поведение Render Finder Bot для Даши и не ухудшив полноту/точность
> поиска квартир.
>
> **Исполнитель:** другая, потенциально более простая модель в роли Integration Captain плюс три
> параллельных агента. Выполнять не линейную очередь, а DAG из §2.6: постоянно занимать все безопасные
> worker-слоты, но не перепрыгивать общие barriers. Если код противоречит этому плану — остановиться,
> записать точное противоречие и сначала обновить план. Покупка, ввод платёжных/идентификационных данных
> и production activation требуют отдельных точек подтверждения Дмитрия.

---

## 0. Стартовый контекст и состояние дерева

### 0.1 Что прочитать до первого изменения

1. Этот файл целиком.
2. `HANDOFF.md`: текущий production-контур, Nekretnine/DataDome, правила deploy и smoke.
3. `README.md`: inbound → scrape → match → delivery.
4. `docs/BACKLOG.md` #21/#23: почему cookie transfer/httpx не заменяют browser fallback.
5. `src/scrapers/base.py` — `ListingPreview`, `ListingDetail`, proxy contract.
6. `src/scrapers/nekretnine.py` — list payload, detail payload, Playwright fallback.
7. `src/scrapers/orchestrator.py` — `scrape_platform`, `_process_one`, photo download, heal/rolling.
8. `src/jobs/pipeline.py`, `src/matching/**`, `src/telegram/bot_delivery.py`,
   `src/telegram/delivery.py`, `src/post/template.py` — защищаемый Render Finder Bot contract.
9. `src/scrapers/smoke.py`, `src/cli.py` (`nightly-refresh`, `scrape-smoke`, `pipeline-probe`).
10. `scripts/vps_install.sh`, `scripts/deploy_sync.sh` — реальные systemd и deploy boundaries.

### 0.2 Незакоммиченные изменения, которые уже были в дереве 24.08

```text
deploy/otz-admin.service
deploy/otz-site.service
scripts/vps_install.sh
src/cli.py
src/scrapers/base.py
src/scrapers/nekretnine.py
src/scrapers/orchestrator.py
src/site/liveness.py
tests/test_link_verify.py
tests/test_scrapers_base.py
```

Кроме них текущий handoff добавил `HANDOFF.md` и сам этот plan-файл. Это планирующие артефакты, а не
реализация snapshot migration; их тоже нельзя потерять при создании веток/worktree.

Первое действие исполнителя:

```bash
git status --short
git diff --stat
git diff --check
git diff -- <каждый пересекающийся файл>
```

**Запрещено:** `git reset --hard`, `git checkout -- <file>`, `git restore <file>` или другое удаление
этих изменений. Integration branch `codex/nzr-integration` создаётся только после того, как текущий diff
понят и сохранён. Создание ветки не должно потерять dirty state.

### 0.3 Проверенные production-факты, которые объясняют задачу

- Успешный weekly 23.08: 5 077 Nekretnine previews из 5 082.
- 369 list-навигаций и 919 detail-навигаций прошли через browser/proxy path.
- 851 новая квартира; 4 226 существующих увидены снова.
- 11 823 фото обработаны; `pic.nekretnine.rs` доступен напрямую, без proxy.
- В живом list payload на 25/25 карточках были ID, title, price, surface, rooms, district,
  coordinates и photo IDs; address был на 22/25.
- Matcher использует фото/pHash/CLIP + price/area/district; description не участвует в выборе.
- Render Finder Bot отправляет **inbound-фотографии Даши** (`InboundPost.photo_paths`), а не скачанные
  listing-фото. Listing влияет на ссылку, platform, price/area, map и hashtags.

### 0.4 Суть целевого изменения

```text
Nekretnine list page (__NEXT_DATA__)
               │
               ▼
        ListingPreview + snapshot
               │
       ┌───────┴────────┐
       │ snapshot полон?│
       └───────┬────────┘
          да   │   нет / parser drift
       ┌───────▼──────┐       ┌──────────────────────┐
       │ upsert/merge │       │ legacy fetch_detail  │
       │ + direct CDN │       │ через browser/proxy  │
       └───────┬──────┘       └──────────┬───────────┘
               └──────────────┬──────────┘
                              ▼
                  существующий matcher без изменений
                              ▼
                Render Finder Bot без изменений
```

Fallback на legacy detail — обязательная часть дизайна, а не временный костыль. Он гарантирует, что
редкое неполное/изменившееся объявление не попадёт в БД с меньшим качеством данных.

---

## 1. Железные правила: нарушение = остановка

### 1.1 Защищённый контракт Render Finder Bot

В рамках этого перехода **не менять**:

- `src/telegram/monitor.py`;
- `src/jobs/pipeline.py`;
- `src/telegram/bot_delivery.py`;
- `src/telegram/delivery.py`;
- `src/post/template.py`;
- `src/matching/**`;
- matching thresholds в `src/config.py`;
- `TG_DELIVERY_MODE`, `TG_BOT_TOKEN`, `TG_BOT_RECIPIENTS`, `TG_DELIVERY_TARGET` в production `.env`;
- порядок/число inbound-фотографий, header, platform/source links, confidence/method, готовый текст,
  hashtags, карту и правила `PENDING → PUBLISHED`/auto-publish.

Перед работой записать локальные blob hashes защищённых файлов, после работы сравнить. Допустимы только
новые тесты, которые импортируют этот код; сам код выше должен остаться byte-identical.

```bash
git hash-object src/telegram/monitor.py src/jobs/pipeline.py \
  src/telegram/bot_delivery.py src/telegram/delivery.py src/post/template.py \
  src/matching/*.py
```

Если для реализации якобы требуется менять защищённый файл — это архитектурный сигнал, что решение
выбрано неправильно. Остановиться и запросить Дмитрия.

### 1.2 Что означает «0% потери качества» в проверяемых терминах

| Контракт | Обязательный гейт |
|---|---|
| Даша присылает тот же Telegram post/album | monitor/watermark/album grouping не меняются |
| Выбирается та же квартира | на replay/golden наборе: 100% одинаковые `status`, `listing_id`, `method`, `matched_photo_count`, `confidence` |
| Даша получает тот же Render Finder Bot результат | caption сравнивается byte-for-byte; количество и порядок media одинаковы |
| Нет ложного `NO_MATCH` из-за snapshot | complete snapshot содержит все matcher-required поля; иначе обязательный legacy detail fallback |
| Нет ухудшения географии/поста | merge никогда не заменяет существующее non-null поле на `None`; перед delivery используются те же DB fields |
| Фото-кандидаты не ухудшаются | тот же упорядоченный набор XXL photo IDs; при неуспешной загрузке старый hash/embedding набор сохраняется |
| Новые объявления не теряются | полный crawl: `skipped_pages=0`, coverage не хуже текущего healthy baseline и не ниже 98% site total |
| Остальные источники не страдают | 4zida, Halooglasi, CityExpert продолжают ходить напрямую и проходят smoke |
| Liveness не ухудшается | в первой поставке не менять stale/verify/site-liveness semantics |
| Скорость для Даши не хуже | inbound → bot delivery не медленнее baseline более чем на 10%; ожидается ускорение |

Абсолютное требование для видимого output и replay — **100% равенство**, не «почти одинаково».

### 1.3 Финансы, Chrome и секреты

- Агент может открыть страницы тарифа/checkout и собрать quote.
- Перед последней кнопкой покупки — остановиться и показать Дмитрию: продукт, страна/город, число IP,
  срок, auto-renewal, налог и итоговую сумму.
- Покупать только после новой явной команды, привязанной к этому quote.
- Номер карты, CVV, пароль, OTP/3DS, документы KYC и proxy password Дмитрий вводит локально сам.
- Не просить эти значения в чате, не снимать их скриншотом и не сохранять в repo/Second Brain/log.
- Proxy URL/credentials живут только в `/opt/otz/.env` с mode `600`; в `.env.example` только пустые
  placeholders.
- Никогда не `cat /opt/otz/.env` и не печатать proxy URL. До/после env-правки сравнивать hash строк
  `TG_` без вывода самих строк.

### 1.4 Production и deploy

- Production host на момент плана: `3.121.163.130`; перед deploy перепроверить DNS и SSH.
- `scripts/deploy_sync.sh` всё ещё содержит устаревший default VPS `90.156.254.159`; **не запускать без
  явного `VPS=...` и SSH key transport**. Случайный deploy на старый хост — blocker.
- Не deploy из отдельного worktree: сначала интегрировать проверенный commit в основной checkout.
- Не rsync `.env`, `data/`, DB, Telegram session или secrets.
- До любого DB-writing canary — backup + SQLite `PRAGMA integrity_check`/`quick_check`.
- Feature default при первом deploy: `legacy`; production поведение не меняется до отдельного gate.
- Никакой миграции БД для этого перехода не требуется. Если исполнитель считает иначе — остановиться.

---

## 2. Целевая архитектура и конфигурация

### 2.1 List snapshot contract

Минимальный diff:

1. В `src/scrapers/base.py` добавить в `ListingPreview`:

   ```python
   snapshot: ListingDetail | None = None
   ```

   Благодаря `from __future__ import annotations` можно сохранить текущий порядок классов.

2. В `src/scrapers/nekretnine.py::_previews_from_payload` для каждого `realEstate` строить тот же
   `ListingDetail`, который умеет `_detail_from_real_estate`, и класть его в `preview.snapshot`.

3. Photo URLs строить из list `photo.id` как:

   ```text
   https://pic.nekretnine.rs/image/<id>/xxl.jpg
   ```

   Если payload уже даёт large URL — разрешено использовать его; порядок и dedup сохраняются.

4. Для **нового** listing snapshot считается complete только если есть все поля, способные повлиять
   на matcher или видимый Render Finder Bot caption:

   - `external_id` и canonical URL;
   - `price_eur`;
   - `area_m2`;
   - `rooms`;
   - `district`;
   - `address_raw`;
   - `lat` и `lon`;
   - `title`;
   - хотя бы один валидный `photo_url` с host `pic.nekretnine.rs`.

   Это консервативнее matcher minimum: live list имел address в 22/25 случаях, поэтому несколько
   объявлений всё ещё уйдут в legacy detail. Так мы не допустим видимой замены address-first карты на
   coords-only карту у Даши. Для **существующего** listing недостающее snapshot-поле можно сохранить из
   уже заполненной DB строки; отсутствие в snapshot никогда не очищает его. Description не является
   required и не используется Render Finder Bot.

5. Любой incomplete snapshot или parser drift автоматически идёт по нынешнему `fetch_detail` пути.
   Нельзя вставлять all-None/empty-photo shell.

### 2.2 Режимы feature flag

Добавить в `src/config.py` один строковый режим:

```text
NEKRETNINE_LIST_SNAPSHOT_MODE=legacy|shadow|active
```

- `legacy` — поведение до перехода; default и первый production deploy.
- `shadow` — snapshot строится и сравнивается, но DB пишет legacy detail path.
- `active` — complete snapshot используется, incomplete уходит в legacy fallback.

Невалидное значение должно fail-fast при старте Settings, а не молча выбирать active.

### 2.3 Upsert/merge semantics

В `src/scrapers/orchestrator.py`:

- передавать `_process_one` сам `ListingPreview`, а не только `url/external_id`, либо добавить новый
  узкий helper, сохранив совместимость старой сигнатуры для других платформ;
- для нового complete snapshot: скачать direct-CDN фото, посчитать pHash/CLIP и вставить Listing без
  detail navigation;
- для нового incomplete snapshot: вызвать текущий `fetch_detail`;
- для существующего: обновить `last_seen_at`, canonical URL, `is_active`; non-null snapshot fields
  merge-ить без очистки хороших значений;
- если набор photo IDs не изменился — не скачивать/не пересчитывать ничего;
- если изменился — сначала полностью подготовить новые files/hashes/embeddings, потом атомарно заменить
  DB fields; при ошибке оставить прежний фото-набор;
- если цена изменилась — сохранить `PriceChange` и пропустить его через тот же nightly price-drop alert
  contract; не отправлять дубль с rolling refresh;
- `details_refreshed_at`: snapshot не притворяется detail fetch. Не использовать это поле для ложного
  утверждения, что description/detail обновлены.

### 2.4 Что пока сознательно остаётся прежним

До прохождения одного полного weekly в active mode:

- `scrape-smoke` продолжает проверять хотя бы один реальный Nekretnine detail;
- Nekretnine остаётся в rolling detail/heal;
- `verify_active_links` и `site_liveness` не меняют алгоритм, DB semantics или fail-safe rules. Их
  текущий transport baseline также заморожен: Nekretnine `verify_active_links` уже использует
  настроенный scraper proxy после H1 hardening (это отражено в HANDOFF §0.-7), тогда как остальные
  платформы остаются direct. Snapshot migration не расширяет этот liveness scope и не переключает
  transport;
- stale sweep health/coverage gates не меняются;
- другие три scraper не получают snapshot-специфичных веток.

Это небольшое временное число detail/HEAD запросов на unlimited ISP proxy. Оно сохраняет эксплуатационное
качество и отделяет ingestion migration от liveness migration.

### 2.5 ISP proxy pool

Добавить backward-compatible настройки:

```text
NEKRETNINE_PROXY_PRIMARY=
NEKRETNINE_PROXY_SECONDARY=
NEKRETNINE_PROXY_METERED_FALLBACK=
NEKRETNINE_PROXY_KIND=legacy_rotating|static_isp
```

Все три URL-поля объявить через `Field(default="", repr=False)` либо эквивалентный secret-safe type.

Правила:

- если новые поля пусты, использовать существующий `SCRAPER_PROXY_URL` и нынешнее поведение;
- `static_isp`: передавать credentials как есть, **не** добавлять IPRoyal `_session-..._lifetime-...`;
- `legacy_rotating`: сохранить нынешний session suffix;
- порядок: primary → secondary → metered fallback;
- failover только после bounded browser challenge/reload failure, не на каждой странице;
- новый proxy получает новый browser profile/cookies;
- proxy URLs/passwords никогда не попадают в repr/log/error/Telegram summary;
- 4zida/Halooglasi/CityExpert и прямой Nekretnine CDN остаются direct. Для liveness сохраняется
  описанный выше pre-migration transport baseline: Nekretnine verify-links proxy path не меняется,
  остальные platform HEAD paths direct; новые snapshot proxy selectors туда не протекают.

Добавить run telemetry без секретов:

- list browser navigations;
- legacy detail fallbacks;
- challenge reloads;
- proxy failovers;
- skipped pages/coverage;
- approximate/encoded response bytes, если CDP measurement устойчив; иначе не выдавать estimate за
  provider-billed bytes.

Даже при unlimited ISP оставить page/detail caps и runaway guard.

### 2.6 Multi-agent execution topology — обязательный режим

Здесь «максимальная мультиагентность» означает максимальную занятость **безопасно независимых** lanes,
а не одновременную запись четырёх агентов в один checkout, одну SQLite DB или production. Лимит команды:
`1 Integration Captain + 3 worker agents`. Пока есть независимая работа, все три worker-слота должны
быть заняты. Общие side effects — merge, Chrome, SSH, `.env`, DB writes, restart, payment и mode switch —
всегда выполняет только Integration Captain.

#### Четыре постоянных слота и сменные lanes

| Слот | Постоянная ответственность | Запрещено |
|---|---|---|
| Integration Captain / Production Operator | integration branch, plan/HANDOFF, manifests, commit/cherry-pick, barriers, Chrome, SSH и production side effects | самостоятельно объявлять QA PASS; передавать Chrome/production/payment worker-агенту |
| Agent A | сменный implementation lane с точным allowlist текущего batch | файлы Agent B/C, Telegram/matching, production |
| Agent B | второй сменный implementation/docs lane с точным allowlist | файлы Agent A/C, protected code, production |
| Agent C | постоянный независимый `zero_regression_qa` / Merge Guardian | любой production-код, ослабление tests/golden, Chrome/production |

Чтобы три worker-слота не простаивали на последовательных задачах, Agent A/B получают новую роль после
каждого integration barrier:

| Batch | Agent A | Agent B | Agent C | Порядок интеграции |
|---|---|---|---|---|
| P0 read-only | parser/browser dirty-diff audit | config/orchestrator/CLI dirty-diff audit | baseline suite/probe + golden topology | только manifests/checkpoint |
| P1 contract/primitives | snapshot parser: `base.py`, `nekretnine.py`, fixtures/tests | proxy/config primitives: `config.py`, `.env.example`, новый `proxy_pool.py`, tests | immutable golden + initial parity/negative tests | proxy/config → parser; expected-red QA commit можно держать до P2 |
| P2 ingestion/audit | orchestrator active/shadow + узкий helper/tests | новый shadow-audit core + fake tests, **без CLI** | review exact integrated SHA, parity/security | orchestrator → audit core → QA findings/fixes |
| P3 wiring/docs/release | только `src/cli.py` + отдельный CLI test | `README.md`, `docs/BACKLOG.md`; plan/HANDOFF остаются root | clean full-suite/security/docs consistency | CLI → docs → fixes → release gate |
| P4 shadow/canary observers | crawl/proxy/fallback metrics | DB/no-write/integrity/photo-price atomicity | Telegram/protected/parity/duplicate evidence | root выполняет side effects; agents только читают sanitized evidence |

Если существующий тестовый файл нужен двум lanes, не редактировать его параллельно. Создать два новых
узких test-файла либо отдать общий файл одному владельцу через обновлённый `ownership.json` до начала
batch. Один файл в одном batch имеет ровно одного writer.

Agent C — независимый Merge Guardian с безусловным правом `STOP`. Он не может исправлять найденную
регрессию production-кодом, обновлять golden после implementation, добавлять `skip/xfail`, увеличивать
tolerance или ослаблять assertion. Он возвращает минимальный reproduction Integration Captain, который
переадресует его владельцу lane.

#### Изоляция worktree и dirty-state checkpoint

1. Integration Captain фиксирует исходный `HEAD`, `git status`, полный diff, SHA-256 diff и protected
   hashes. Секреты в manifest не включать.
2. Уже существует detached worktree
   `/Users/mvstrike/Projects/BelgradAppartment/.claude/worktrees/determined-jones-7da11f` на исходном
   SHA. Его не удалять, не очищать и не переиспользовать: он вне scope этой миграции.
3. Все уже существующие dirty-файлы классифицируются до commit. Только если каждый diff понятен,
   переключить текущий dirty checkout на `codex/nzr-integration`, не теряя изменений. Затем сделать два
   локальных commits: (a) отдельный rescue-checkpoint из проверенных 10 proxy/production-hardening
   файлов; (b) отдельный docs-checkpoint из `HANDOFF.md` и этого plan-файла. Ничего не push. Если хотя бы
   один diff не понятен — `STOP` и запрос Дмитрию; не stash/reset/restore.
4. Чистый SHA второго checkpoint становится `BASELINE_SHA`. Для каждого batch Captain создаёт новые
   `codex/nzr-<batch>-<lane>` branches/worktrees от точного integration SHA предыдущего barrier. Нельзя
   переиспользовать worker-ветку следующего batch и нельзя стартовать её от старого `99b7b22`.
5. В prompt каждого агента передаётся точный абсолютный `worktree_path`, `base_sha`, allowlist и
   forbidden list. Агент выполняет все команды только со своим `workdir`.
6. Только Integration Captain делает `git add/commit/cherry-pick/merge`. Worker сообщает готовый diff;
   Captain проверяет allowlist и делает узкий commit из worktree. Worker не переключает ветки и не
   меняет историю.
7. После каждого batch Captain интегрирует commits по заранее записанному порядку. Любой conflict
   возвращается автору lane; Captain не угадывает семантику конфликта и не применяет `--ours`/`--theirs`.
   После передачи commit SHA запрещены amend/rebase/force-push; исправление — отдельным новым commit.
8. Для независимого финального QA создать новый detached validation worktree exact integrated SHA.
   Полный suite и critical parity run выполняются там, а не в грязном integration checkout.

P1 parity tests могут оставаться ожидаемо красными до active/shadow wiring P2. В таком случае Captain
сохраняет Agent C commit SHA/manifest, но cherry-pick делает после P2 implementation. Красный тест должен
быть собран pytest и падать только по отсутствующему target behavior, не по import/fixture/environment.

Для каждого агента обязательны отдельные:

- temp directory;
- SQLite DB;
- report directory;
- pytest cache;
- Playwright `user-data-dir`, если browser вообще нужен локальному test-double.

Нельзя параллельно использовать общую production-like DB, общий Playwright profile или один report
file. Unit tests не ходят в live network.

Worker-агентам буквально запрещены `ssh`, `scp`, `rsync`, `systemctl`, `journalctl`,
`scripts/deploy_sync.sh`, `scripts/vps_install.sh`, доступ к `/opt/otz`, production `.env`/DB, Telegram
send, Chrome checkout и payment. Только Captain выполняет эти действия из чистого canonical checkout;
workers получают лишь sanitized output для независимого анализа.

После каждого worker commit Captain проверяет:

```bash
git diff --name-only <agent-base-sha>..<agent-commit-sha>
```

Файл вне allowlist, изменение protected-файла или пересечение ownership = `STOP`.

#### Evidence workspace

Каждый запуск получает `run-id` и локальный каталог, который не коммитится:

```text
data/reports/nekretnine-zero-regression/<run-id>/
```

Минимальные sanitized artifacts:

- `baseline-manifest.json`: base/checkpoint SHA, dirty-diff SHA-256, protected hashes, versions,
  baseline collected/passed/skipped;
- `ownership.json`: role, branch, worktree, base SHA, exact allowed/forbidden files;
- `interface-freeze.md`: snapshot/completeness/proxy/decision/telemetry/audit contracts;
- `agent-result-<lane>.json`: commit SHA, changed files, red/green evidence, reviewer;
- `render-golden-manifest.json`: raw fixture hashes, matcher result, exact caption hash, ordered media
  manifest/hash;
- `integration-manifest.json`: integrated SHA, commit order, conflicts, protected hashes before/after,
  collected tests, skip/xfail delta;
- `cross-agent-review.md`: author, reviewer, QA verdict и незакрытые findings;
- shadow/proxy/canary reports из следующих разделов.

Не сохранять в artifacts полные адреса, cookies, checkout URL, proxy URL/password, Telegram tokens,
значения `TG_*` или платёжные данные.

#### DAG и barriers

```text
S0: read-only bootstrap — root + A + B + C одновременно
 ├─ A: audit dirty parser/browser diff + propose snapshot signatures
 ├─ B: audit orchestrator/config/CLI + atomic/no-write boundaries
 ├─ C: baseline suite/probe + freeze golden/parity oracle
 └─ root: git/protected manifests + architecture + read-only production facts
                              │
                         BARRIER B0
              checkpoint + INTERFACE FREEZE + ownership
                              │
        ┌─────────────────────┼──────────────────────┐
        ▼                     ▼                      ▼
 P1/A: Waves 1–2        P1/B: proxy/config       P1/C: Wave 4 golden,
 snapshot parser        primitives Wave 5        initial parity/negative tests
        └─────────────────────┼──────────────────────┘
                              │
                    P1 integration barrier
               proxy/config → parser → QA tests
                              │
        ┌─────────────────────┼──────────────────────┐
        ▼                     ▼                      ▼
 P2/A: Wave 3           P2/B: Wave 6 audit     P2/C: integrated parity,
 orchestrator           core, no CLI            cross-review/security
        └─────────────────────┼──────────────────────┘
                              │
                         BARRIER B1
              commits + allowlist + cross-review + QA
                              │
        ┌─────────────────────┼──────────────────────┐
        ▼                     ▼                      ▼
 P3/A: CLI wiring        P3/B: docs            P3/C: full/security shard
                              │
 root: integrate → clean detached worktree → full gate twice where required
                              │
                         BARRIER B2
                    Chrome всё ещё запрещён
                              │
 A: ISP checklist ─ root: prod evidence ─ B/C: sanitized preflight/fingerprint audit
                              │
 root: Chrome 24h quote → fresh user approval Q1 → payment by user
                              │
 root: shadow trial; A/B/C параллельно проверяют sanitized evidence
                              │
                         BARRIER B3
                              │
 root: monthly quote → fresh user approval Q2 → payment by user
                              │
 root: 5-page active canary; A/B/C параллельно проверяют три плоскости
                              │
                         BARRIER B4
                              │
             2 incremental → 1 full weekly → BARRIER B5
```

**B0 — разрешение писать код:** baseline честно записан; checkpoint создан; protected/golden hashes
заморожены; ownership не пересекается; Integration Captain публикует `INTERFACE FREEZE` с точными:

- `ListingPreview.snapshot` type/semantics;
- signature completeness helper;
- snapshot decision result;
- config names/defaults;
- proxy selector input/output и redaction contract;
- telemetry schema;
- audit API/report schema.

**B1 — разрешение перейти из P2 в release wiring P3:** каждый P1/P2 scoped test прошёл; каждый commit
внутри allowlist; Agent A cross-review’ит diff Agent B, Agent B — diff Agent A; Agent C даёт `QA_PASS`
на exact integrated SHA. Самопроверка автора не считается review. P3 всегда стартует от этого нового
integration SHA, а не от старой worker branch.

**B2 — разрешение открыть Chrome:** чистый detached validation worktree; полный suite и probe зелёные;
parity suite два раза подряд зелёный на одном SHA; protected/golden hashes совпадают; новых skip/xfail
нет; test collection не уменьшился; все docs согласованы.

**B3 — разрешение купить monthly:** 24h shadow audit прошёл; no DB writes/no Telegram sends доказаны;
coverage/field parity/proxy challenge metrics зелёные; все три observers вернули PASS. Молчание агента
не является PASS.

**B4 — разрешение оставить active:** 5-page DB diff, coverage/fallback telemetry и bot parity проверены
тремя разными observers; любой STOP немедленно включает rollback.

**B5 — завершение перехода:** два incremental и один полный weekly зелёные; QA Guardian проверил
sanitized production artifacts; опыт Даши неизменен; recurring spend подтверждён invoice/dashboard.

#### Как постоянно загружать три worker-слота

- P1: A пишет snapshot parser, B одновременно пишет proxy/config primitives, C — независимый oracle.
- P2 после быстрой интеграции P1: A переключается на orchestrator, B — на audit core, C проверяет exact
  integration SHA и гоняет parity/security. Не заставлять A/B последовательно продолжать старый lane.
- P3: A подключает только CLI, B пишет docs, C делает full/security/docs-consistency review.
- Пока Captain интегрирует один lane, освободившийся автор сразу получает cross-review другого lane.
- Пока clean full suite идёт у Captain, A/B/C параллельно проверяют свои shards и evidence.
- Перед Chrome A проверяет ISP product checklist без checkout, Captain снимает production preflight и
  fingerprints, B/C независимо анализируют только его sanitized output.
- Во время shadow Agent A анализирует coverage/proxy/fallback, Agent B доказывает no-write/integrity,
  Agent C — no-Telegram/protected/parity.
- Во время active canary Agent A следит за crawl, Agent B за DB/photo/price atomicity, Agent C за
  caption/media/delivery/duplicates. Только Captain выполняет команды, меняющие состояние.

Agent, закончивший раньше, не ждёт пассивно: Captain даёт ему независимый review, test shard,
sanitized report audit или documentation consistency. Но нельзя создавать работу, которая размывает
ownership или side-effect boundary.

#### Готовые стартовые prompts

**P1 / Prompt Agent A — `snapshot_parser`:**

```text
Прочитай план целиком. Работай только в WORKTREE_PATH, base SHA = BASE_SHA.
Ты владелец lane snapshot_parser. Сначала read-only изучи текущий diff base.py/nekretnine.py и доложи
точки пересечения. До сообщения INTERFACE FREEZE ничего не меняй. После freeze реализуй Waves 1–2 и
проводку frozen proxy helper в nekretnine.py только в переданном allowlist. Сначала докажи ожидаемый
RED, затем GREEN. Не меняй config/orchestrator/CLI/docs/Telegram/matching. Не делай git mutations,
Chrome, live network purchase, production или DB writes. Не ослабляй required snapshot fields и legacy
fallback. В конце верни completion packet, exact commands/results и отсутствие secrets.
```

**P1 / Prompt Agent B — `proxy_config`:**

```text
Прочитай план целиком. Работай только в WORKTREE_PATH, base SHA = BASE_SHA.
Ты владелец lane proxy_config. До INTERFACE FREEZE только изучи config и proxy contract. После freeze
реализуй settings/default/redaction и отдельный низкоуровневый proxy_pool helper с tests только в
allowlist: config.py, .env.example, новый helper/test. Не меняй base.py/nekretnine.py/orchestrator/CLI,
matching thresholds, docs или protected code. Сохрани legacy fallback и secret-safe repr/errors.
Не делай git mutations, Chrome, production, restarts, env или shared DB writes. Верни completion packet.
```

**Prompt Agent C — `zero_regression_qa`:**

```text
Прочитай план целиком. Работай только в WORKTREE_PATH, base SHA = BASE_SHA.
Ты независимый QA/Merge Guardian и не меняешь production-код. До implementation заморозь две
независимые raw fixtures legacy/list и golden manifest. Создай абсолютный parity gate: 100% matcher
equality, byte-for-byte caption, identical ordered media, delivery-state parity, NO_MATCH и mandatory
fallback. Добавь negative controls, доказывающие, что tests краснеют при потере mapping/location/photo
или failover. Не перегенерируй golden, не добавляй skip/xfail/tolerance. При failure дай root минимальный
reproduction; чужой код не чини. Никаких git mutations, Chrome, production, Telegram network или DB
writes. Docs можно менять только после отдельного follow-up от root.
```

**P2 / Follow-up Agent A — `orchestrator_active_shadow`:**

```text
Новый batch, новый WORKTREE_PATH и BASE_SHA равен exact P1 integration SHA. Ты единственный владелец
orchestrator active/shadow lane. Реализуй Wave 3 и изолированные tests по frozen interfaces. Сохрани
legacy default, non-null merge, atomic photo preparation/swap, price-change contract и direct paths
трёх других платформ. Не меняй parser/config/CLI/Telegram/matching/docs. Сначала RED, затем GREEN;
никаких git/production/shared-DB действий. Верни completion packet.
```

**P2 / Follow-up Agent B — `shadow_audit_core`:**

```text
Новый batch, новый WORKTREE_PATH и BASE_SHA равен exact P1 integration SHA. Реализуй только read-only
shadow-audit core и fake-network tests Wave 6 в новом модуле. Не подключай src/cli.py: это P3. Докажи
no DB writes, no stale sweep, no Telegram, sanitized report и fail-closed thresholds. Не меняй parser,
orchestrator, config, Telegram/matching/docs. Никаких git/live-network/production действий.
```

**P3 / Follow-up Agent A — `cli_wiring`:**

```text
Новый batch от exact P2 integration SHA. Подключи уже интегрированный audit core только в src/cli.py
и новый отдельный CLI test. Не меняй audit core/orchestrator/parser/config. Сохрани default behavior
остальных команд. Никаких live network, git или production действий. Верни narrow completion packet.
```

**P3 / Follow-up Agent B — `docs`:**

```text
Новый batch от exact P2 integration SHA. После команды root обнови README.md и docs/BACKLOG.md по
фактически интегрированному коду. HANDOFF.md и основной plan не трогай: ими владеет root. Не объявляй
production outcome до реального canary/weekly. Проверь ссылки/commands и верни docs consistency report.
```

**Follow-up любому production observer:**

```text
Перейди в read-only observer mode. Не открывай checkout, не выполняй SSH write/restart, не меняй
env/mode/DB, не отправляй Telegram и не открывай checkout/payment. Проверь только назначенный sanitized
evidence slice и верни PASS или STOPPED с точными метриками. Любой secret маскируй. Твой PASS не
разрешает следующий этап автоматически: общее решение принимает Integration Captain после barrier.
```

#### Completion packet каждого агента

```text
Lane / role:
Worktree / branch:
Base SHA / agent commit SHA:
Статус: PASS | STOPPED
Allowed-files check: PASS | STOP
Изменённые файлы:
Красные тесты до реализации:
Зелёные scoped tests:
Collected tests / skip-xfail delta:
Frozen interface violations:
Cross-reviewer verdict:
QA Guardian verdict:
Protected/golden hashes unchanged: yes | STOP
Git/Chrome/production/payment actions: none
Secrets in output/artifacts: no
Риски и minimal reproduction:
Что проверить Integration Captain:
```

Worker branch сама по себе никогда не считается общей зелёной сборкой. Общий PASS существует только на
exact integrated SHA после allowlist check, cross-review, независимого QA и clean validation worktree.

---

## 3. Wave 0 — baseline до изменений

- [ ] Integration Captain сразу запускает три read-only агента prompts из §2.6; никто не ждёт окончания
  чужого аудита, пока есть своя независимая проверка.
- [ ] Agent A прочитал parser/proxy files и dirty diff; Agent B — config/orchestrator/CLI; Agent C —
  protected Render Finder flow и test topology; Captain — все архитектурные документы и общий diff.
- [ ] Текущий `HEAD`, `git status --short`, `git diff --stat`, полный diff SHA-256 и классификация каждого
  dirty-файла записаны в `baseline-manifest.json` без секретов.
- [ ] Создана integration branch `codex/nzr-integration`; после полного review сделаны два локальных
  checkpoint commits из §2.6: сначала 10 существующих hardening-файлов, затем HANDOFF/plan. Непонятный
  файл блокирует checkpoint; ничего не push.
- [ ] Зафиксированы blob hashes защищённых Render Finder Bot/matcher файлов, sanitized hash matching
  thresholds и production `TG_*` contract без вывода значений.
- [ ] Созданы `ownership.json`, `interface-freeze.md`, три ветки/worktree и уникальные temp/DB/report/cache
  paths. Write ownership не пересекается.
- [ ] Прогнан текущий unit baseline отдельным процессом:

  ```bash
  .venv/bin/python -m pytest tests/ -q -p no:cacheprovider
  ```

- [ ] Отдельно прогнан существующий golden pipeline probe:

  ```bash
  .venv/bin/python -m src.cli pipeline-probe
  ```

- [ ] Записаны фактические passed/skipped; ожидание по последней проверке — 1108 passed, 55 skipped,
  но правдой является новый фактический прогон.
- [ ] Проверены protected Telegram files: текущий `send_match_via_bot` берёт media из
  `InboundPost.photo_paths`, caption из `render_post`, а delivery state не зависит от description.
- [ ] Production read-only check перед любыми действиями: endpoints, services/timers, Alembic,
  SQLite quick check, последние monitor/smoke/nightly/weekly results. Не рестартовать.

**Гейт Wave 0 / B0:** baseline зелёный либо каждая baseline-ошибка доказана как предсуществующая и
записана; golden/parity corpus заморожен до implementation; `INTERFACE FREEZE` опубликован; ownership
не пересекается. С неизвестным красным baseline не продолжать и write-access агентам не выдавать.

**Captain exception — 2026-08-24, по прямому указанию Дмитрия:** независимый QA Guardian был повторно
перезапущен после закрытия шести завершённых worker-ов, но один runtime завис на полном прогоне, а
bounded replacement завершился после проверки только exact HEAD и не успел выполнить остальные checks.
Это не является B0 PASS. Чтобы не оставлять implementation неподвижной из-за недоступного worker
runtime, Captain открывает provisional P1 write batch на clean `aeb8a87` с сохранением всех protected
hashes и без production/Chrome/payment действий. Provisional P1 commits не могут закрыть B0/B1/B2,
не могут быть merged в release wiring и требуют fresh independent QA на exact integrated SHA перед
любым deployment или activation.

---

## 4. Wave 1 — тесты list snapshot (TDD, production не трогать)

**Lane P1:** Agent A `snapshot_parser`, параллельно с Agent B `proxy_config` и Agent C Wave 4 после B0.

Изменяемые файлы:

- `tests/test_scrapers_nekretnine.py`;
- новый `tests/fixtures/nekretnine/list_snapshot.json` или маленький synthetic fixture без полного
  копирования живой страницы;
- при необходимости `tests/conftest.py`.

Сначала красные тесты:

- [ ] `test_list_preview_contains_complete_snapshot` — price/area/rooms/location/photos.
- [ ] `test_snapshot_photo_ids_become_ordered_unique_xxl_urls`.
- [ ] `test_snapshot_missing_required_field_is_incomplete`.
- [ ] `test_old_list_fixture_without_snapshot_fields_still_parses_preview`.
- [ ] `test_snapshot_never_uses_non_nekretnine_photo_host_as_direct_cdn`.
- [ ] `test_list_snapshot_matches_detail_for_bot_visible_fields` — price, area, rooms, district,
  address, lat/lon, title, ordered photo IDs.
- [ ] Edge cases: missing price, missing address, missing coordinates, empty photos, duplicated photo ID,
  malformed `properties`, string/number locale formats.

Не использовать live network в unit tests.

**Гейт Wave 1:** тесты красные именно из-за отсутствующей функции, а не из-за плохой fixture.

---

## 5. Wave 2 — parser implementation

**Lane P1:** Agent A `snapshot_parser`; Agent B/C не редактируют перечисленные ниже файлы в этом batch.

Изменяемые файлы:

- `src/scrapers/base.py`;
- `src/scrapers/nekretnine.py`;
- тесты Wave 1.

Задачи:

- [ ] Добавить optional snapshot в preview без изменения других scraper constructors.
- [ ] Переиспользовать `_detail_from_real_estate`; не создавать второй расходящийся parser.
- [ ] Научить `_photo_urls` принимать list payload с `id + urls.small` и строить XXL URL.
- [ ] Добавить чистый helper `is_snapshot_complete`/эквивалент с явным контрактом.
- [ ] Сохранить canonical numeric URL `/oglasi/<id>/`.
- [ ] Сохранить list fingerprint; расширить его только структурными keys, без values.
- [ ] Не менять fetch/retry/browser logic в этой волне.

Команда гейта:

```bash
.venv/bin/python -m pytest tests/test_scrapers_nekretnine.py tests/test_scrape_smoke.py -q
```

---

## 6. Wave 3 — orchestrator active/shadow path

**Lane P2:** Agent A `orchestrator_active_shadow`, параллельно с Agent B audit core и Agent C integrated
QA. Batch стартует только от P1 integration SHA; parser/proxy интерфейсы берутся из
`interface-freeze.md` и интегрированного кода, не из старого worker worktree.

Изменяемые файлы:

- `src/scrapers/orchestrator.py`;
- новый узкий snapshot-ingestion helper, если это уменьшает конфликтность;
- новые изолированные orchestrator tests. `src/cli.py` подключается только в P3.

Сначала тесты:

- [ ] `legacy` вызывает detail точно как сейчас.
- [ ] `shadow` сравнивает, но DB result создаётся legacy detail data.
- [ ] `active + complete` не вызывает `fetch_detail` для нового listing.
- [ ] `active + incomplete` вызывает legacy detail ровно один раз.
- [ ] Existing listing merge не заменяет non-null field на `None`.
- [ ] Existing listing с теми же photo IDs не скачивает фото повторно.
- [ ] Изменившийся photo set заменяется только после полного успешного download/hash/embed.
- [ ] Ошибка скачивания оставляет старые URLs/hashes/embeddings.
- [ ] Price change даёт тот же `PriceChange`/alert contract и не дублируется.
- [ ] Empty shell не вставляется.
- [ ] 4zida/Halooglasi/CityExpert идут по старому пути без изменения результата.
- [ ] Невалидный `NEKRETNINE_LIST_SNAPSHOT_MODE` fail-fast.

Implementation notes:

- Не расширять `_process_one` десятком platform-specific `if`; выделить узкий resolution/merge helper.
- Snapshot/fallback decision должен быть тестируемым без сети и БД.
- Photo preparation делать вне DB transaction, DB swap — коротким атомарным update.
- В shadow mismatch логировать только listing ID и названия полей, не полный raw payload/address.

Гейт:

```bash
.venv/bin/python -m pytest \
  tests/test_scrapers_nekretnine.py \
  tests/test_pipeline.py \
  tests/test_nightly_steps.py \
  tests/test_link_verify.py -q
```

---

## 7. Wave 4 — абсолютный Render Finder Bot regression gate

**Lane:** Agent C `zero_regression_qa`; он создаёт oracle до implementation и проверяет exact integrated
SHA после каждого merge batch. Production-код Agent C не меняет.

Добавить новый файл `tests/test_render_finder_zero_regression.py`. Защищённые production-файлы не
редактировать.

Golden corpus и его manifest заморозить в B0, **до implementation**. Golden нельзя генерировать заново
внутри финального test run. Любое изменение его hash = `STOP` и отдельное решение Дмитрия.

Тест строит две независимые Listing версии одной квартиры из двух независимых raw fixtures, а не из
одного уже нормализованного объекта:

1. legacy detail parser;
2. list snapshot active path.

Для одного и того же `InboundPost` проверить:

- [ ] одинаковые matcher-visible fields;
- [ ] `match_inbound` возвращает одинаковые `status/listing_id/method/matched_photo_count/confidence`;
- [ ] `render_post(...).text` byte-for-byte одинаков;
- [ ] fake aiogram Bot получает одинаковый ordered media album;
- [ ] caption первого media byte-for-byte одинаков, остальные captions `None`;
- [ ] platform link и source link одинаковы;
- [ ] те же PUBLISHED/PublishedPost/auto-publish transitions;
- [ ] `NO_MATCH` по-прежнему не вызывает bot delivery;
- [ ] caption >1024 обрезается тем же способом;
- [ ] missing address/coords case даёт legacy fallback, а не исчезнувшую строку `Локация`.
- [ ] complete snapshot доказывает, что `fetch_detail` действительно не был вызван.
- [ ] удалить address/coordinates или photo ID → обязательный legacy fallback.
- [ ] убрать snapshot mapping → parity test обязан стать красным.
- [ ] сломать primary/secondary proxy → metered fallback либо честная ошибка; silent green невозможен.
- [ ] ни один comparison не нормализует caption/media; разрешены только заранее перечисленные невидимые
  dynamic message IDs/timestamps.

Расширить `pipeline-probe` не обязательно. Если расширяется, только новым `--render-check` внутри
изолированной in-memory DB, без Telegram network и без изменения default поведения команды.

Финальный hash gate:

```bash
git diff --exit-code -- \
  src/telegram/monitor.py src/jobs/pipeline.py \
  src/telegram/bot_delivery.py src/telegram/delivery.py \
  src/post/template.py src/matching
```

**Гейт Wave 4:** protected diff пуст; все parity assertions абсолютны и зелёные.

Дополнительно проверить exact integrated SHA, test collection count, нулевой прирост skip/xfail и
sanitized hashes matching thresholds/`TG_*`. Parity suite запустить два раза подряд на одном clean SHA:
PASS после повторного запуска при первом failure считается flaky failure, а не green.

---

## 8. Wave 5 — ISP proxy pool и failover

**Разделённый ownership P1/P2/P3:** в P1 Agent B владеет `config.py`, `.env.example`, отдельным
`proxy_pool.py` и tests, а Agent A — вызовом frozen helper из `base.py`/`nekretnine.py`. В P2 Agent A
подключает telemetry к orchestrator; в P3 Agent A подключает summary к CLI. Один файл не редактируется
двумя агентами одновременно.

Изменяемые файлы:

- `src/config.py`;
- `.env.example`;
- новый `src/scrapers/proxy_pool.py` или эквивалентный изолированный helper;
- `src/scrapers/base.py` только для вызова frozen interface, если это действительно необходимо;
- `src/scrapers/nekretnine.py`;
- `src/scrapers/orchestrator.py` в P2 и `src/cli.py` в P3 только для telemetry plumbing;
- proxy tests.

Тесты до кода:

- [ ] новые поля пусты → старый `SCRAPER_PROXY_URL` работает как раньше;
- [ ] static ISP password не получает `_session-...` suffix;
- [ ] rotating legacy password продолжает получать bounded session suffix;
- [ ] primary success → secondary не используется;
- [ ] primary challenge exhaustion → новый profile + secondary;
- [ ] secondary failure → metered fallback;
- [ ] все failures → честная ошибка, не empty result и не silent success;
- [ ] proxy URL/password отсутствуют в `caplog` и exception text;
- [ ] asset blocking image/media/font/stylesheet сохраняется;
- [ ] direct CDN photo downloader не использует proxy;
- [ ] другие платформы не используют proxy.

Не добавлять новый proxy SDK/dependency: обычных HTTP(S) proxy настроек Playwright/httpx достаточно.

---

## 9. Wave 6 — read-only shadow audit command

**Lane P2/P3:** Agent B `shadow_audit_core` делает новый узкий модуль и tests без изменения CLI. После
P2 integration Agent A `cli_wiring` в новом worktree подключает только тонкую команду. Agent C
независимо доказывает no-write/no-Telegram на exact integrated SHA.

Добавить CLI:

```text
otz nekretnine-shadow-audit \
  --max-pages 500 \
  --compare-details 100 \
  --report data/reports/nekretnine-shadow-YYYYMMDD.json
```

Обязательный контракт команды:

- не пишет Listings/Matches/Inbound/SiteListing;
- не выполняет stale sweep;
- ничего не отправляет в Telegram;
- секреты и полные addresses в report не пишет;
- проходит полный list crawl;
- для deterministic sample до 100 listings получает legacy detail и сравнивает normalized fields;
- report содержит counts/rates, mismatched field names и listing IDs;
- exit 0 только при прохождении quality thresholds.

Thresholds:

- `skipped_pages == 0`;
- coverage ≥98% текущего `site_total` и не хуже последнего healthy baseline;
- 100% sample совпадений по `external_id`, URL, price, area, rooms, district, coordinates и ordered
  photo ID set **либо** snapshot корректно classified incomplete и отправлен в fallback;
- ни один complete snapshot не теряет bot-visible location line относительно legacy;
- legacy fallback rate записан; ориентир по живой странице <15%, но качество важнее — больший rate не включает active mode,
  пока причина не понята;
- browser/proxy errors = 0 после допустимого failover.

Команда и report получают unit tests с fake scraper, без live network.

### Captain continuation — 2026-08-25

При отсутствии user-side blocker, но сохранённом worker-runtime STOP, Captain завершил локальный
P2/P3 continuation на exact integration code SHA `d8b298a1dd2b08080a9d78bdc1fa382228251325` в рамках
документированного provisional exception. Добавлены `src/scrapers/shadow_audit.py`, его offline tests
и тонкая CLI-команда `nekretnine-shadow-audit`; audit не импортирует DB/orchestrator/Telegram и пишет
только явно указанный sanitized report. Proxy pool получил реальный bounded failover и non-secret
counters для challenge/fallback evidence.

Фактическая проверка: `1136 passed, 55 skipped, 34 warnings`; focused shadow/parser/proxy/CLI batch —
`57 passed`; `pipeline-probe` — `golden-0001`, confidence `1.000`, `phash`; `git diff --check` clean.
Последующий proxy telemetry follow-up отделяет recoverable `proxy_challenges` от residual
`browser_proxy_errors`/`proxy_errors`, поэтому успешный failover не блокирует audit сам по себе.
Это не закрывает B0/B1/B2: свежий независимый QA PASS на exact integrated SHA всё ещё обязателен;
Chrome, payment, production env/restart/deploy и live activation остаются запрещены до этого PASS.

### P2 telemetry plumbing — 2026-08-25

На exact code chain `2fcb3b35a4adcbb511febe770605d5482d440555` telemetry подключена к обычному
`scrape_platform()`/`ScrapeResult` и CLI summary: snapshot mode, complete/incomplete/fallback/active-use,
shadow mismatch, coverage и secret-safe proxy counters. Добавлены свойства `coverage_rate` и
`snapshot_fallback_rate`; DB schema и protected Render Finder path не менялись.

Focused telemetry/orchestrator tests: `16 passed`; full suite после интеграции: `1140 passed, 55 skipped,
34 warnings`; pipeline probe и два parity-run зелёные. B0 остаётся STOP до свежего независимого QA PASS.

---

## 10. Полный локальный gate до Chrome/денег

```bash
.venv/bin/python -m pytest tests/ -q -p no:cacheprovider
.venv/bin/python -m src.cli pipeline-probe
.venv/bin/ruff check src tests
git diff --check
bash -n scripts/vps_install.sh scripts/deploy_sync.sh
```

Перед общим запуском остановить запись всех worker-агентов. Agent A/B/C сначала параллельно запускают
свои shards в отдельных DB/cache, затем Integration Captain интегрирует reviewed commits и выполняет
команды выше на новом detached validation worktree exact integrated SHA. Зафиксировать collected tests,
skip/xfail delta и warnings. Critical parity suite выполнить два раза подряд на том же SHA.

Если общий ruff baseline красный из-за известного version drift, отчёт должен отделить baseline от новых
ошибок; новые/изменённые Python-файлы обязаны быть clean.

Обновить:

- `README.md` — snapshot/fallback в архитектуре;
- `HANDOFF.md` — flags, proxy pool, deploy/rollback;
- `.env.example` — placeholders без secrets;
- `docs/BACKLOG.md` — закрытое/оставшееся;
- этот план — фактические результаты каждого gate.

**До B2 не открывать checkout и ничего не покупать.** Worker-ветка или три scoped PASS не заменяют
общий clean integrated PASS.

---

## 11. Chrome flow: пробный ISP proxy на 24 часа

### 11.1 Открытие страниц

Перед Chrome одновременно: Agent A сверяет ISP checklist/official product requirements без checkout;
Integration Captain получает production preflight read-only и отдаёт sanitized evidence Agent B/C;
Agent B проверяет deploy/DB boundaries, Agent C — protected/bot fingerprints. После их отчётов Chrome с
текущей пользовательской сессией открывает **только Integration Captain** и только после B2:

1. Открыть официальный тариф:
   `https://iproyal.com/pricing/static-residential-proxies/`
2. Нажать официальный `Buy Now` для ISP/Static Residential.
3. Если требуется — войти в существующий IPRoyal account; пароль/OTP вводит Дмитрий.
4. Выбрать:

   - product: **ISP / Static Residential**, не rotating residential и не datacenter;
   - location: **Serbia**; Belgrade — если dashboard позволяет city choice;
   - quantity: **1 IP**;
   - duration: **24 hours**;
   - protocol: HTTP(S) совместимый с Playwright/httpx;
   - auto-renewal: off для trial.

Ориентир на 24.08: около $1.80. Цена не считается гарантированной.

### 11.2 Обязательная остановка перед оплатой

На checkout показать Дмитрию текстом:

- ISP/static residential;
- Serbia/Belgrade;
- 1 IP;
- 24 hours;
- auto-renewal state;
- subtotal/tax/total/currency;
- refund/KYC caveat, если показан.

Не нажимать финальную кнопку без новой явной команды. Если total >$2.00, выбран не тот продукт/срок,
появилась подписка или KYC — остановиться.

Карту/CVV/3DS/OTP/KYC Дмитрий завершает локально. После покупки агент может открыть credentials page,
но не читает/копирует secret в чат.

### 11.3 Безопасное внесение trial credential

- Агент делает backup `/opt/otz/.env` с mode 600.
- До правки вычисляет SHA-256 только по строкам `TG_` без вывода их значений.
- Дмитрий сам вставляет proxy credential в `NEKRETNINE_PROXY_PRIMARY` на сервере.
- Установить `NEKRETNINE_PROXY_KIND=static_isp` и пока
  `NEKRETNINE_LIST_SNAPSHOT_MODE=shadow`.
- После правки повторить hash `TG_`; он обязан совпасть.
- Не удалять старый `SCRAPER_PROXY_URL`: оставить как rollback/metered fallback до завершения transition.

---

## 12. Trial canary на production без влияния на Дашу

До trial deploy:

1. Проверить production host/services read-only.
2. Сделать timestamped backup DB:

   ```bash
   sqlite3 /opt/otz/data/otz.db ".backup '/opt/otz/data/backups/otz-before-snapshot-YYYYMMDD-HHMM.db'"
   sqlite3 /opt/otz/data/otz.db 'PRAGMA integrity_check;'
   ```

3. Deploy code с mode `legacy`; smoke 4/4; monitor/result unchanged.
4. Переключить только shadow после внесения trial ISP.
5. Не рестартовать site/admin; restart monitor нужен только если Settings читается при старте.

Запустить:

```bash
/opt/otz/.venv/bin/python -m src.cli nekretnine-shadow-audit \
  --max-pages 500 --compare-details 100 \
  --report /opt/otz/data/reports/nekretnine-shadow-$(date +%Y%m%d).json
```

Команду и любые production-side effects выполняет только Captain. Он sanitizes результаты и сразу
раздаёт три параллельных observer-задачи: Agent A — coverage/proxy/fallback/challenge metrics; Agent B —
DB before/after/no-write/integrity; Agent C — no-Telegram/protected hashes/pipeline parity. B3 требует
три явных PASS; молчание, timeout или «похоже нормально» = STOPPED.

Проверить:

- exit 0;
- thresholds §9;
- 4-source `scrape-smoke` green;
- `pipeline-probe` green;
- no Telegram send from audit;
- no new/changed DB rows from audit;
- proxy challenge/failover metrics;
- runtime и page count.

Если не проходит — вернуть `legacy`, не покупать monthly, сохранить sanitized report и чинить локально.

---

## 13. Chrome flow: основной monthly ISP pool

Только после зелёного trial audit:

1. Вернуться в IPRoyal dashboard через Chrome.
2. Если trial IP можно продлить без смены — сохранить проверенный IP.
3. Выбрать **2 Serbia ISP/static residential IP**, 30 days, unlimited traffic.
4. Проверить auto-renewal; default — off, пока Дмитрий отдельно не решит иначе.
5. Ориентир на 24.08: около $2.70/IP, всего около $5.40 + возможный tax.
6. На checkout снова показать exact quote и получить отдельное подтверждение.
7. Дмитрий локально завершает payment/3DS/KYC.
8. Первый credential → `PRIMARY`, второй → `SECONDARY`; старый metered pool →
   `METERED_FALLBACK` до завершения одного weekly.

Если ISP proxy не проходит DataDome, fallback-покупка — только после отдельного gate:

- открыть `https://dataimpulse.com/residential-proxies/`;
- intro $5/5 GB, country targeting Serbia, no-expiry — перепроверить live quote;
- не покупать заранее и не включать как primary.

---

## 14. Active canary без изменения Render Finder Bot

### 14.1 Сначала ручной 5-page scrape

- Captain — единственный production writer и владелец rollback token.
- Перед запуском Agent A/B/C получают sanitized baseline manifests и точные observer checklists.
- Backup DB + quick check.
- `NEKRETNINE_LIST_SNAPSHOT_MODE=active`.
- Запустить только Nekretnine, 5 страниц; не выполнять stale sweep.
- Сравнить до/после:

  - count active/new;
  - null counts price/area/photo hashes/address/coords;
  - случайную выборку updated rows;
  - photo set changes;
  - errors/fallbacks;
  - никаких Telegram sends.

Любое ухудшение null/coverage или массовая перезапись fields → немедленно `legacy` и restore DB, если
изменения реально повреждающие.

После команды три проверки идут параллельно: Agent A — crawl/coverage/skipped/fallback/proxy; Agent B —
DB diff/null growth/photo-price atomicity; Agent C — protected hashes/no-Telegram/parity evidence.
Captain разрешает monitor только после трёх PASS.

### 14.2 Затем monitor

- Перезапустить `otz-monitor` в тихое окно; Telethon watermark должен сохранить сообщения.
- Проверить `active`, `NRestarts=0`, authorized session, свежий log без secrets.
- Не отправлять Даше искусственный тест и не делать duplicate resend.
- Следующее реальное inbound проходит обычный pipeline.
- Проверить только агрегатно:

  - inbound persisted;
  - match/no_match status;
  - при match — одна delivery, те же recipients, album count и caption hash;
  - no duplicate PublishedPost;
  - inbound → delivery time не хуже baseline >10%.

Если видимый текст/album отличается хотя бы одним байтом вне динамических message IDs/timestamps —
rollback немедленно.

Agent C имеет право STOP по одному несовпадающему caption/media hash. Agent A/B в это время независимо
проверяют latency/crawl и DB/delivery uniqueness; Captain не заменяет отсутствующий observer verdict
собственной оценкой.

### 14.3 Daily и weekly gates

- Два последовательных incremental nightly: healthy, без пропущенных источников, no proxy 402.
- Один полный weekly:

  - Nekretnine coverage ≥98%, `skipped_pages=0`;
  - list browser navigations примерно соответствуют 369, а не 369+сотни обязательных details;
  - legacy detail fallback только для incomplete snapshots + smoke/rolling/heal;
  - остальные источники direct и healthy;
  - stale sweep выполняется только при healthy coverage;
  - Render Finder Bot/monitor остаётся healthy;
  - DB quick check `ok`.

---

## 15. Post-stabilization: доведение стоимости без риска качеству

Только после одного зелёного full weekly и минимум 7 дней без Render Finder Bot regressions:

- [ ] Snapshot price/photo updates доказаны на production.
- [ ] Исключить Nekretnine из generic rolling detail refresh **или** сделать его snapshot-aware.
- [ ] Heal broken Nekretnine сначала использует fresh snapshot; detail остаётся fallback.
- [ ] Detail smoke оставить: один запрос каждые 6 часов на unlimited ISP — дешёвая страховка.
- [ ] `verify_active_links`/site liveness пока оставить. Их изменение — отдельная задача после двух
  weekly и отдельного плана; экономия не стоит риска ложного снятия карточек.
- [ ] Отключить metered fallback только после подтверждения обоих ISP IP.
- [ ] Проверить IPRoyal dashboard: rotating plan/auto-renewal не должен продолжать списания.
- [ ] Сохранить DataImpulse только как optional emergency, не primary.

Целевой recurring cost: два ISP IP ≈$5.40/month вместо ≈$10/week; фактическая сумма берётся только из
live invoice/dashboard.

---

## 16. Rollback runbook

### 16.1 Parser/ingestion rollback

1. `NEKRETNINE_LIST_SNAPSHOT_MODE=legacy`.
2. Перезапустить только процессы, читающие Settings (`otz-monitor`; oneshot jobs получат mode на старте).
3. Запустить `scrape-smoke` и `pipeline-probe`.
4. Проверить следующий incremental; не откатывать DB без доказанного повреждения.

### 16.2 Proxy rollback

1. Оставить ISP credentials в `.env`, но снять primary/secondary fields или переключить kind.
2. Вернуть существующий `SCRAPER_PROXY_URL` как единственный fallback.
3. Не раскрывать credential в command history/log.

### 16.3 DB rollback

Только если canary испортил rows и это доказано SQL diff:

1. остановить writer units;
2. сохранить повреждённую DB отдельно для анализа;
3. восстановить timestamped backup;
4. quick/integrity check;
5. запустить services и smoke;
6. сверить watermark/последние inbound, чтобы не потерять сообщения.

### 16.4 Мгновенные rollback triggers

- Render Finder Bot caption/media отличается;
- новый matched inbound стал NO_MATCH на replay/live evidence;
- coverage <98%, skipped page или массовый null рост;
- другие платформы используют proxy/получают 402;
- audit/CLI отправил Telegram сообщение;
- secret появился в log;
- DB integrity не `ok`;
- monitor lost/duplicated inbound.

### 16.5 Мгновенные multi-agent STOP triggers

- worker изменил файл вне allowlist или два agents писали один файл в одном batch;
- conflict разрешён без повторного author review либо через слепой `--ours`/`--theirs`;
- protected hash, matching threshold hash, sanitized `TG_*` hash или golden hash изменился;
- golden перегенерирован после implementation или обе стороны parity построены из одного normalized object;
- новый test не попал в collection, появился новый skip/xfail или assertion/tolerance ослаблен;
- RED был вызван import/fixture/environment ошибкой, а не отсутствующим поведением;
- первый critical run красный, второй зелёный: это flaky STOP, не PASS;
- unit test использовал live network, audit записал DB/Telegram или evidence SHA не совпадает с commit;
- worker выполнил SSH/deploy/restart/Chrome/payment или получил production secret;
- одновременно появилось больше одного production operator;
- dependency или DB schema изменены без отдельного согласования;
- secret попал в branch, log, artifact или межагентный completion packet.

---

## 17. Definition of Done

- [ ] Все implementation changes сделаны в изолированных batch worktree от правильного integration SHA.
- [ ] Ни один batch не имел пересекающегося write ownership; allowlist manifests зелёные.
- [ ] Все worker branches либо интегрированы, либо явно отклонены; скрытых изменений нет.
- [ ] Каждый implementation diff прошёл независимый cross-review, Agent C подписал `QA_PASS`.
- [ ] Финальный integrated SHA одинаков во всех release/evidence manifests.
- [ ] Traceability matrix связывает каждый zero-regression пункт с test, artifact и commit SHA.
- [ ] Golden corpus создан до implementation; его hashes не изменились.
- [ ] Final test collection не уменьшился, новых skip/xfail нет; critical parity дважды зелёный на одном SHA.
- [ ] Negative controls доказали, что parity/fallback/failover tests действительно умеют краснеть.
- [ ] Protected Telegram/matching source files byte-identical исходной базе.
- [ ] Полный unit suite зелёный; фактические counts внесены в этот файл.
- [ ] `pipeline-probe` зелёный.
- [ ] New Render Finder parity test зелёный: matcher 100%, caption byte-for-byte, media identical.
- [ ] Shadow audit: full coverage, 100 normalized sample parity/fallback, no DB/Telegram writes.
- [ ] Один 24h Serbia ISP trial доказал доступ именно к Nekretnine/DataDome.
- [ ] Exact monthly quote подтверждён Дмитрием; куплены максимум 2 ISP IP.
- [ ] Secrets только в production `.env`; TG config hash не изменился.
- [ ] Active 5-page canary не ухудшил DB fields.
- [ ] Два incremental и один full weekly зелёные.
- [ ] Реальный Render Finder Bot опыт Даши не изменился.
- [ ] Другие три источника direct и healthy.
- [ ] Metered weekly spend прекращён; recurring invoice соответствует согласованному quote.
- [ ] README/HANDOFF/BACKLOG/.env.example и этот план отражают фактический итог.
- [ ] Second Brain project note обновлена без secrets.
- [ ] Chrome/payment/production выполнял только Integration Captain; QA Guardian проверил sanitized
  shadow/canary artifacts; worker-агенты не получили secrets.

---

## 18. Формат отчёта исполнителя после каждой волны

```text
Wave N: <название>
Batch / lane / role: ...
Worktree / branch: ...
Base SHA / worker commit SHA / integrated SHA: ...
Статус: PASS | STOPPED
Allowed-files check: PASS | STOP
Изменённые файлы: ...
Красный тест до кода: ...
Зелёный gate: ...
Collected tests / skip-xfail delta: ...
Cross-reviewer verdict: REVIEW_PASS | STOP
QA Guardian verdict: QA_PASS | STOP
Golden hashes unchanged: yes | STOP
Protected Render Finder diff: empty | НЕ empty (STOP)
Production/Chrome действия: none | exact read-only/action
Деньги потрачены: $0 | exact approved quote
Секреты в выводе: no
Artifacts: ...
Риски/расхождения: ...
Следующая разрешённая волна: ...
```

Нельзя писать «всё работает», если не проверены public endpoints, services/timers, DB/migrations,
smoke, pipeline probe, logs и один реальный inbound outcome после activation.
