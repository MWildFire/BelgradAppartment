# Водяные знаки площадок: команда `/clean` в RentFinderBot — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> В этом проекте задачи выполняют отдельные сессии Claude Code на Haiku 5.5 (раздел «Сессии и запуск»). Код и тесты в задачах проверены заранее: переносить их дословно, а не переписывать.

**Goal:** В RentFinderBot появляется команда `/clean` и приём фото: бот возвращает фото без водяных знаков Halo Oglasi, 4zida и CityExpert. По отдельным флагам те же чистые фото уходят Дарье кандидатами и показываются на сайте rentbelgrad.com.

**Architecture:** Новый пакет `src/watermark/`: шаблоны знаков с измеренной геометрией (PNG покрытия + JSON) → поиск каждого знака возле предсказанного места (корреляция контуров) → удаление: точное «вычитание» полупрозрачного знака (CityExpert) или дорисовка нейросетью LaMa (Halo Oglasi, 4zida; без torch — OpenCV) → чистые копии в `data/clean/` с отчётом рядом. Оригиналы не меняются никогда. Бот получает один общий long-polling демон `otz bot` (юнит `otz-bot.service`): у токена может быть только один поллер, поэтому туда же переезжает `/start` подписок. Доставка кандидатов и сайт берут чистые копии только при включённых флагах.

**Tech Stack:** Python 3.11+ (прод 3.12), OpenCV (`opencv-python-headless`, новая зависимость), numpy, Pillow, PyTorch из уже существующего extra `clip` + TorchScript-модель big-lama (Apache-2.0, 206 МБ), aiogram 3, click, SQLAlchemy async, pytest.

## Global Constraints

- Исполнители: Claude Code, модель `claude-haiku-5-5`, `--effort max`, `--dangerously-skip-permissions`, отдельные сессии в терминале (раздел «Сессии и запуск»).
- База: `origin/main` (на 14.10.2026 — `91d6676`). Рабочая ветка `feat/watermark-remover` в worktree `~/Projects/BelgradAppartment-wm`. Основной checkout `~/Projects/BelgradAppartment` (ветка `codex/nightly-full-20260914`) не менять: никаких checkout/stash/reset/commit в нём.
- Ровно одна новая зависимость: `opencv-python-headless` — `"opencv-python-headless>=4.13"` в `[project] dependencies` и `"opencv-python-headless==4.14.0.94"` в `[tool.uv] constraint-dependencies`; `uv.lock` обновляется только командой `uv lock`. torch не добавлять в основные зависимости (он в extra `clip`).
- Оригиналы фото (`data/inbound/**`, `data/listings/**`) никогда не изменяются и не удаляются. Чистые копии — только в `WATERMARK_CLEAN_DIR` (по умолчанию `data/clean`, вне `data/inbound`). Единственные файлы, которые код удаляет: устаревшие чистые копии и (в `otz wm-revert`) сгенерированные превью `<фото>.card.webp`/`<фото>.detail.webp`, которые тут же пересобираются.
- Все новые флаги по умолчанию выключены: `WATERMARK_BOT_ENABLED=false`, `WATERMARK_CLEAN_ON_DELIVERY=false`, `WATERMARK_SITE_USE_CLEAN=false`. С выключенными флагами бот, доставка и сайт ведут себя как раньше.
- Один поллер на токен: `otz-bot` заменяет `otz-subscription-bot`; одновременно их не запускать.
- Доставка важнее чистки: любая ошибка чистки при отправке кандидата → уходят оригиналы.
- Lint-гейт проекта (`scripts/check_changed_lint.py`, ruff 0.15.21 из `uv.lock`): ноль новых диагностик. Комментарии и docstrings нового кода — на английском; русские тексты для пользователя помечаются `# noqa: RUF001` только там, где ruff на них ругается (как в `src/admin/routes.py`).
- Тесты офлайн: без сети и без настоящего Telegram (поддельные бот и сообщения), картинки генерируются в `tmp_path`, в репозитории нет фото. Тесты LaMa пропускаются без torch или файла модели.
- В сессиях 0, A–D и R никаких действий на сервере: не ssh, не `/opt/otz/.env`, не systemctl, не деплой. Никаких сообщений живым людям.
- Реальные фото-образцы (`data/wm-samples`) и модель (`data/models`) не коммитить (`data/*` в `.gitignore`).
- Не мержить в `main`, не делать force-push. Результат — ветка и draft PR.

## Проверенные факты (14.10.2026)

Исследование: ~1300 фото с площадок (Halo Oglasi 160, 4zida 200, CityExpert 144, Nekretnine.rs 150), из них 36 сохранены как образцы для QA (`~/Projects/BelgradAppartment/data/wm-samples`, рядом `expected.csv` и `README.md`).

| Площадка | Знак | Геометрия (проверено на образцах) | Как убираем |
|---|---|---|---|
| Halo Oglasi | `©www.halooglasi.com` белым в правом нижнем углу | фиксированный размер ~235×25 px, якорь — правый нижний угол | шаблон `halo_corner`: дорисовка OpenCV Telea по маске |
| Halo Oglasi | большой логотип «halo oglasi» + слоган по центру (не на всех фото) | масштаб = ширина фото / 1000 | `halo_center`: LaMa (без неё — Telea) |
| 4zida | два логотипа «4zida.rs» с кубом: левый верх и правый низ, белые ~38 % | масштаб = ширина / 1440 | `zida_logo`: LaMa; без LaMa — вычитание знака |
| CityExpert | огромный логотип по центру (иконка из трёх столбиков + «cityexpert»), белый ~20 % | масштаб = ширина / 1920 | `cityexpert_center`: вычитание знака с подбором прозрачности по частям |
| Nekretnine.rs | своих знаков нет | — | — |

- Знаки агентств внутри фото (BEOSTAN и т. п.) — не наши, не трогаем.
- На образцах: все 36 знаков найдены на своих местах, лишних срабатываний нет (каждое фото — отрицательный пример для чужих шаблонов; плюс 24 синтетических фото без знаков в тестах). Визуально после чистки знака не видно; «вычитание» на 4zida давало пятна, поэтому там LaMa.
- Время на 2 ядрах: CityExpert ~1,5 с на фото, Halo угол 0,1–0,3 с, Halo логотип 2–6 с, 4zida 2–9 с (LaMa, два знака). Загрузка LaMa ~3–19 с один раз на процесс, пик памяти ~1,1 ГБ.
- Прод: AWS, 8 vCPU / 8 ГБ, Ubuntu 24.04, Python 3.12, `/opt/otz`, torch CPU уже в окружении (extra `clip`). Релиз — `scripts/release.py` (копирует `deploy/otz-*.service`, проверяет ExecStart/User/Type/WorkingDirectory, останавливает и поднимает сервисы из `SERVICES`).
- Бот: `TG_DELIVERY_MODE=bot`; `src/telegram/bot_delivery.py` только отправляет; поллер подписок `otz-subscription-bot` на проде установлен, но выключен (`inactive`). Каждая отправка кандидата хранится в `MatchDelivery(recipient_id, message_ids, payload.photo_paths)` — по ней ответ `/clean` на альбом находит кандидата.
- Фото: `data/inbound/<source_id>/<message_id>/NN.jpg`; превью сайта лежат рядом (`<фото>.card.webp`, `<фото>.detail.webp`), манифест `data/photo_manifest.json`. Бэкапы берут `data/inbound`, `data/listings` и БД; `data/clean` в бэкап не входит (производные данные).
- CI (`.github/workflows/checks.yml`): `uv sync --frozen --group dev` (без torch) → `pytest -q --ignore=tests/e2e` → `bash -n scripts/*.sh` → `scripts/check_changed_lint.py`; отдельно e2e в браузере.
- Модель: `https://github.com/Sanster/models/releases/download/add_big_lama/big-lama.pt`, 205 669 692 байт, sha256 `344c77bbcb158f17dd143070d1e789f38a66c04202311ae3a258ef66667a9ea9`.

## Карта файлов

| Файл | Задача | Ответственность |
|---|---|---|
| `pyproject.toml`, `uv.lock` | 1 | зависимость OpenCV |
| `src/config.py`, `.env.example` | 1 | флаги `WATERMARK_*` |
| `src/watermark/__init__.py`, `templates.py`, `assets/*.png|json` | 1 | шаблоны знаков и их геометрия |
| `src/watermark/detect.py` | 2 | поиск знака возле предсказанного места |
| `src/watermark/inpaint.py` | 2 | дорисовка: Telea + выбор LaMa |
| `src/watermark/remove.py` | 2 | вычитание знака / дорисовка по маске |
| `src/watermark/pipeline.py` | 2 | публичный API: `find_marks`, `clean_array`, `clean_bytes`, `clean_file` |
| `src/watermark/lama.py` | 3 | LaMa (torch, ленивая загрузка) + скачивание модели |
| `src/watermark/store.py` | 4 | чистые копии `data/clean` + отчёт `.wm.json` |
| `src/watermark/published.py` | 5 | чистые копии для публичных карточек, периодический проход |
| `src/watermark/cli.py`, `src/cli.py` | 5 | команды `otz wm-*` |
| `src/telegram/wm_bot.py` | 6 | обработчики `/clean` и фото |
| `src/telegram/bot_daemon.py`, `src/telegram/subscription_bot.py`, `src/cli.py` | 7 | единый демон `otz bot` |
| `deploy/otz-bot.service`, `scripts/release.py`, `scripts/vps_install.sh`, `docs/operations/releases.md` | 8 | юнит и релиз |
| `src/site/photos.py`, `src/site/media.py`, `src/site/og_image.py` | 9 | сайт берёт чистые копии (флаг) |
| `src/telegram/bot_delivery.py` | 10 | кандидаты без знаков (флаг) |
| `docs/operations/watermark-remover.md` | 11 | эксплуатация: включение, проверка, откат |
| `tests/watermark/*`, `tests/test_wm_bot.py`, `tests/test_bot_daemon.py`, `tests/test_bot_unit.py`, `tests/site/test_watermark_site.py`, `tests/test_bot_delivery_clean.py` | 1–10 | тесты |

## Сессии и запуск

```text
Сессия 0  подготовка: worktree, план, окружение, базовый прогон тестов   (Task 0)
   ↓
Сессия A  ядро: шаблоны, поиск, удаление, LaMa, чистые копии, CLI        (Task 1–5)
   ↓
Сессия R1 ревью A отдельным агентом
   ↓
Сессия B  бот: /clean, единый демон, юнит      ┐ параллельно,
Сессия C  сайт и доставка кандидатов           ┘ разные worktree           (Task 6–8 | 9–10)
   ↓
Сессия D  интеграция, полный прогон, QA на реальных фото, runbook, PR    (Task 11–12)
   ↓
Сессия E  поправки по независимому ревью плана                           (Task 13)
   ↓
Сессия R2 финальное ревью всей ветки
   ↓
Прод — только после «да» Дмитрия, по docs/operations/releases.md и runbook (не Haiku)
```

Каждую сессию запускать в своей вкладке терминала, следующую — после отчёта предыдущей (B и C — одновременно, в двух вкладках). Команды ниже готовы к вставке целиком.

**Сессия 0** (из основного каталога проекта):

```bash
cd ~/Projects/BelgradAppartment && claude --model claude-haiku-5-5 --effort max --dangerously-skip-permissions -n wm-0 "Ты — сессия 0 проекта «водяные знаки». Прочитай целиком docs/superpowers/plans/2026-10-14-watermark-remover.md. Выполни только Task 0, шаг за шагом, проверяя каждый шаг. Текущий checkout (ветка codex/nightly-full-20260914) не меняй: никаких checkout/stash/reset/commit в нём. Никаких действий на сервере. Если что-то идёт не так, как написано, — остановись и опиши. В конце отчитайся по-русски простыми словами: где рабочая копия, закоммичен ли план, что показал базовый прогон тестов."
```

**Сессия A** (ядро):

```bash
cd ~/Projects/BelgradAppartment-wm && claude --model claude-haiku-5-5 --effort max --dangerously-skip-permissions -n wm-a "Ты — сессия A проекта «водяные знаки». Прочитай целиком docs/superpowers/plans/2026-10-14-watermark-remover.md (Global Constraints, «Правила для всех сессий»). Выполни Task 1–5 строго по плану (skill superpowers:executing-plans, если доступен): код и тесты переноси из плана дословно, каждый шаг проверяй командой из плана, коммит после каждой задачи. Если якорь правки не найден или тест ведёт себя не так, как в плане, — остановись и опиши, не придумывай обходов. Работай только в этом каталоге, ничего не делай на сервере. В конце — отчёт по-русски простыми словами: что готово, сколько тестов прошло, были ли отклонения."
```

**Сессия R1** (ревью ядра; после отчёта A):

```bash
cd ~/Projects/BelgradAppartment-wm && claude --model claude-haiku-5-5 --effort max --dangerously-skip-permissions -n wm-r1 "Ты — ревьюер (сессия R1); код писал не ты. Прочитай docs/superpowers/plans/2026-10-14-watermark-remover.md: Global Constraints, Task 1–5 и раздел «Ревью». Сверь изменения ветки с планом по чек-листу «Ревью», прогони команды проверки. Подтверждённые ошибки исправь минимально, с тестом, отдельным коммитом «review: …». Спорное не правь — опиши. Отчёт сохрани в docs/reviews/2026-10-14-watermark-review-a.md и закоммить. Ничего не делай на сервере. В конце — короткий отчёт по-русски простыми словами."
```

**Сессия B** (бот; запускать одновременно с C):

```bash
cd ~/Projects/BelgradAppartment-wm && git worktree add ../BelgradAppartment-wm-bot -b feat/wm-bot && cd ../BelgradAppartment-wm-bot && uv sync && claude --model claude-haiku-5-5 --effort max --dangerously-skip-permissions -n wm-b "Ты — сессия B (бот) проекта «водяные знаки». Прочитай целиком docs/superpowers/plans/2026-10-14-watermark-remover.md. Выполни Task 6–8 строго по плану (superpowers:executing-plans, если доступен): код и тесты дословно из плана, проверка каждого шага, коммит после каждой задачи. Работай только в этом каталоге (ветка feat/wm-bot). Параллельно сессия C меняет другие файлы в соседнем каталоге — не трогай её файлы и не мержи ветки. Ничего не делай на сервере, не отправляй сообщений живым людям. В конце — отчёт по-русски простыми словами."
```

**Сессия C** (сайт и доставка; одновременно с B):

```bash
cd ~/Projects/BelgradAppartment-wm && git worktree add ../BelgradAppartment-wm-site -b feat/wm-site && cd ../BelgradAppartment-wm-site && uv sync && claude --model claude-haiku-5-5 --effort max --dangerously-skip-permissions -n wm-c "Ты — сессия C (сайт и доставка) проекта «водяные знаки». Прочитай целиком docs/superpowers/plans/2026-10-14-watermark-remover.md. Выполни Task 9–10 строго по плану (superpowers:executing-plans, если доступен): код и тесты дословно из плана, проверка каждого шага, коммит после каждой задачи. Работай только в этом каталоге (ветка feat/wm-site). Параллельно сессия B меняет другие файлы в соседнем каталоге — не трогай её файлы и не мержи ветки. Ничего не делай на сервере. В конце — отчёт по-русски простыми словами."
```

**Сессия D** (интеграция и QA; после отчётов B и C):

```bash
cd ~/Projects/BelgradAppartment-wm && claude --model claude-haiku-5-5 --effort max --dangerously-skip-permissions -n wm-d "Ты — сессия D (интеграция и проверка) проекта «водяные знаки». Прочитай целиком docs/superpowers/plans/2026-10-14-watermark-remover.md. Выполни Task 11–12 строго по плану: слей ветки feat/wm-bot и feat/wm-site, полный прогон тестов и lint, проверка на реальных фото с LaMa и просмотр каждой картинки *.marks.jpg глазами, runbook, push ветки и draft PR. В main не мержить, ничего не делать на сервере. Параметры алгоритма самостоятельно не менять: если чистка где-то плохая — опиши, какие файлы и что видно. В конце — отчёт по-русски простыми словами и ссылка на PR."
```

**Сессия E** (поправки по ревью; после отчёта D):

```bash
cd ~/Projects/BelgradAppartment-wm && claude --model claude-haiku-5-5 --effort max --dangerously-skip-permissions -n wm-e "Ты — сессия E проекта «водяные знаки». Прочитай целиком docs/superpowers/plans/2026-10-14-watermark-remover.md, особенно Task 13. Выполни Task 13 по пунктам: для каждого пункта сначала тест, который падает, потом правка, потом зелёные тесты и lint-гейт, отдельный коммит на пункт. Не меняй ничего сверх описанного. После всех пунктов — полный прогон тестов (сравнение с data/wm-baseline-tests.txt), повтор QA из Task 11 Step 4–5 и git push. В main не мержить, ничего не делать на сервере. В конце — отчёт по-русски простыми словами."
```

**Сессия R2** (финальное ревью; после отчёта E):

```bash
cd ~/Projects/BelgradAppartment-wm && claude --model claude-haiku-5-5 --effort max --dangerously-skip-permissions -n wm-r2 "Ты — финальный ревьюер (сессия R2); код писал не ты. Прочитай docs/superpowers/plans/2026-10-14-watermark-remover.md целиком. Проверь всю ветку против плана по разделу «Ревью» (git diff origin/main...HEAD), прогони все команды проверки, посмотри отчёт QA. Подтверждённые ошибки исправь минимально, с тестом, отдельными коммитами «review: …», затем git push. Спорное не правь — опиши. Отчёт — docs/reviews/2026-10-14-watermark-review-final.md (закоммитить и запушить). В main не мержить, ничего не делать на сервере. В конце — отчёт по-русски простыми словами: можно ли выпускать и что осталось решить Дмитрию."
```

## Правила для всех сессий

- Перед началом: `git status` чистый, ветка правильная (`git branch --show-current`). Если нет — остановиться и описать.
- Код и тесты из плана переносить дословно. Если файл уже существует с другим содержимым или якорь для правки не находится (репозиторий мог уйти вперёд), найти то же место по смыслу и внести ту же правку; если места нет — остановиться и описать.
- Не ослаблять и не удалять тесты ради зелёного прогона. Падение «чужого» теста сверять с базовым прогоном из Task 0 (файл `data/wm-baseline-tests.txt (в worktree)`): новые падения — остановиться и описать.
- Каждая задача заканчивается своими тестами и коммитом. Сообщения коммитов — как в плане.
- Числа `passed` в Expected даны по состоянию `origin/main` на 14.10.2026. Если в репозитории с тех пор появились тесты, число может быть больше — важно, чтобы было 0 failed и столько же skipped.
- Сеть: только `uv`/`pip` и (в Task 11) скачивание модели и push. Никаких запросов к площадкам и Telegram.
- Отчёт в конце — по-русски, простыми словами, без лишних технических деталей.

## Ревью (для сессий R1 и R2)

Чек-лист:

1. `git log --oneline origin/main..HEAD` — коммиты задач по порядку, без посторонних изменений; `git diff --stat origin/main...HEAD` — только файлы из «Карты файлов» (плюс `uv.lock`, план и отчёты в `docs/`).
2. Код совпадает с планом (точечные отличия допустимы только как описанные в отчётах сессий).
3. Инварианты: оригиналы не пишутся и не удаляются (`grep -rnE "unlink|write_bytes|os.replace" src/watermark src/telegram/wm_bot.py` — только `data/clean`, `.part`/`.tmp` и превью в `wm-revert`); флаги по умолчанию `False`; при ошибке чистки доставка шлёт оригиналы; один поллер на токен; нет сетевых вызовов в тестах.
4. Команды проверки (все должны пройти):

```bash
uv run pytest -q tests/watermark tests/test_wm_bot.py tests/test_bot_daemon.py tests/test_bot_unit.py tests/site/test_watermark_site.py tests/test_bot_delivery_clean.py tests/test_subscription_cli.py tests/test_subscription_telegram.py tests/test_release.py
uv run python scripts/check_changed_lint.py
for s in scripts/*.sh; do bash -n "$s"; done
```

(Для R1 — только существующие на тот момент файлы тестов; в R2 дополнительно полный прогон `uv run pytest -q --ignore=tests/e2e` и сравнение с `data/wm-baseline-tests.txt (в worktree)`.)

5. Отчёт: что проверено, что найдено (с файлом и строкой), что исправлено, что оставлено на решение и почему.

---

### Task 0: Рабочая копия, план и окружение (сессия 0)

**Files:**
- Create: `~/Projects/BelgradAppartment-wm` (git worktree, ветка `feat/watermark-remover` от `origin/main`)
- Create: `docs/superpowers/plans/2026-10-14-watermark-remover.md`, `docs/superpowers/plans/assets/2026-10-14-watermark/*` (копии из основного каталога)

**Interfaces:**
- Produces: worktree с закоммиченным планом и шаблонами; окружение `uv` с extra `clip`; `data/wm-baseline-tests.txt` (в worktree, `data/` не в git) — какие тесты падали до изменений.

- [ ] **Step 1: Проверить исходное состояние (только чтение)**

```bash
cd ~/Projects/BelgradAppartment
git fetch origin
git log -1 --format='%h %ci %s' origin/main
ls data/wm-samples docs/superpowers/plans/assets/2026-10-14-watermark
```

Expected: коммит `91d6676` или новее; в `data/wm-samples` — `4zida cityexpert halooglasi expected.csv README.md`; в assets — 8 файлов (`*.png`, `*.json`).

- [ ] **Step 2: Создать worktree**

```bash
cd ~/Projects/BelgradAppartment
git worktree add ../BelgradAppartment-wm -b feat/watermark-remover origin/main
```

Expected: `Preparing worktree (new branch 'feat/watermark-remover')`. Если ветка или каталог уже есть — остановиться и описать.

- [ ] **Step 3: Перенести план и шаблоны в worktree**

```bash
cd ~/Projects/BelgradAppartment
mkdir -p ../BelgradAppartment-wm/docs/superpowers/plans/assets
cp docs/superpowers/plans/2026-10-14-watermark-remover.md ../BelgradAppartment-wm/docs/superpowers/plans/
cp -R docs/superpowers/plans/assets/2026-10-14-watermark ../BelgradAppartment-wm/docs/superpowers/plans/assets/
cd ../BelgradAppartment-wm/docs/superpowers/plans/assets/2026-10-14-watermark && shasum -a 256 *
```

Expected (ровно так):

```text
83ce35ed3b3e22b611711d859fcad0a3030d881491fcd964cd92b312b274043b  cityexpert_center.json
17fccc34dd4590dfbe279c0a7db93783eede16e9c0623739ea4aafbf6bc302f0  cityexpert_center.png
17692f40b3383938a3aca2af0d9fb07856f791f8f3307ec05193c925714af26b  halo_center.json
39fefa477fcd6df1e6321332b39f70c239150f18ccd0552eadfb2da7d82def8b  halo_center.png
d7690f41628827284cfc30d7e58c18ee582a001d3f6828cda6031837d46ab7b2  halo_corner.json
5f8e424349c489292ad74571f2c4057808a17223a708a4f607bc0ac76843dd86  halo_corner.png
aa1c6d682e6bcedfac273bcd9676d1e80e9bca2d8e125d890be744602e0b49e4  zida_logo.json
ca76584a6bfb1f4e6150a93f28f010db23cdb58df5d08bd6722ac8c5a4c6ca58  zida_logo.png
```

- [ ] **Step 4: Окружение**

```bash
cd ~/Projects/BelgradAppartment-wm
uv sync --extra clip
uv run python -c "import torch; print(torch.__version__)"
```

Expected: синхронизация без ошибок; печатается `2.12.0`.

- [ ] **Step 5: Базовый прогон тестов (до любых изменений, ~10 минут)**

```bash
cd ~/Projects/BelgradAppartment-wm
uv run pytest -q -p no:cacheprovider --ignore=tests/e2e > data/wm-baseline-tests.txt 2>&1; tail -n 3 data/wm-baseline-tests.txt
grep -oE "^(FAILED|ERROR) [^ ]+" data/wm-baseline-tests.txt || echo "no baseline failures"
```

Expected: итоговая строка `N passed …`. Список падений (если есть) записать в отчёт: это «чужие» падения, с ними сравниваются следующие прогоны.

- [ ] **Step 6: Закоммитить план**

```bash
cd ~/Projects/BelgradAppartment-wm
git add docs/superpowers/plans/2026-10-14-watermark-remover.md docs/superpowers/plans/assets/2026-10-14-watermark
git commit -m "docs: watermark remover plan and templates"
```

- [ ] **Step 7: Убрать временные копии из основного каталога**

```bash
cd ~/Projects/BelgradAppartment
cmp docs/superpowers/plans/2026-10-14-watermark-remover.md ../BelgradAppartment-wm/docs/superpowers/plans/2026-10-14-watermark-remover.md \
  && diff -r docs/superpowers/plans/assets/2026-10-14-watermark ../BelgradAppartment-wm/docs/superpowers/plans/assets/2026-10-14-watermark \
  && rm -rf docs/superpowers/plans/2026-10-14-watermark-remover.md docs/superpowers/plans/assets/2026-10-14-watermark
rmdir docs/superpowers/plans/assets 2>/dev/null || true
git status --short
```

Expected: копии совпали и удалены; `git status` основного каталога такой же, как до начала (план теперь живёт в worktree и в ветке; неотслеживаемые копии в основном каталоге потом мешали бы переключению на ветку с планом). Больше в основном каталоге ничего не удалять; образцы `data/wm-samples` нужны в Task 11.

---

### Task 1: Зависимость OpenCV, флаги, шаблоны знаков

**Files:**
- Modify: `pyproject.toml`, `uv.lock`
- Modify: `src/config.py` (после `notify_no_match_enabled: bool = True`)
- Modify: `.env.example` (в конец)
- Create: `src/watermark/__init__.py`, `src/watermark/templates.py`, `src/watermark/assets/` (8 файлов)
- Test: `tests/watermark/__init__.py`, `tests/watermark/synth.py`, `tests/watermark/test_templates.py`

**Interfaces:**
- Produces: `src.watermark.templates` — `ASSETS_DIR: Path`; `Instance(anchor, dx, dy)`; `Placement(instance: int, x: float, y: float, scale: float)`; `WatermarkTemplate` (поля `id, platform, title, coverage (float32 HxW 0..1), opacity: tuple[float,float,float], strategy ("reverse_blend"|"inpaint"), inpaint_backend ("lama"|"telea"), ref_width, scale_mode ("width"|"fixed"), instances, scale_search, pos_tol, min_score, lama_fallback ("telea"|"reverse_blend")`; свойства `width`, `height`; методы `scale_for(image_width) -> float`, `predicted(image_width, image_height) -> list[Placement]`); `TemplateError`; `load_template(json_path) -> WatermarkTemplate`; `load_templates(directory: str | None = None) -> tuple[WatermarkTemplate, ...]` (кэш, сортировка по id).
- Produces: настройки `watermark_bot_enabled: bool = False`, `watermark_allowed_chat_ids: str = ""`, `watermark_clean_dir: Path = PROJECT_ROOT/"data"/"clean"`, `watermark_lama_enabled: bool = True`, `watermark_lama_model_path: Path = PROJECT_ROOT/"data"/"models"/"big-lama.pt"`, `watermark_clean_on_delivery: bool = False`, `watermark_site_use_clean: bool = False`, `watermark_jpeg_quality: int = 95`.
- Produces (тесты): `tests.watermark.synth` — `make_background(width, height, seed=0)`, `jpeg_roundtrip(img, quality=88)`, `apply_mark(img, tpl, *, opacity_scale=1.0)`, `mark_mask(tpl, width, height, *, dilate=4)`.

- [ ] **Step 1: Зависимость**

В `pyproject.toml`, список `dependencies` — найти строку и добавить следующую за ней:

```toml
    "Pillow>=10.4.0",
    "opencv-python-headless>=4.13",
```

В `[tool.uv] constraint-dependencies` — найти строку и добавить следующую за ней:

```toml
    "open_clip_torch==3.3.0",
    "opencv-python-headless==4.14.0.94",
```

```bash
uv lock
git diff uv.lock | grep '^+name = '
uv sync --extra clip
uv run python -c "import cv2; print(cv2.__version__)"
```

Expected: в diff новых пакетов только `+name = "opencv-python-headless"` (версии остальных пакетов не меняются — иначе остановиться и описать); печатается `4.14.0`.

- [ ] **Step 2: Написать падающие тесты**

`tests/watermark/__init__.py` — пустой файл.

`tests/watermark/synth.py`:

```python
"""Synthetic data for watermark tests: a "room-like" background + a template mark + JPEG.

Images are generated in memory (the repo has no photo fixtures). Mark application is
independent of src/watermark/remove.py (integer position, its own resize) so the tests check
the template geometry instead of repeating the module's own code.
"""

from __future__ import annotations

import io

import cv2
import numpy as np
from PIL import Image

from src.watermark.templates import WatermarkTemplate


def make_background(width: int, height: int, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    ys = np.linspace(0.0, 1.0, height, dtype=np.float32)[:, None]
    top = rng.uniform(150, 220, 3).astype(np.float32)
    bottom = rng.uniform(60, 140, 3).astype(np.float32)
    img = (top * (1 - ys[..., None]) + bottom * ys[..., None]) * np.ones((1, width, 1), np.float32)
    low = rng.normal(0, 25, (max(2, height // 40), max(2, width // 40), 3)).astype(np.float32)
    img += cv2.resize(low, (width, height), interpolation=cv2.INTER_CUBIC)
    for _ in range(12):  # "furniture": rectangles with sharp edges
        x0, y0 = int(rng.integers(0, width - 20)), int(rng.integers(0, height - 20))
        x1 = min(width, x0 + int(rng.integers(20, max(21, width // 3))))
        y1 = min(height, y0 + int(rng.integers(20, max(21, height // 3))))
        img[y0:y1, x0:x1] = img[y0:y1, x0:x1] * 0.5 + rng.uniform(20, 230, 3).astype(np.float32) * 0.5
    img += rng.normal(0, 6, img.shape).astype(np.float32)
    return np.clip(img, 0, 255).astype(np.uint8)


def jpeg_roundtrip(img: np.ndarray, quality: int = 88) -> np.ndarray:
    buf = io.BytesIO()
    Image.fromarray(img).save(buf, "JPEG", quality=quality)
    return np.asarray(Image.open(io.BytesIO(buf.getvalue())).convert("RGB"))


def apply_mark(img: np.ndarray, tpl: WatermarkTemplate, *, opacity_scale: float = 1.0) -> np.ndarray:
    """Apply every template instance at its predicted place (as the platform does)."""
    out = img.astype(np.float32)
    h, w = img.shape[:2]
    for p in tpl.predicted(w, h):
        tw, th = max(2, round(tpl.width * p.scale)), max(2, round(tpl.height * p.scale))
        cov = cv2.resize(tpl.coverage, (tw, th), interpolation=cv2.INTER_AREA)
        x0, y0 = round(p.x), round(p.y)
        xs0, ys0 = max(0, x0), max(0, y0)
        xs1, ys1 = min(w, x0 + tw), min(h, y0 + th)
        if xs1 <= xs0 or ys1 <= ys0:
            continue
        c = cov[ys0 - y0 : ys1 - y0, xs0 - x0 : xs1 - x0][..., None]
        alpha = c * np.asarray(tpl.opacity, np.float32)[None, None, :] * opacity_scale
        region = out[ys0:ys1, xs0:xs1]
        out[ys0:ys1, xs0:xs1] = alpha * 255.0 + (1 - alpha) * region
    return np.clip(out, 0, 255).astype(np.uint8)


def mark_mask(tpl: WatermarkTemplate, width: int, height: int, *, dilate: int = 4) -> np.ndarray:
    mask = np.zeros((height, width), np.uint8)
    for p in tpl.predicted(width, height):
        tw, th = max(2, round(tpl.width * p.scale)), max(2, round(tpl.height * p.scale))
        cov = cv2.resize(tpl.coverage, (tw, th), interpolation=cv2.INTER_AREA)
        x0, y0 = round(p.x), round(p.y)
        xs0, ys0, xs1, ys1 = max(0, x0), max(0, y0), min(width, x0 + tw), min(height, y0 + th)
        mask[ys0:ys1, xs0:xs1] |= (cov[ys0 - y0 : ys1 - y0, xs0 - x0 : xs1 - x0] > 0.05).astype(np.uint8)
    if dilate:
        mask = cv2.dilate(mask, np.ones((2 * dilate + 1, 2 * dilate + 1), np.uint8))
    return mask > 0
```

`tests/watermark/test_templates.py`:

```python
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.watermark.templates import ASSETS_DIR, TemplateError, load_template, load_templates


def _by_id():
    return {t.id: t for t in load_templates()}


def test_all_builtin_templates_load():
    tpls = _by_id()
    assert set(tpls) == {"cityexpert_center", "halo_center", "halo_corner", "zida_logo"}
    for t in tpls.values():
        assert t.coverage.dtype.name == "float32"
        assert 0.99 <= float(t.coverage.max()) <= 1.0
        assert all(0.0 < o < 0.95 for o in t.opacity)
        assert t.min_score > 0


@pytest.mark.parametrize(
    ("tid", "size", "expected"),
    [
        ("cityexpert_center", (1920, 1080), [(83.2, 365.8, 1.0)]),
        ("cityexpert_center", (900, 1600), [(39.0, 718.3, 0.46875)]),
        ("zida_logo", (1440, 1080), [(98.9, 99.9, 1.0), (1059.23, 700.21, 1.0)]),
        ("zida_logo", (810, 1080), [(55.63, 56.19, 0.5625), (595.82, 866.38, 0.5625)]),
        ("halo_center", (1000, 750), [(248.0, 271.0, 1.0)]),
        ("halo_center", (562, 750), [(139.38, 316.55, 0.562)]),
        ("halo_corner", (1000, 750), [(759.0, 725.0, 1.0)]),
    ],
)
def test_predicted_placement_matches_measured_geometry(tid, size, expected):
    placements = _by_id()[tid].predicted(*size)
    assert len(placements) == len(expected)
    for p, (x, y, s) in zip(placements, expected, strict=True):
        assert p.x == pytest.approx(x, abs=0.05)
        assert p.y == pytest.approx(y, abs=0.05)
        assert p.scale == pytest.approx(s, abs=1e-4)


def test_broken_template_is_rejected(tmp_path: Path):
    meta = json.loads((ASSETS_DIR / "zida_logo.json").read_text(encoding="utf-8"))
    (tmp_path / "zida_logo.png").write_bytes((ASSETS_DIR / "zida_logo.png").read_bytes())
    meta["opacity"] = [0.5, 0.5]
    (tmp_path / "zida_logo.json").write_text(json.dumps(meta), encoding="utf-8")
    with pytest.raises(TemplateError):
        load_template(tmp_path / "zida_logo.json")
```

- [ ] **Step 3: Убедиться, что тесты падают**

Run: `uv run pytest -q tests/watermark/test_templates.py`
Expected: ошибка сбора `ModuleNotFoundError: No module named 'src.watermark'`.

- [ ] **Step 4: Шаблоны знаков**

```bash
mkdir -p src/watermark/assets
cp docs/superpowers/plans/assets/2026-10-14-watermark/*.png docs/superpowers/plans/assets/2026-10-14-watermark/*.json src/watermark/assets/
ls src/watermark/assets
```

Expected: `cityexpert_center.json cityexpert_center.png halo_center.json halo_center.png halo_corner.json halo_corner.png zida_logo.json zida_logo.png`.

- [ ] **Step 5: Пакет и загрузчик шаблонов**

`src/watermark/__init__.py`:

```python
"""Remove listing-platform watermarks (Halo Oglasi, 4zida, CityExpert) from apartment photos.

Public API lives in `src.watermark.pipeline`: `clean_array`, `clean_bytes`, `clean_file`.
Watermark templates are in `src/watermark/assets/` (coverage PNG + geometry JSON); how they
were measured is described in docs/superpowers/plans/2026-10-14-watermark-remover.md.
"""
```

`src/watermark/templates.py`:

```python
"""Watermark templates: coverage (PNG, 0..255) + placement geometry (JSON).

Mark model: observed pixel J = a*255 + (1 - a)*O, where a = opacity[c] * coverage(x, y) and
O is the original pixel. Marks are white; opacity is per channel (R, G, B).

Geometry is expressed in the template's reference pixels. For scale_mode="width" the mark
scale in an image of width W is s = W / ref_width (CityExpert, 4zida and the big Halo Oglasi
logo behave like this). For scale_mode="fixed" s = 1.0 (the Halo Oglasi corner text has a
fixed pixel size). Top-left corner of an instance: (anchor_x + dx * s, anchor_y + dy * s),
where the anchor is the image center, top-left or bottom-right corner.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

import numpy as np
from PIL import Image

ASSETS_DIR = Path(__file__).resolve().parent / "assets"

Anchor = Literal["center", "top-left", "bottom-right"]
Strategy = Literal["reverse_blend", "inpaint"]
Backend = Literal["lama", "telea"]
LamaFallback = Literal["telea", "reverse_blend"]
ScaleMode = Literal["width", "fixed"]

_ANCHORS: frozenset[str] = frozenset({"center", "top-left", "bottom-right"})
_STRATEGIES: frozenset[str] = frozenset({"reverse_blend", "inpaint"})
_BACKENDS: frozenset[str] = frozenset({"lama", "telea"})
_LAMA_FALLBACKS: frozenset[str] = frozenset({"telea", "reverse_blend"})
_SCALE_MODES: frozenset[str] = frozenset({"width", "fixed"})


class TemplateError(ValueError):
    """Broken or incomplete watermark template."""


@dataclass(frozen=True)
class Instance:
    """One instance of a mark on the image (4zida has two)."""

    anchor: Anchor
    dx: float
    dy: float


@dataclass(frozen=True)
class Placement:
    """Predicted instance position: top-left corner and scale."""

    instance: int
    x: float
    y: float
    scale: float


@dataclass(frozen=True, eq=False)
class WatermarkTemplate:
    id: str
    platform: str
    title: str
    coverage: np.ndarray  # float32, HxW, 0..1
    opacity: tuple[float, float, float]
    strategy: Strategy
    inpaint_backend: Backend
    ref_width: int
    scale_mode: ScaleMode
    instances: tuple[Instance, ...]
    scale_search: tuple[float, ...]
    pos_tol: float
    min_score: float
    # What to do when inpaint_backend is "lama" but LaMa is unavailable (no torch/model).
    lama_fallback: LamaFallback = "telea"

    @property
    def height(self) -> int:
        return int(self.coverage.shape[0])

    @property
    def width(self) -> int:
        return int(self.coverage.shape[1])

    def scale_for(self, image_width: int) -> float:
        if self.scale_mode == "fixed":
            return 1.0
        return image_width / float(self.ref_width)

    def predicted(self, image_width: int, image_height: int) -> list[Placement]:
        """Where each instance of the mark should be on an image of this size."""
        s = self.scale_for(image_width)
        out: list[Placement] = []
        for idx, inst in enumerate(self.instances):
            if inst.anchor == "center":
                ax, ay = image_width / 2.0, image_height / 2.0
            elif inst.anchor == "top-left":
                ax, ay = 0.0, 0.0
            else:  # bottom-right
                ax, ay = float(image_width), float(image_height)
            out.append(Placement(idx, ax + inst.dx * s, ay + inst.dy * s, s))
        return out


def _require(meta: dict[str, object], key: str, path: Path) -> object:
    if key not in meta:
        raise TemplateError(f"{path.name}: missing key {key!r}")
    return meta[key]


def load_template(json_path: Path) -> WatermarkTemplate:
    """Read the <id>.json + <id>.png pair; validate fields and PNG size."""
    meta = json.loads(json_path.read_text(encoding="utf-8"))
    if not isinstance(meta, dict):
        raise TemplateError(f"{json_path.name}: top-level JSON must be an object")
    tid = str(_require(meta, "id", json_path))
    png_path = json_path.with_suffix(".png")
    if not png_path.is_file():
        raise TemplateError(f"{json_path.name}: coverage PNG {png_path.name} not found")
    with Image.open(png_path) as im:
        coverage = np.asarray(im.convert("L"), dtype=np.float32) / 255.0
    if coverage.shape != (int(meta.get("height", -1)), int(meta.get("width", -1))):
        raise TemplateError(f"{json_path.name}: PNG size {coverage.shape} != JSON width/height")
    opacity = tuple(float(v) for v in _require(meta, "opacity", json_path))  # type: ignore[attr-defined]
    if len(opacity) != 3 or not all(0.0 < v < 0.95 for v in opacity):
        raise TemplateError(f"{json_path.name}: opacity must be 3 values in (0, 0.95)")
    strategy = str(_require(meta, "strategy", json_path))
    backend = str(_require(meta, "inpaint_backend", json_path))
    scale_mode = str(_require(meta, "scale_mode", json_path))
    if strategy not in _STRATEGIES or backend not in _BACKENDS or scale_mode not in _SCALE_MODES:
        raise TemplateError(f"{json_path.name}: bad strategy/inpaint_backend/scale_mode")
    lama_fallback = str(meta.get("lama_fallback", "telea"))
    if lama_fallback not in _LAMA_FALLBACKS:
        raise TemplateError(f"{json_path.name}: bad lama_fallback {lama_fallback!r}")
    raw_instances = _require(meta, "instances", json_path)
    if not isinstance(raw_instances, list) or not raw_instances:
        raise TemplateError(f"{json_path.name}: instances must be a non-empty list")
    instances: list[Instance] = []
    for raw in raw_instances:
        anchor = str(raw["anchor"])
        if anchor not in _ANCHORS:
            raise TemplateError(f"{json_path.name}: bad anchor {anchor!r}")
        instances.append(Instance(anchor, float(raw["dx"]), float(raw["dy"])))  # type: ignore[arg-type]
    return WatermarkTemplate(
        id=tid,
        platform=str(_require(meta, "platform", json_path)),
        title=str(meta.get("title", tid)),
        coverage=coverage,
        opacity=(opacity[0], opacity[1], opacity[2]),
        strategy=strategy,  # type: ignore[arg-type]
        inpaint_backend=backend,  # type: ignore[arg-type]
        ref_width=int(_require(meta, "ref_width", json_path)),  # type: ignore[call-overload]
        scale_mode=scale_mode,  # type: ignore[arg-type]
        instances=tuple(instances),
        scale_search=tuple(float(v) for v in _require(meta, "scale_search", json_path)),  # type: ignore[attr-defined]
        pos_tol=float(_require(meta, "pos_tol", json_path)),  # type: ignore[arg-type]
        min_score=float(_require(meta, "min_score", json_path)),  # type: ignore[arg-type]
        lama_fallback=lama_fallback,  # type: ignore[arg-type]
    )


@lru_cache(maxsize=4)
def load_templates(directory: str | None = None) -> tuple[WatermarkTemplate, ...]:
    """All templates from a directory (default: bundled assets), sorted by id."""
    root = Path(directory) if directory else ASSETS_DIR
    paths = sorted(root.glob("*.json"))
    if not paths:
        raise TemplateError(f"no templates in {root}")
    return tuple(load_template(p) for p in paths)
```

- [ ] **Step 6: Флаги**

В `src/config.py` найти строку `    notify_no_match_enabled: bool = True` (класс `Settings`) и сразу после неё вставить:

```python

    # --- Watermark removal (docs/superpowers/plans/2026-10-14-watermark-remover.md) ---
    # /clean and incoming photos in RentFinderBot; served by the otz-bot.service daemon.
    watermark_bot_enabled: bool = False
    # Extra chat ids (besides TG_BOT_RECIPIENTS) allowed to use /clean, comma-separated.
    watermark_allowed_chat_ids: str = ""
    # Clean copies: <clean_dir>/<path relative to inbound_photos_dir>. Originals never change.
    # Must stay OUTSIDE inbound_photos_dir (the site backfill walks that whole tree).
    watermark_clean_dir: Path = PROJECT_ROOT / "data" / "clean"
    # LaMa neural inpainting for Halo Oglasi marks (needs torch from the `clip` extra + model).
    watermark_lama_enabled: bool = True
    watermark_lama_model_path: Path = PROJECT_ROOT / "data" / "models" / "big-lama.pt"
    # Deliver candidates to Darya already cleaned (off by default).
    watermark_clean_on_delivery: bool = False
    # The site serves clean copies instead of originals when they exist (off by default).
    watermark_site_use_clean: bool = False
    watermark_jpeg_quality: int = Field(default=95, ge=70, le=100)
```

(`Field`, `Path` и `PROJECT_ROOT` в `src/config.py` уже импортированы/определены — проверить `grep -nE "^from pydantic import|^PROJECT_ROOT|^from pathlib" src/config.py`.)

В конец `.env.example` добавить:

```bash
# === Watermark remover (docs/superpowers/plans/2026-10-14-watermark-remover.md) ===
# RentFinderBot /clean + incoming photos (served by otz-bot.service; off by default).
WATERMARK_BOT_ENABLED=false
# Extra chat ids besides TG_BOT_RECIPIENTS allowed to use /clean (comma-separated).
WATERMARK_ALLOWED_CHAT_IDS=
# Clean copies live here (mirror of data/inbound); must stay OUTSIDE data/inbound.
# WATERMARK_CLEAN_DIR=/opt/otz/data/clean
# LaMa inpainting for Halo Oglasi marks: needs torch (`clip` extra) + `otz wm-fetch-model`.
WATERMARK_LAMA_ENABLED=true
# WATERMARK_LAMA_MODEL_PATH=/opt/otz/data/models/big-lama.pt
# Send candidates to Darya already cleaned (stage 2 of the rollout).
WATERMARK_CLEAN_ON_DELIVERY=false
# Site serves clean copies (stage 3; then run `otz wm-clean-published`).
WATERMARK_SITE_USE_CLEAN=false
WATERMARK_JPEG_QUALITY=95
```

- [ ] **Step 7: Тесты проходят**

Run: `uv run pytest -q tests/watermark/test_templates.py`
Expected: `9 passed`.

- [ ] **Step 8: Lint-гейт**

Run: `uv run python scripts/check_changed_lint.py`
Expected: последняя строка `New lint diagnostics: 0`.

- [ ] **Step 9: Commit**

```bash
git add pyproject.toml uv.lock src/config.py .env.example src/watermark tests/watermark
git commit -m "feat(watermark): OpenCV dependency, flags and platform watermark templates"
```

---

### Task 2: Поиск и удаление знаков (pipeline)

**Files:**
- Create: `src/watermark/detect.py`, `src/watermark/inpaint.py`, `src/watermark/remove.py`, `src/watermark/pipeline.py`
- Test: `tests/watermark/test_pipeline.py`

**Interfaces:**
- Consumes: `src.watermark.templates` (Task 1), `tests.watermark.synth` (Task 1).
- Produces: `src.watermark.detect` — `WORK_MAX = 900`, `Detection(template_id, instance, x, y, scale, score, dx, dy)`, `to_gray(img_rgb)`, `gradient_field(gray)`, `scaled_coverage(tpl, scale)`, `match_instance(gray, tpl, placement, *, work_max=WORK_MAX) -> Detection | None`, `score_at(gray, tpl, det) -> float`.
- Produces: `src.watermark.inpaint` — `Inpainter` (Protocol: `name: str`, `inpaint(img_rgb, mask) -> np.ndarray`), `TeleaInpainter(radius=5)`, `get_inpainter(preferred: str) -> Inpainter` (LaMa из `src.watermark.lama.get_lama_inpainter`, если модуль есть и модель доступна, иначе Telea; Task 3 добавит модуль).
- Produces: `src.watermark.remove` — `alpha_map`, `reverse_blend(img, alpha, k)` (k — число или карта HxW), `refine_opacity(img, alpha) -> float`, `opacity_map(img, alpha) -> (kmap, k_center)`, `support_mask`, `edge_band`, `remove_reverse_blend(img, tpl, det) -> (img, k)`, `remove_inpaint(img, tpl, det, inpainter, *, dilate_px=3) -> img`.
- Produces: `src.watermark.pipeline` — `PLATFORM_TITLES = {"halooglasi": "Halo Oglasi", "4zida": "4zida", "cityexpert": "CityExpert"}`, `DEFAULT_JPEG_QUALITY = 95`, `NotAnImageError`, `MarkResult(template_id, platform, instance, score, x, y, scale, method, residual)` (`method`: `"reverse_blend"` | `"inpaint:lama"` | `"inpaint:telea"`), `CleanReport(width, height, marks=[], seconds=0.0)` со свойством `changed` и методами `platform_counts() -> Counter[str]`, `to_dict()`; функции `decode_image(data) -> np.ndarray`, `encode_jpeg(img, quality)`, `find_marks(img, templates=None) -> list[tuple[WatermarkTemplate, Detection]]`, `removal_for(tpl) -> tuple[str, Inpainter | None]`, `clean_array(img, templates=None) -> (np.ndarray, CleanReport)`, `clean_bytes(data, *, quality=95) -> (bytes, CleanReport)` (без знаков возвращает тот же объект bytes), `clean_file(src, dst, *, quality=95) -> CleanReport` (пишет dst атомарно и только если знаки найдены).

- [ ] **Step 1: Написать падающие тесты**

`tests/watermark/test_pipeline.py`:

```python
"""Detection and removal on synthetic data: every platform mark, negatives, bytes/files."""

from __future__ import annotations

import io
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from src.config import get_settings
from src.watermark.pipeline import (
    NotAnImageError,
    clean_array,
    clean_bytes,
    clean_file,
    decode_image,
    find_marks,
    removal_for,
)
from src.watermark.templates import load_templates
from tests.watermark.synth import apply_mark, jpeg_roundtrip, make_background, mark_mask

CASES = [
    ("cityexpert_center", (1920, 1080)),
    ("cityexpert_center", (900, 1600)),
    ("zida_logo", (1440, 1080)),
    ("zida_logo", (810, 1080)),
    ("halo_center", (1000, 750)),
    ("halo_center", (562, 750)),
    ("halo_corner", (1000, 750)),
    ("halo_corner", (750, 750)),
]


@pytest.fixture(autouse=True)
def _telea_only(monkeypatch):
    """CI has no torch: test the Telea path (LaMa has its own skippable tests)."""
    monkeypatch.setattr(get_settings(), "watermark_lama_enabled", False)


def _tpl(tid):
    return {t.id: t for t in load_templates()}[tid]


def _marked(tid, size, seed=3):
    bg = make_background(*size, seed=seed)
    return bg, jpeg_roundtrip(apply_mark(bg, _tpl(tid)))


@pytest.mark.parametrize(("tid", "size"), CASES)
def test_detects_exactly_the_applied_mark(tid, size):
    _bg, img = _marked(tid, size)
    found = find_marks(img)
    assert {(t.id, d.instance) for t, d in found} == {
        (tid, i) for i in range(len(_tpl(tid).instances))
    }
    for _t, det in found:
        assert abs(det.dx) <= 1.5 and abs(det.dy) <= 1.5


@pytest.mark.parametrize("seed", range(6))
@pytest.mark.parametrize("size", [(1920, 1080), (1000, 750), (810, 1080), (562, 750)])
def test_clean_background_has_no_marks(seed, size):
    img = jpeg_roundtrip(make_background(*size, seed=100 + seed))
    assert find_marks(img) == []


@pytest.mark.parametrize(("tid", "size"), CASES)
def test_cleaning_restores_background_under_the_mark(tid, size):
    bg, img = _marked(tid, size)
    out, report = clean_array(img)
    assert report.changed
    assert find_marks(out) == []
    mask = mark_mask(_tpl(tid), *size)
    # Error floor: even a perfect restoration carries the source JPEG noise.
    floor = np.abs(jpeg_roundtrip(bg).astype(np.float32) - bg)[mask].mean()
    before = np.abs(img.astype(np.float32) - bg)[mask].mean()
    after = np.abs(out.astype(np.float32) - bg)[mask].mean()
    if {m.method for m in report.marks} == {"reverse_blend"}:
        assert after <= floor + 2.0, (floor, before, after)
    else:  # inpainting cannot know the true pixels: require a clear improvement
        assert after <= min(before * 0.7, floor + 4.5), (floor, before, after)
    assert all(m.residual < _tpl(m.template_id).min_score for m in report.marks)


@pytest.mark.parametrize("seed", [11, 12])
def test_parts_with_weaker_opacity_are_restored_too(seed):
    """CityExpert draws its icon bars fainter than the letters; k is measured per part.

    With one k for the whole mark the bars come out ~3 levels too dark and the letters
    ~3-5 levels too bright (visible as faint rectangles/letters); per part both stay near 0.
    """
    tpl, size = _tpl("cityexpert_center"), (1920, 1080)
    bg = make_background(*size, seed=seed)
    place = tpl.predicted(*size)[0]
    bars_x = round(place.x + 560)  # the icon bars end at x~456 of the template
    faint = apply_mark(bg, tpl, opacity_scale=0.75)
    img = apply_mark(bg, tpl)
    img[:, :bars_x] = faint[:, :bars_x]
    out, report = clean_array(jpeg_roundtrip(img))
    assert [m.method for m in report.marks] == ["reverse_blend"]
    core = mark_mask(tpl, *size, dilate=0) & (apply_mark(np.zeros_like(bg), tpl).mean(axis=2) > 40)
    bias = out.astype(np.float32).mean(axis=2) - bg.astype(np.float32).mean(axis=2)
    bars, letters = core.copy(), core.copy()
    bars[:, bars_x:] = False
    letters[:, :bars_x] = False
    assert abs(float(bias[bars].mean())) < 1.5
    assert abs(float(bias[letters].mean())) < 1.5


def test_lama_fallback_when_lama_is_missing():
    assert removal_for(_tpl("zida_logo"))[0] == "reverse_blend"
    assert removal_for(_tpl("halo_center"))[0] == "inpaint:telea"
    assert removal_for(_tpl("halo_corner"))[0] == "inpaint:telea"
    assert removal_for(_tpl("cityexpert_center"))[0] == "reverse_blend"


@pytest.mark.parametrize(("tid", "size"), CASES)
def test_pixels_far_from_the_mark_are_untouched(tid, size):
    _bg, img = _marked(tid, size)
    out, _report = clean_array(img)
    far = ~mark_mask(_tpl(tid), *size, dilate=12)
    assert np.array_equal(out[far], img[far])


def test_clean_bytes_returns_same_bytes_when_nothing_found():
    buf = io.BytesIO()
    Image.fromarray(make_background(800, 600, seed=7)).save(buf, "JPEG", quality=90)
    data = buf.getvalue()
    out, report = clean_bytes(data)
    assert out is data and not report.changed


def test_clean_bytes_returns_jpeg_without_mark():
    _bg, img = _marked("zida_logo", (1440, 1080))
    buf = io.BytesIO()
    Image.fromarray(img).save(buf, "JPEG", quality=92)
    out, report = clean_bytes(buf.getvalue())
    assert report.changed and len(report.marks) == 2
    cleaned = decode_image(out)
    assert cleaned.shape == img.shape
    assert find_marks(cleaned) == []


def test_decode_rejects_garbage():
    with pytest.raises(NotAnImageError):
        decode_image(b"definitely not an image")


def test_decode_applies_exif_rotation():
    img = Image.fromarray(make_background(400, 200, seed=1))
    exif = img.getexif()
    exif[0x0112] = 6  # rotate by 90 degrees
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif.tobytes())
    assert decode_image(buf.getvalue()).shape[:2] == (400, 200)


def test_clean_file_writes_only_when_marks_found(tmp_path: Path):
    plain = tmp_path / "plain.jpg"
    Image.fromarray(make_background(800, 600, seed=9)).save(plain, "JPEG", quality=90)
    marked = tmp_path / "marked.jpg"
    Image.fromarray(_marked("halo_corner", (1000, 750))[1]).save(marked, "JPEG", quality=92)
    original_bytes = marked.read_bytes()
    assert not clean_file(plain, tmp_path / "plain.clean.jpg").changed
    assert not (tmp_path / "plain.clean.jpg").exists()
    assert clean_file(marked, tmp_path / "out" / "marked.clean.jpg").changed
    assert (tmp_path / "out" / "marked.clean.jpg").is_file()
    assert marked.read_bytes() == original_bytes
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `uv run pytest -q tests/watermark/test_pipeline.py`
Expected: ошибка сбора `ModuleNotFoundError: No module named 'src.watermark.pipeline'`.

- [ ] **Step 3: Поиск знака**

`src/watermark/detect.py`:

```python
"""Find a mark instance near its predicted place: NCC of gradient fields + refinement.

Marks are white and semi-transparent, so their edges are the most reliable signal. The image
and template gradient fields (gx, gy) are compared with normalized correlation
(TM_CCORR_NORMED over two channels). The search runs only inside a +/- pos_tol*W window around
the prediction and over a few scales: platform geometry is strict (checked on ~1300 photos).

Two stages: coarse on a downscaled copy (long side <= WORK_MAX), then refinement at full
resolution in a +/- (2/f + 2) px window with 0.5 % scale steps and a sub-pixel parabola fit.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import cv2
import numpy as np

from src.watermark.templates import Placement, WatermarkTemplate

WORK_MAX = 900
_REFINE_STEPS = (-0.01, -0.005, 0.0, 0.005, 0.01)


@dataclass(frozen=True)
class Detection:
    template_id: str
    instance: int
    x: float  # template top-left corner in image pixels (sub-pixel)
    y: float
    scale: float
    score: float
    dx: float  # offset from the prediction, px
    dy: float


def to_gray(img_rgb: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(img_rgb, cv2.COLOR_RGB2GRAY).astype(np.float32)


def gradient_field(gray: np.ndarray) -> np.ndarray:
    g = cv2.GaussianBlur(gray, (0, 0), 1.0)
    gx = cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)
    return cv2.merge([gx, gy])


def scaled_coverage(tpl: WatermarkTemplate, scale: float) -> np.ndarray:
    w = max(2, round(tpl.width * scale))
    h = max(2, round(tpl.height * scale))
    interp = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
    return cv2.resize(tpl.coverage, (w, h), interpolation=interp)


def _best_in_window(
    gray: np.ndarray, cov: np.ndarray, cx: float, cy: float, radius: int
) -> tuple[float, float, float] | None:
    """Best top-left position in a window around (cx, cy): (score, x, y) or None."""
    th, tw = cov.shape
    H, W = gray.shape
    x0, y0 = max(0, math.floor(cx) - radius), max(0, math.floor(cy) - radius)
    x1, y1 = min(W - tw, math.floor(cx) + radius), min(H - th, math.floor(cy) + radius)
    if x1 < x0 or y1 < y0:
        return None
    sub = gray[y0 : y1 + th, x0 : x1 + tw]
    res = cv2.matchTemplate(gradient_field(sub), gradient_field(cov * 255.0), cv2.TM_CCORR_NORMED)
    _, score, _, (px, py) = cv2.minMaxLoc(res)
    fx, fy = 0.0, 0.0
    if 0 < px < res.shape[1] - 1:
        left, mid, right = res[py, px - 1], res[py, px], res[py, px + 1]
        den = left - 2.0 * mid + right
        fx = float(0.5 * (left - right) / den) if abs(den) > 1e-9 else 0.0
    if 0 < py < res.shape[0] - 1:
        up, mid, down = res[py - 1, px], res[py, px], res[py + 1, px]
        den = up - 2.0 * mid + down
        fy = float(0.5 * (up - down) / den) if abs(den) > 1e-9 else 0.0
    fx, fy = max(-0.5, min(0.5, fx)), max(-0.5, min(0.5, fy))
    return float(score), x0 + px + fx, y0 + py + fy


def match_instance(
    gray: np.ndarray, tpl: WatermarkTemplate, placement: Placement, *, work_max: int = WORK_MAX
) -> Detection | None:
    """Best match of one instance near its prediction (score may be below the threshold)."""
    H, W = gray.shape
    f = min(1.0, work_max / float(max(H, W)))
    small = gray if f >= 1.0 else cv2.resize(
        gray, (round(W * f), round(H * f)), interpolation=cv2.INTER_AREA
    )
    radius_full = int(tpl.pos_tol * W) + 4
    radius_small = max(3, int(radius_full * f) + 2)
    coarse: tuple[float, float, float, float] | None = None  # score, scale, x, y (full res)
    for mult in tpl.scale_search:
        scale = placement.scale * mult
        cov = scaled_coverage(tpl, scale * f)
        hit = _best_in_window(small, cov, placement.x * f, placement.y * f, radius_small)
        if hit is None:
            continue
        score, x, y = hit
        if coarse is None or score > coarse[0]:
            coarse = (score, scale, x / f, y / f)
    if coarse is None:
        return None
    _, scale0, x0, y0 = coarse
    radius_refine = math.ceil(2.0 / f) + 2
    best: tuple[float, float, float, float] | None = None
    for step in _REFINE_STEPS:
        scale = scale0 * (1.0 + step)
        hit = _best_in_window(gray, scaled_coverage(tpl, scale), x0, y0, radius_refine)
        if hit is None:
            continue
        score, x, y = hit
        if best is None or score > best[0]:
            best = (score, scale, x, y)
    if best is None:
        return None
    score, scale, x, y = best
    return Detection(
        template_id=tpl.id,
        instance=placement.instance,
        x=x,
        y=y,
        scale=scale,
        score=score,
        dx=x - placement.x,
        dy=y - placement.y,
    )


def score_at(gray: np.ndarray, tpl: WatermarkTemplate, det: Detection) -> float:
    """Correlation exactly at the detected place (residual check after removal)."""
    cov = scaled_coverage(tpl, det.scale)
    hit = _best_in_window(gray, cov, det.x, det.y, 1)
    return 0.0 if hit is None else hit[0]
```

- [ ] **Step 4: Дорисовка (Telea; LaMa подключится в Task 3)**

`src/watermark/inpaint.py`:

```python
"""Inpainters fill the masked pixels with plausible content.

TeleaInpainter uses OpenCV (cv2.INPAINT_TELEA): fast, no extra dependencies, good for small
text and thin bands. LaMa (neural network, optional, needs torch) lives in
`src/watermark/lama.py`; `get_inpainter("lama")` falls back to Telea when LaMa is unavailable.
"""

from __future__ import annotations

import logging
from typing import Protocol

import cv2
import numpy as np

logger = logging.getLogger(__name__)


class Inpainter(Protocol):
    name: str

    def inpaint(self, img_rgb: np.ndarray, mask: np.ndarray) -> np.ndarray:
        """img_rgb: uint8 HxWx3, mask: uint8 HxW (non-zero = fill). Returns uint8 HxWx3."""
        ...


class TeleaInpainter:
    name = "telea"

    def __init__(self, radius: int = 5) -> None:
        self.radius = radius

    def inpaint(self, img_rgb: np.ndarray, mask: np.ndarray) -> np.ndarray:
        if not mask.any():
            return img_rgb.copy()
        bgr = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2BGR)
        out = cv2.inpaint(bgr, (mask > 0).astype(np.uint8), self.radius, cv2.INPAINT_TELEA)
        return cv2.cvtColor(out, cv2.COLOR_BGR2RGB)


_TELEA = TeleaInpainter(radius=5)
_lama_warned = False


def get_inpainter(preferred: str) -> Inpainter:
    """LaMa when enabled and available, otherwise Telea (logs one warning)."""
    global _lama_warned
    if preferred == "lama":
        try:
            from src.watermark.lama import get_lama_inpainter
        except ImportError:
            get_lama_inpainter = None  # type: ignore[assignment]
        lama = get_lama_inpainter() if get_lama_inpainter is not None else None
        if lama is not None:
            return lama
        if not _lama_warned:
            logger.warning("[watermark] LaMa unavailable — falling back to Telea inpainting")
            _lama_warned = True
    return _TELEA
```

- [ ] **Step 5: Удаление знака**

`src/watermark/remove.py`:

```python
"""Remove a detected mark.

reverse_blend: exact "subtraction" of a semi-transparent white mark,
O = (J - k*a*255) / (1 - k*a). Keeps the real pixels under the mark (best for the big
CityExpert logo). The multiplier k is chosen per connected part of the mark (each letter, each
icon bar) so the restored pixels match the brightness of nearby unmarked pixels: platforms draw
parts of their logo with slightly different opacity. A thin band along the mark contour is then
filled with Telea.

inpaint: the mark mask (dilated by a few pixels) is filled by an inpainter (LaMa or Telea).
Used for thin or uneven marks (Halo Oglasi, 4zida) where subtraction leaves outlines.
"""

from __future__ import annotations

import cv2
import numpy as np

from src.watermark.detect import Detection, scaled_coverage
from src.watermark.inpaint import Inpainter, TeleaInpainter
from src.watermark.templates import WatermarkTemplate

WHITE = 255.0
K_GRID = tuple(round(0.60 + 0.02 * i, 2) for i in range(41))  # 0.60 .. 1.40
K_PART_LIMIT = 0.25  # a part's k stays within the central k +/- this
BRIGHT_BACKGROUND = 235.0  # on near-white backgrounds the brightness criterion is unreliable
MIN_VALID_PIXELS = 30
PLATEAU = 0.85  # compare only part pixels with alpha >= 85 % of the part's peak
OUTSIDE_MARGIN = 2  # px between the template edge and pixels used as background
BACKGROUND_SIGMA = 4.0  # px, Gaussian radius of the local background estimate
BAND_INPAINTER = TeleaInpainter(radius=3)


def alpha_map(tpl: WatermarkTemplate, det: Detection, shape_hw: tuple[int, int]) -> np.ndarray:
    """Mark alpha for the whole image: HxWx3 float32 (per channel R, G, B)."""
    H, W = shape_hw
    cov = scaled_coverage(tpl, det.scale)
    m = np.float32([[1, 0, det.x], [0, 1, det.y]])
    placed = cv2.warpAffine(
        cov, m, (W, H), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0
    )
    opacity = np.asarray(tpl.opacity, dtype=np.float32)
    return placed[..., None] * opacity[None, None, :]


def reverse_blend(img_rgb: np.ndarray, alpha: np.ndarray, k: float | np.ndarray) -> np.ndarray:
    """k: a scalar or an HxW per-pixel map."""
    k_arr = np.asarray(k, dtype=np.float32)
    if k_arr.ndim == 2:
        k_arr = k_arr[..., None]
    a = np.clip(alpha * k_arr, 0.0, 0.95)
    return ((img_rgb.astype(np.float32) - a * WHITE) / (1.0 - a)).astype(np.float32)


def _window(lum: np.ndarray, x: int, y: int, w: int, h: int) -> tuple[int, int, int, int]:
    """Box (x, y, w, h) grown by 12 px and clipped: (y0, y1, x0, x1)."""
    return (max(0, y - 12), min(lum.shape[0], y + h + 12),
            max(0, x - 12), min(lum.shape[1], x + w + 12))


def _best_k(
    img_rgb: np.ndarray,
    alpha: np.ndarray,
    lum: np.ndarray,
    window: tuple[int, int, int, int],
    part: np.ndarray,
) -> tuple[float, int] | None:
    """(k, weight) for one part: the K_GRID value whose median (restored part pixel - nearby
    unmarked background) is closest to 0; weight = number of pixels it was measured on.

    Only the part's plateau (alpha >= PLATEAU of its peak) is compared, and the background
    starts OUTSIDE_MARGIN px beyond the template: real marks have softer edges than the
    template, and measuring on those edges biases k (bars of the CityExpert icon came out as
    visible rectangles). `part` is a boolean mask inside `window` (y0, y1, x0, x1). None when
    too few plateau pixels lie near unmarked ones or the background is near-white (there the
    mark is barely visible and the criterion is noise).
    """
    y0, y1, x0, x1 = window
    local = lum[y0:y1, x0:x1]
    size = 2 * OUTSIDE_MARGIN + 1
    support = cv2.dilate((local >= 0.005).astype(np.uint8), np.ones((size, size), np.uint8))
    outside = (support == 0).astype(np.float32)
    den = cv2.GaussianBlur(outside, (0, 0), BACKGROUND_SIGMA)
    peak = float(local[part].max()) if part.any() else 0.0
    valid = part & (local >= PLATEAU * peak) & (den > 0.03)
    count = int(valid.sum())
    if count < MIN_VALID_PIXELS:
        return None
    sub = img_rgb[y0:y1, x0:x1]
    gray = sub.astype(np.float32).mean(axis=2)
    background = cv2.GaussianBlur(gray * outside, (0, 0), BACKGROUND_SIGMA) / np.maximum(den, 1e-6)
    if float(np.median(background[valid])) > BRIGHT_BACKGROUND:
        return None
    al = alpha[y0:y1, x0:x1]
    best_k, best_err = 1.0, float("inf")
    for k in K_GRID:
        r = reverse_blend(sub, al, k).mean(axis=2).astype(np.float32)
        bg = cv2.GaussianBlur(r * outside, (0, 0), BACKGROUND_SIGMA) / np.maximum(den, 1e-6)
        err = abs(float(np.median((r - bg)[valid])))
        if err < best_err:
            best_k, best_err = k, err
    return best_k, count


def refine_opacity(img_rgb: np.ndarray, alpha: np.ndarray) -> float:
    """One k for the whole mark (1.0 when it cannot be measured)."""
    lum = alpha.mean(axis=2)
    peak = float(lum.max())
    if peak <= 0.0:
        return 1.0
    core = lum > 0.5 * peak
    ys, xs = np.nonzero(core)
    x, y = int(xs.min()), int(ys.min())
    window = _window(lum, x, y, int(xs.max()) - x + 1, int(ys.max()) - y + 1)
    y0, y1, x0, x1 = window
    found = _best_k(img_rgb, alpha, lum, window, core[y0:y1, x0:x1])
    return 1.0 if found is None else found[0]


def _weighted_median(values: list[tuple[float, int]]) -> float:
    ordered = sorted(values)
    half = sum(w for _k, w in ordered) / 2.0
    acc = 0
    for k, w in ordered:
        acc += w
        if acc >= half:
            return k
    return ordered[-1][0]


def opacity_map(img_rgb: np.ndarray, alpha: np.ndarray) -> tuple[np.ndarray, float]:
    """Per-pixel k (HxW float32) from per-part estimates, plus the central k.

    Parts = connected components of the mark core (letters, icon bars). The central k is the
    pixel-weighted median of the measured parts (a whole-mark measurement when no part can be
    measured); each part's k stays within central +/- K_PART_LIMIT. Pixels between parts get a
    smooth blend of nearby parts' k, pixels far from any part get the central k.
    """
    lum = alpha.mean(axis=2)
    peak = float(lum.max())
    if peak <= 0.0:
        return np.ones(lum.shape, np.float32), 1.0
    core = (lum > 0.5 * peak).astype(np.uint8)
    n, labels, stats, _centroids = cv2.connectedComponentsWithStats(core, connectivity=8)
    measured: dict[int, tuple[float, int]] = {}
    for i in range(1, n):
        x, y, w, h = (int(v) for v in stats[i, :4])
        window = _window(lum, x, y, w, h)
        y0, y1, x0, x1 = window
        found = _best_k(img_rgb, alpha, lum, window, labels[y0:y1, x0:x1] == i)
        if found is not None:
            measured[i] = found
    center = (
        _weighted_median(list(measured.values())) if measured else refine_opacity(img_rgb, alpha)
    )
    values = np.full(n, center, np.float32)
    for i, (k, _weight) in measured.items():
        values[i] = min(max(k, center - K_PART_LIMIT), center + K_PART_LIMIT)
    known = (labels > 0).astype(np.float32)
    num = cv2.GaussianBlur(values[labels] * known, (0, 0), 3.0)
    den = cv2.GaussianBlur(known, (0, 0), 3.0)
    kmap = np.full(lum.shape, center, np.float32)
    near = den > 1e-3
    kmap[near] = num[near] / den[near]
    return kmap, center


def support_mask(alpha: np.ndarray, *, threshold: float = 0.02, dilate_px: int = 0) -> np.ndarray:
    mask = (alpha.mean(axis=2) > threshold).astype(np.uint8)
    if dilate_px > 0:
        size = 2 * dilate_px + 1
        mask = cv2.dilate(mask, np.ones((size, size), np.uint8))
    return mask


def edge_band(alpha: np.ndarray, *, band_px: int = 2) -> np.ndarray:
    support = support_mask(alpha, threshold=0.01)
    band = cv2.morphologyEx(support, cv2.MORPH_GRADIENT, np.ones((3, 3), np.uint8))
    if band_px > 0:
        size = 2 * band_px + 1
        band = cv2.dilate(band, np.ones((size, size), np.uint8))
    return band


def remove_reverse_blend(
    img_rgb: np.ndarray, tpl: WatermarkTemplate, det: Detection
) -> tuple[np.ndarray, float]:
    """Returns (cleaned image, central k)."""
    alpha = alpha_map(tpl, det, img_rgb.shape[:2])
    kmap, k_center = opacity_map(img_rgb, alpha)
    restored = np.clip(reverse_blend(img_rgb, alpha, kmap), 0, 255).astype(np.uint8)
    return BAND_INPAINTER.inpaint(restored, edge_band(alpha)), k_center


def remove_inpaint(
    img_rgb: np.ndarray,
    tpl: WatermarkTemplate,
    det: Detection,
    inpainter: Inpainter,
    *,
    dilate_px: int = 3,
) -> np.ndarray:
    alpha = alpha_map(tpl, det, img_rgb.shape[:2])
    grow = max(1, round(dilate_px * max(det.scale, 0.5)))
    mask = support_mask(alpha, threshold=0.02, dilate_px=grow)
    return inpainter.inpaint(img_rgb, mask)
```

- [ ] **Step 6: Публичный API**

`src/watermark/pipeline.py`:

```python
"""Public API: find listing-platform marks on a photo and remove them.

Steps: decode (with EXIF rotation) -> for every template instance find the best match near its
predicted place -> keep matches that pass the template threshold -> remove each mark with its
template strategy -> measure the residual. The original is never modified: functions return a
new image or write a new file.
"""

from __future__ import annotations

import io
import os
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError

from src.watermark.detect import Detection, match_instance, score_at, to_gray
from src.watermark.inpaint import Inpainter, get_inpainter
from src.watermark.remove import remove_inpaint, remove_reverse_blend
from src.watermark.templates import WatermarkTemplate, load_templates

PLATFORM_TITLES = {"halooglasi": "Halo Oglasi", "4zida": "4zida", "cityexpert": "CityExpert"}
DEFAULT_JPEG_QUALITY = 95
MAX_PIXELS = 40_000_000  # decompression-bomb guard: Telegram <= 2560 px, platforms <= 1920 px


class NotAnImageError(ValueError):
    """Bytes do not decode as an image, or the image is unreasonably large."""


@dataclass(frozen=True)
class MarkResult:
    template_id: str
    platform: str
    instance: int
    score: float
    x: float
    y: float
    scale: float
    method: str  # "reverse_blend" | "inpaint:lama" | "inpaint:telea"
    residual: float  # correlation at the same place after removal


@dataclass
class CleanReport:
    width: int
    height: int
    marks: list[MarkResult] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def changed(self) -> bool:
        return bool(self.marks)

    def platform_counts(self) -> Counter[str]:
        return Counter(PLATFORM_TITLES.get(m.platform, m.platform) for m in self.marks)

    def to_dict(self) -> dict[str, object]:
        return {
            "width": self.width,
            "height": self.height,
            "seconds": round(self.seconds, 3),
            "marks": [m.__dict__ for m in self.marks],
        }


def decode_image(data: bytes) -> np.ndarray:
    """RGB uint8 with EXIF orientation applied."""
    try:
        with Image.open(io.BytesIO(data)) as im:
            if im.width * im.height > MAX_PIXELS:
                raise NotAnImageError(f"image too large: {im.width}x{im.height}")
            rgb = ImageOps.exif_transpose(im).convert("RGB")
            return np.asarray(rgb, dtype=np.uint8).copy()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as error:
        raise NotAnImageError(str(error)) from error


def encode_jpeg(img_rgb: np.ndarray, quality: int = DEFAULT_JPEG_QUALITY) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(img_rgb).save(buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def max_offset_px(image_width: int) -> float:
    """Allowed distance from the prediction (real marks are within 1.5 px)."""
    return 2.0 + 0.004 * image_width


def is_confident(tpl: WatermarkTemplate, det: Detection, image_width: int) -> bool:
    limit = max_offset_px(image_width)
    return det.score >= tpl.min_score and abs(det.dx) <= limit and abs(det.dy) <= limit


def find_marks(
    img_rgb: np.ndarray, templates: tuple[WatermarkTemplate, ...] | None = None
) -> list[tuple[WatermarkTemplate, Detection]]:
    gray = to_gray(img_rgb)
    h, w = gray.shape
    found: list[tuple[WatermarkTemplate, Detection]] = []
    for tpl in templates if templates is not None else load_templates():
        for placement in tpl.predicted(w, h):
            det = match_instance(gray, tpl, placement)
            if det is not None and is_confident(tpl, det, w):
                found.append((tpl, det))
    return found


def removal_for(tpl: WatermarkTemplate) -> tuple[str, Inpainter | None]:
    """(method name, inpainter) for a template; honours lama_fallback when LaMa is missing."""
    if tpl.strategy == "reverse_blend":
        return "reverse_blend", None
    inpainter = get_inpainter(tpl.inpaint_backend)
    if inpainter.name != tpl.inpaint_backend and tpl.lama_fallback == "reverse_blend":
        return "reverse_blend", None
    return f"inpaint:{inpainter.name}", inpainter


def clean_array(
    img_rgb: np.ndarray, templates: tuple[WatermarkTemplate, ...] | None = None
) -> tuple[np.ndarray, CleanReport]:
    started = time.monotonic()
    h, w = img_rgb.shape[:2]
    report = CleanReport(width=w, height=h)
    out = img_rgb
    for tpl, det in find_marks(img_rgb, templates):
        method, inpainter = removal_for(tpl)
        if inpainter is None:
            out, _k = remove_reverse_blend(out, tpl, det)
        else:
            out = remove_inpaint(out, tpl, det, inpainter)
        residual = score_at(to_gray(out), tpl, det)
        report.marks.append(
            MarkResult(
                template_id=tpl.id,
                platform=tpl.platform,
                instance=det.instance,
                score=round(det.score, 4),
                x=round(det.x, 2),
                y=round(det.y, 2),
                scale=round(det.scale, 5),
                method=method,
                residual=round(residual, 4),
            )
        )
    report.seconds = time.monotonic() - started
    return (out if report.changed else img_rgb.copy()), report


def clean_bytes(data: bytes, *, quality: int = DEFAULT_JPEG_QUALITY) -> tuple[bytes, CleanReport]:
    """Photo bytes -> (JPEG without marks, report); unmarked input is returned unchanged."""
    img = decode_image(data)
    out, report = clean_array(img)
    return (encode_jpeg(out, quality) if report.changed else data), report


def clean_file(src: Path, dst: Path, *, quality: int = DEFAULT_JPEG_QUALITY) -> CleanReport:
    """Write dst (JPEG) atomically, only when marks were found; src is never touched."""
    img = decode_image(src.read_bytes())
    out, report = clean_array(img)
    if report.changed:
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(f".{dst.name}.{os.getpid()}.tmp")
        tmp.write_bytes(encode_jpeg(out, quality))
        os.replace(tmp, dst)
    return report
```

- [ ] **Step 7: Тесты проходят**

Run: `uv run pytest -q tests/watermark`
Expected: `65 passed` (около минуты).

- [ ] **Step 8: Lint-гейт и commit**

```bash
uv run python scripts/check_changed_lint.py
git add src/watermark tests/watermark
git commit -m "feat(watermark): detect platform marks and remove them"
```

Expected (lint): `New lint diagnostics: 0`.

---

### Task 3: LaMa — нейросетевая дорисовка

**Files:**
- Create: `src/watermark/lama.py`
- Test: `tests/watermark/test_lama.py`

**Interfaces:**
- Consumes: `get_settings().watermark_lama_enabled`, `.watermark_lama_model_path` (Task 1); `get_inpainter` (Task 2) импортирует `get_lama_inpainter` отсюда.
- Produces: `src.watermark.lama` — `MODEL_URL`, `MODEL_SHA256`, `MODEL_BYTES`, `LamaInpainter(model_path, *, margin=96, max_side=1024)` (`name = "lama"`, `inpaint(img_rgb, mask)` меняет только пиксели под маской), `torch_available() -> bool`, `get_lama_inpainter() -> LamaInpainter | None`, `sha256_of(path) -> str`, `download_model(dest, *, url=MODEL_URL, sha256=MODEL_SHA256) -> Path`.

- [ ] **Step 1: Написать тесты**

`tests/watermark/test_lama.py`:

```python
"""LaMa: without torch/model it falls back to Telea; with the model only the mask changes.

Model tests are skipped in CI (no torch there). Locally: `uv sync --extra clip`,
`uv run otz wm-fetch-model`, then `uv run pytest tests/watermark/test_lama.py`.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pytest

from src.config import get_settings
from src.watermark.inpaint import get_inpainter
from src.watermark.pipeline import clean_array
from src.watermark.templates import load_templates
from tests.watermark.synth import apply_mark, jpeg_roundtrip, make_background

MODEL = Path(get_settings().watermark_lama_model_path)
HAS_LAMA = importlib.util.find_spec("torch") is not None and MODEL.is_file()


def test_pad_to_multiple_of_8():
    from src.watermark.lama import _pad_to_multiple

    assert _pad_to_multiple(np.zeros((13, 17, 3), np.uint8)).shape == (16, 24, 3)
    assert _pad_to_multiple(np.zeros((16, 8), np.uint8)).shape == (16, 8)


def test_download_model_keeps_only_a_verified_file(tmp_path):
    import hashlib

    import httpx
    import respx

    from src.watermark.lama import download_model

    url = "https://models.example.invalid/big-lama.pt"
    good = hashlib.sha256(b"model-bytes").hexdigest()
    with respx.mock:
        respx.get(url).mock(return_value=httpx.Response(200, content=b"model-bytes"))
        dest = download_model(tmp_path / "models" / "big-lama.pt", url=url, sha256=good)
        assert dest.read_bytes() == b"model-bytes"
        with pytest.raises(ValueError):
            download_model(tmp_path / "bad.pt", url=url, sha256="0" * 64)
    assert not (tmp_path / "bad.pt").exists()
    assert not list(tmp_path.glob(".bad.pt.*"))  # the partial download is removed


def test_falls_back_to_telea_without_model(monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "watermark_lama_enabled", True)
    monkeypatch.setattr(get_settings(), "watermark_lama_model_path", tmp_path / "missing.pt")
    assert get_inpainter("lama").name == "telea"


def test_disabled_flag_means_telea(monkeypatch):
    monkeypatch.setattr(get_settings(), "watermark_lama_enabled", False)
    assert get_inpainter("lama").name == "telea"


@pytest.mark.skipif(not HAS_LAMA, reason="torch or the LaMa model file is unavailable")
def test_lama_changes_only_masked_pixels():
    from src.watermark.lama import LamaInpainter

    img = make_background(640, 480, seed=4)
    mask = np.zeros((480, 640), np.uint8)
    mask[200:240, 220:420] = 1
    out = LamaInpainter(MODEL).inpaint(img, mask)
    assert out.shape == img.shape
    assert np.array_equal(out[mask == 0], img[mask == 0])
    assert not np.array_equal(out[mask == 1], img[mask == 1])


@pytest.mark.skipif(not HAS_LAMA, reason="torch or the LaMa model file is unavailable")
def test_pipeline_uses_lama_for_halo_center(monkeypatch):
    monkeypatch.setattr(get_settings(), "watermark_lama_enabled", True)
    tpl = {t.id: t for t in load_templates()}["halo_center"]
    img = jpeg_roundtrip(apply_mark(make_background(1000, 750, seed=2), tpl))
    _out, report = clean_array(img)
    assert [m.method for m in report.marks] == ["inpaint:lama"]


@pytest.mark.skipif(not HAS_LAMA, reason="torch or the LaMa model file is unavailable")
def test_pipeline_uses_lama_for_4zida(monkeypatch):
    monkeypatch.setattr(get_settings(), "watermark_lama_enabled", True)
    tpl = {t.id: t for t in load_templates()}["zida_logo"]
    img = jpeg_roundtrip(apply_mark(make_background(1440, 1080, seed=6), tpl))
    _out, report = clean_array(img)
    assert [m.method for m in report.marks] == ["inpaint:lama", "inpaint:lama"]
```

- [ ] **Step 2: Убедиться, что новые тесты падают**

Run: `uv run pytest -q tests/watermark/test_lama.py`
Expected: `2 failed, 2 passed, 3 skipped` (`test_pad_to_multiple_of_8` и `test_download_model_keeps_only_a_verified_file` — `ModuleNotFoundError: No module named 'src.watermark.lama'`).

- [ ] **Step 3: Реализация**

`src/watermark/lama.py`:

```python
"""LaMa: neural inpainting (big-lama, Apache-2.0) for marks that cannot be "subtracted".

Optional: needs torch (already on the server via the `clip` extra) and the model file
(`otz wm-fetch-model` downloads it and checks sha256). Without torch/model, or with
WATERMARK_LAMA_ENABLED=0, `get_lama_inpainter()` returns None and the pipeline uses Telea.

Processing: crop a rectangle around the mask with `margin` pixels, downscale to `max_side`
if needed, pad to a multiple of 8, run the model and paste back ONLY the masked pixels;
everything else stays byte-for-byte identical to the input.
"""

from __future__ import annotations

import hashlib
import importlib.util
import logging
import os
import threading
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from src.config import get_settings

logger = logging.getLogger(__name__)

MODEL_URL = "https://github.com/Sanster/models/releases/download/add_big_lama/big-lama.pt"
MODEL_SHA256 = "344c77bbcb158f17dd143070d1e789f38a66c04202311ae3a258ef66667a9ea9"
MODEL_BYTES = 205_669_692


def _pad_to_multiple(arr: np.ndarray, mod: int = 8) -> np.ndarray:
    h, w = arr.shape[:2]
    ph, pw = (mod - h % mod) % mod, (mod - w % mod) % mod
    pads: tuple[tuple[int, int], ...] = ((0, ph), (0, pw)) + (((0, 0),) if arr.ndim == 3 else ())
    return np.pad(arr, pads, mode="symmetric")


class LamaInpainter:
    name = "lama"

    def __init__(self, model_path: Path, *, margin: int = 96, max_side: int = 1024) -> None:
        self.model_path = model_path
        self.margin = margin
        self.max_side = max_side
        self._model: Any = None
        self._lock = threading.Lock()

    def _load(self) -> Any:
        with self._lock:
            if self._model is None:
                import torch

                torch.set_num_threads(max(1, min(4, os.cpu_count() or 1)))
                model = torch.jit.load(str(self.model_path), map_location="cpu")
                model.eval()
                self._model = model
                logger.info("[watermark] LaMa model loaded from %s", self.model_path)
            return self._model

    def inpaint(self, img_rgb: np.ndarray, mask: np.ndarray) -> np.ndarray:
        import torch

        sel = mask > 0
        if not sel.any():
            return img_rgb.copy()
        ys, xs = np.nonzero(sel)
        H, W = sel.shape
        y0, y1 = max(0, int(ys.min()) - self.margin), min(H, int(ys.max()) + self.margin + 1)
        x0, x1 = max(0, int(xs.min()) - self.margin), min(W, int(xs.max()) + self.margin + 1)
        crop = img_rgb[y0:y1, x0:x1]
        crop_mask = sel[y0:y1, x0:x1].astype(np.uint8)
        ch, cw = crop.shape[:2]
        factor = min(1.0, self.max_side / float(max(ch, cw)))
        if factor < 1.0:
            size = (round(cw * factor), round(ch * factor))
            work = cv2.resize(crop, size, interpolation=cv2.INTER_AREA)
            work_mask = cv2.resize(crop_mask, size, interpolation=cv2.INTER_NEAREST)
        else:
            work, work_mask = crop, crop_mask
        hh, ww = work.shape[:2]
        image = torch.from_numpy(_pad_to_multiple(work)).permute(2, 0, 1).unsqueeze(0).float()
        image = image / 255.0
        mask_t = torch.from_numpy((_pad_to_multiple(work_mask) > 0).astype(np.float32))
        mask_t = mask_t.unsqueeze(0).unsqueeze(0)
        model = self._load()
        with self._lock, torch.inference_mode():
            result = model(image, mask_t)
        res = (result[0].permute(1, 2, 0).numpy() * 255.0).clip(0, 255).astype(np.uint8)[:hh, :ww]
        if factor < 1.0:
            res = cv2.resize(res, (cw, ch), interpolation=cv2.INTER_CUBIC)
        out = img_rgb.copy()
        region = out[y0:y1, x0:x1]
        keep = crop_mask > 0
        region[keep] = res[keep]
        return out


_instance: LamaInpainter | None = None
_instance_lock = threading.Lock()


def torch_available() -> bool:
    return importlib.util.find_spec("torch") is not None


def get_lama_inpainter() -> LamaInpainter | None:
    """LaMa singleton, or None (disabled by flag / no torch / no model file)."""
    global _instance
    settings = get_settings()
    if not getattr(settings, "watermark_lama_enabled", True):
        return None
    model_path = Path(getattr(settings, "watermark_lama_model_path", ""))
    if not model_path.is_file() or not torch_available():
        return None
    with _instance_lock:
        if _instance is None or _instance.model_path != model_path:
            _instance = LamaInpainter(model_path)
        return _instance


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_model(dest: Path, *, url: str = MODEL_URL, sha256: str = MODEL_SHA256) -> Path:
    """Download the model to a temp file, verify sha256, then move it into place."""
    import httpx

    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and sha256_of(dest) == sha256:
        return dest
    tmp = dest.with_name(f".{dest.name}.{os.getpid()}.part")
    with httpx.stream("GET", url, follow_redirects=True, timeout=httpx.Timeout(60.0)) as resp:
        resp.raise_for_status()
        with tmp.open("wb") as fh:
            for chunk in resp.iter_bytes(1 << 20):
                fh.write(chunk)
    actual = sha256_of(tmp)
    if actual != sha256:
        tmp.unlink(missing_ok=True)
        raise ValueError(f"LaMa model checksum mismatch: {actual} != {sha256}")
    os.replace(tmp, dest)
    return dest
```

- [ ] **Step 4: Тесты проходят**

Run: `uv run pytest -q tests/watermark/test_lama.py`
Expected: `4 passed, 3 skipped` (модель ещё не скачана; в Task 11 после `otz wm-fetch-model` — `7 passed`).

- [ ] **Step 5: Lint-гейт и commit**

```bash
uv run python scripts/check_changed_lint.py
git add src/watermark/lama.py tests/watermark/test_lama.py
git commit -m "feat(watermark): optional LaMa inpainting with verified model download"
```

---

### Task 4: Чистые копии в `data/clean`

**Files:**
- Create: `src/watermark/store.py`
- Test: `tests/watermark/test_store.py`

**Interfaces:**
- Consumes: `clean_file` (Task 2); настройки `inbound_photos_dir`, `watermark_clean_dir`, `watermark_jpeg_quality`, `watermark_site_use_clean`.
- Produces: `src.watermark.store` — `STORE_VERSION = 1`, `SIDECAR_SUFFIX = ".wm.json"`, `StoredResult(original: Path, clean: Path | None, report: dict, reused: bool)` со свойством `best` (чистая копия или оригинал), `resolve_stored(stored) -> Path | None`, `clean_root()`, `clean_path_for(original) -> Path | None` (вне inbound — None), `sidecar_for(clean)`, `read_sidecar(clean)`, `ensure_clean(stored, *, force=False) -> StoredResult | None` (None — файла нет; вне inbound — `report={"outside_inbound": True}`), `discard(original) -> bool`, `clean_or_original(original) -> Path`, `is_current(original) -> bool` (без хэширования), `site_source(original) -> Path` (чистая копия только при `WATERMARK_SITE_USE_CLEAN=1`).

- [ ] **Step 1: Написать падающие тесты**

`tests/watermark/test_store.py`:

```python
"""Clean copies: data/clean/<rel>, sidecar, reuse; originals are never touched."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from PIL import Image

import src.watermark.store as store
from src.config import get_settings
from src.watermark.templates import load_templates
from tests.watermark.synth import apply_mark, make_background


@pytest.fixture
def dirs(tmp_path: Path, monkeypatch):
    inbound, clean = tmp_path / "inbound", tmp_path / "clean"
    inbound.mkdir()
    monkeypatch.setattr(get_settings(), "inbound_photos_dir", inbound)
    monkeypatch.setattr(get_settings(), "watermark_clean_dir", clean)
    monkeypatch.setattr(get_settings(), "watermark_lama_enabled", False)
    return inbound, clean


def _photo(path: Path, marked: bool) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = make_background(1440, 1080, seed=5)
    if marked:
        img = apply_mark(img, {t.id: t for t in load_templates()}["zida_logo"])
    Image.fromarray(img).save(path, "JPEG", quality=92)
    return path


def test_clean_copy_mirrors_inbound_layout(dirs):
    inbound, clean = dirs
    src = _photo(inbound / "7" / "123" / "00.jpg", marked=True)
    before = src.read_bytes()
    result = store.ensure_clean(str(src))
    assert result is not None and result.clean == (clean / "7" / "123" / "00.jpg").resolve()
    assert result.clean.is_file() and not result.reused
    assert store.sidecar_for(result.clean).is_file()
    assert src.read_bytes() == before
    assert store.clean_or_original(src) == result.clean


def test_second_call_reuses_until_source_changes(dirs):
    inbound, _clean = dirs
    src = _photo(inbound / "1" / "1" / "00.jpg", marked=True)
    first = store.ensure_clean(str(src))
    second = store.ensure_clean(str(src))
    assert first is not None and second is not None and second.reused
    _photo(src, marked=True)  # same content -> same sha -> reused again
    assert store.ensure_clean(str(src)).reused  # type: ignore[union-attr]
    third = store.ensure_clean(str(src), force=True)
    assert third is not None and not third.reused


def test_photo_without_marks_gets_no_copy(dirs):
    inbound, _clean = dirs
    src = _photo(inbound / "2" / "2" / "00.jpg", marked=False)
    result = store.ensure_clean(str(src))
    assert result is not None and result.clean is None
    assert store.clean_or_original(src) == src.resolve()


def test_relative_stored_paths_resolve_against_project_root(dirs, monkeypatch, tmp_path):
    inbound, _clean = dirs
    src = _photo(inbound / "3" / "3" / "00.jpg", marked=True)
    monkeypatch.setattr(store, "PROJECT_ROOT", tmp_path)
    assert store.ensure_clean("inbound/3/3/00.jpg").clean is not None  # type: ignore[union-attr]
    assert src.is_file()


def test_missing_and_foreign_paths(dirs, tmp_path):
    assert store.ensure_clean(str(tmp_path / "nope.jpg")) is None
    foreign = _photo(tmp_path / "elsewhere" / "x.jpg", marked=True)
    result = store.ensure_clean(str(foreign))
    assert result is not None and result.clean is None and result.report == {"outside_inbound": True}


def test_is_current_follows_sidecar_and_original(dirs):
    inbound, _clean = dirs
    src = _photo(inbound / "4" / "4" / "00.jpg", marked=True)
    assert not store.is_current(src)
    store.ensure_clean(str(src))
    assert store.is_current(src)
    sidecar = store.sidecar_for(store.clean_path_for(src))  # type: ignore[arg-type]
    later = sidecar.stat().st_mtime_ns + 10_000_000
    os.utime(src, ns=(later, later))  # the original was replaced after cleaning
    assert not store.is_current(src)
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `uv run pytest -q tests/watermark/test_store.py`
Expected: `ModuleNotFoundError: No module named 'src.watermark.store'`.

- [ ] **Step 3: Реализация**

`src/watermark/store.py`:

```python
"""Clean photo copies: data/clean/<path relative to inbound_photos_dir> + a sidecar report.

Originals (InboundPost.photo_paths, data/inbound/...) are never modified. Next to each clean
copy lives `<name>.wm.json` with the algorithm version, the source sha256 and the report. A
repeated call with the same source and version recomputes nothing. When no marks are found,
no clean copy is created (the sidecar says changed=false) and consumers use the original.

Bump STORE_VERSION when templates or the algorithm change: everything is then recomputed.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from src.config import PROJECT_ROOT, get_settings
from src.watermark.pipeline import clean_file

STORE_VERSION = 1
SIDECAR_SUFFIX = ".wm.json"


@dataclass(frozen=True)
class StoredResult:
    original: Path
    clean: Path | None  # None: no marks, use the original
    report: dict[str, object]
    reused: bool

    @property
    def best(self) -> Path:
        return self.clean if self.clean is not None else self.original


def resolve_stored(stored: str) -> Path | None:
    """InboundPost.photo_paths entry -> existing absolute file (same rules as src/site/photos)."""
    if not stored or "://" in stored:
        return None
    path = Path(stored)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    try:
        path = path.resolve()
    except OSError:
        return None
    return path if path.is_file() else None


def clean_root() -> Path:
    return Path(get_settings().watermark_clean_dir).resolve()


def clean_path_for(original: Path) -> Path | None:
    """data/inbound/<rel> -> data/clean/<rel> (.jpg); None when the file is outside inbound."""
    inbound = Path(get_settings().inbound_photos_dir).resolve()
    try:
        rel = original.resolve().relative_to(inbound)
    except (OSError, ValueError):
        return None
    target = clean_root() / rel
    if target.suffix.lower() not in (".jpg", ".jpeg"):
        target = target.with_name(target.name + ".jpg")
    return target


def sidecar_for(clean: Path) -> Path:
    return clean.with_name(clean.name + SIDECAR_SUFFIX)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_sidecar(clean: Path) -> dict[str, object] | None:
    try:
        data = json.loads(sidecar_for(clean).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_sidecar(clean: Path, payload: dict[str, object]) -> None:
    path = sidecar_for(clean)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def ensure_clean(stored: str, *, force: bool = False) -> StoredResult | None:
    """Make sure one photo has an up-to-date clean copy. None: the file is not on disk.

    A photo outside data/inbound (for example an already clean copy from a frozen delivery
    payload) is returned as is, and no clean copy is created for it.
    """
    original = resolve_stored(stored)
    if original is None:
        return None
    clean = clean_path_for(original)
    if clean is None:
        return StoredResult(
            original=original, clean=None, report={"outside_inbound": True}, reused=True
        )
    digest = _sha256(original)
    meta = read_sidecar(clean)
    if (
        not force
        and meta is not None
        and meta.get("version") == STORE_VERSION
        and meta.get("sha256") == digest
        and (not meta.get("changed") or clean.is_file())
    ):
        report = meta.get("report")
        return StoredResult(
            original=original,
            clean=clean if meta.get("changed") else None,
            report=report if isinstance(report, dict) else {},
            reused=True,
        )
    report = clean_file(original, clean, quality=get_settings().watermark_jpeg_quality)
    if not report.changed and clean.exists():
        clean.unlink()  # stale copy from an older algorithm version
    _write_sidecar(
        clean,
        {
            "version": STORE_VERSION,
            "sha256": digest,
            "changed": report.changed,
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "source": str(original),
            "report": report.to_dict(),
        },
    )
    return StoredResult(
        original=original,
        clean=clean if report.changed else None,
        report=report.to_dict(),
        reused=False,
    )


def discard(original: Path) -> bool:
    """Drop the clean copy of one photo and remember the decision: the sidecar then says
    changed=false (with discarded=true), so sweeps and /clean keep using the original until
    `ensure_clean(..., force=True)`. Returns False for files outside the inbound tree."""
    clean = clean_path_for(original)
    if clean is None:
        return False
    if clean.exists():
        clean.unlink()
    _write_sidecar(
        clean,
        {
            "version": STORE_VERSION,
            "sha256": _sha256(original),
            "changed": False,
            "discarded": True,
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "source": str(original),
            "report": {"discarded": True},
        },
    )
    return True


def clean_or_original(original: Path) -> Path:
    """For display: the clean copy when it exists for the current version, else the original."""
    clean = clean_path_for(original)
    if clean is None or not clean.is_file():
        return original
    meta = read_sidecar(clean)
    if meta is None or meta.get("version") != STORE_VERSION or not meta.get("changed"):
        return original
    return clean


def is_current(original: Path) -> bool:
    """True when a sidecar for this algorithm version exists and is not older than the
    original. No hashing: the periodic site sweep calls this for every public photo."""
    clean = clean_path_for(original)
    if clean is None:
        return True
    try:
        if sidecar_for(clean).stat().st_mtime_ns < original.stat().st_mtime_ns:
            return False
    except OSError:
        return False
    meta = read_sidecar(clean)
    if meta is None or meta.get("version") != STORE_VERSION:
        return False
    return not meta.get("changed") or clean.is_file()


def site_source(original: Path) -> Path:
    """What the site shows instead of the original: the clean copy if WATERMARK_SITE_USE_CLEAN=1."""
    if not get_settings().watermark_site_use_clean:
        return original
    return clean_or_original(original)
```

- [ ] **Step 4: Тесты проходят**

Run: `uv run pytest -q tests/watermark/test_store.py`
Expected: `6 passed`.

- [ ] **Step 5: Lint-гейт и commit**

```bash
uv run python scripts/check_changed_lint.py
git add src/watermark/store.py tests/watermark/test_store.py
git commit -m "feat(watermark): clean copies with sidecar reports, originals untouched"
```

---

### Task 5: Команды `otz wm-*` и чистые копии для сайта

**Files:**
- Create: `src/watermark/published.py`, `src/watermark/cli.py`
- Modify: `src/cli.py` (импорт и регистрация команд)
- Test: `tests/watermark/test_cli.py`, `tests/watermark/test_published.py`

**Interfaces:**
- Consumes: `src.watermark.pipeline`, `src.watermark.store`, `src.watermark.lama.download_model`, `src.site.photos.generate_for_stored_paths` и `VARIANT_WIDTHS` (существуют в репозитории), модели `InboundPost`, `Match`, `MatchStatus`, `SiteListing`.
- Produces: `src.watermark.published` — `SWEEP_INTERVAL_SECONDS = 600`, `SWEEP_BATCH = 4`, `async published_photo_paths() -> list[str]`, `pending(paths) -> list[str]`, `clean_batch(paths, *, force=False) -> tuple[int, int]` (с метками, нет на диске), `async sweep_once(run=None) -> int`, `async sweep_forever(run=None)`; `run` — корутина-исполнитель `(func, *args)` (бот передаёт `wm_bot.run_exclusive`).
- Produces: `src.watermark.cli` — `register_watermark_commands(group)`; команды `wm-clean PATHS --out-dir`, `wm-qa FOLDER --out-dir [--expect CSV]` (пишет `*.pair.jpg`, `*.marks.jpg`, `report.csv`; при `--expect` код выхода 1 на любое расхождение), `wm-clean-match ID [--force]`, `wm-revert ID`, `wm-fetch-model [--dest]`, `wm-clean-published [--limit N] [--force] [--dry-run]`; `async _match_photo_paths(match_id) -> list[str] | None` (тесты его подменяют).

- [ ] **Step 1: Написать падающие тесты**

`tests/watermark/test_cli.py`:

```python
"""`otz wm-clean` and `otz wm-qa` on generated photos (no network, no torch)."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner
from PIL import Image

from src.cli import cli
from src.config import get_settings
from src.watermark.templates import load_templates
from tests.watermark.synth import apply_mark, make_background


@pytest.fixture
def samples(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setattr(get_settings(), "watermark_lama_enabled", False)
    folder = tmp_path / "samples"
    (folder / "zida").mkdir(parents=True)
    tpl = {t.id: t for t in load_templates()}["zida_logo"]
    marked = apply_mark(make_background(1440, 1080, seed=1), tpl)
    Image.fromarray(marked).save(folder / "zida" / "marked.jpg", "JPEG", quality=92)
    Image.fromarray(make_background(800, 600, seed=2)).save(folder / "plain.jpg", "JPEG", quality=92)
    return folder


def test_wm_clean_writes_only_marked_files(samples, tmp_path):
    out = tmp_path / "out"
    result = CliRunner().invoke(
        cli,
        ["wm-clean", str(samples / "zida" / "marked.jpg"), str(samples / "plain.jpg"),
         "--out-dir", str(out)],
    )
    assert result.exit_code == 0, result.output
    assert (out / "marked.clean.jpg").is_file()
    assert not (out / "plain.clean.jpg").exists()


def test_wm_qa_pairs_report_and_expectations(samples, tmp_path):
    good = tmp_path / "good.csv"
    good.write_text("file,marks\nzida/marked.jpg,zida_logo#1;zida_logo#0\nplain.jpg,\n")
    out = tmp_path / "qa"
    args = ["wm-qa", str(samples), "--out-dir", str(out)]
    result = CliRunner().invoke(cli, [*args, "--expect", str(good)])
    assert result.exit_code == 0, result.output
    assert "all matched" in result.output
    assert (out / "zida__marked.pair.jpg").is_file()
    assert (out / "zida__marked.marks.jpg").is_file()  # zoomed crops for the visual review
    assert not (out / "plain.marks.jpg").exists()
    assert "zida/marked.jpg,zida_logo#0" in (out / "report.csv").read_text()

    bad = tmp_path / "bad.csv"
    bad.write_text("file,marks\nzida/marked.jpg,\nplain.jpg,halo_corner#0\nmissing.jpg,\n")
    result = CliRunner().invoke(cli, [*args, "--expect", str(bad)])
    assert result.exit_code == 1
    assert result.output.count("MISMATCH") == 3
```

`tests/watermark/test_published.py`:

```python
"""Clean copies for public site photos: the sweep does each photo once; wm-revert sticks."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner
from PIL import Image

import src.watermark.cli as wm_cli
from src.cli import cli
from src.config import get_settings
from src.db import get_session
from src.db.models import (
    InboundPost,
    Listing,
    Match,
    MatchStatus,
    Platform,
    SiteListing,
    SourceChannel,
)
from src.watermark import published, store
from src.watermark.templates import load_templates
from tests.watermark.synth import apply_mark, make_background


@pytest.fixture
def inbound(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "inbound"
    root.mkdir()
    s = get_settings()
    monkeypatch.setattr(s, "inbound_photos_dir", root)
    monkeypatch.setattr(s, "site_photos_root", root)
    monkeypatch.setattr(s, "watermark_clean_dir", tmp_path / "clean")
    monkeypatch.setattr(s, "watermark_lama_enabled", False)
    monkeypatch.setattr(s, "watermark_site_use_clean", False)
    return root


def _write_photos(root: Path, msg: int) -> list[str]:
    tpl = {t.id: t for t in load_templates()}["zida_logo"]
    paths = []
    for i in range(2):
        path = root / "1" / str(msg) / f"{i:02d}.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        img = apply_mark(make_background(1440, 1080, seed=40 + msg + i), tpl)
        Image.fromarray(img).save(path, "JPEG", quality=92)
        paths.append(str(path))
    return paths


async def _apartment(root: Path, msg: int, *, public: bool) -> list[str]:
    paths = _write_photos(root, msg)
    async with get_session() as s:
        if await s.get(SourceChannel, 1) is None:
            s.add(SourceChannel(id=1, tg_id=-100, username="src"))
        listing = Listing(
            platform=Platform.CETIRI_ZIDA, external_id=f"z{msg}", url=f"https://www.4zida.rs/{msg}"
        )
        post = InboundPost(source_channel_id=1, tg_message_id=msg, raw_text="", photo_paths=paths)
        s.add_all([listing, post])
        await s.flush()
        match = Match(
            listing_id=listing.id, inbound_post_id=post.id, confidence=1.0,
            matched_photo_count=2, method="phash", status=MatchStatus.PUBLISHED,
        )
        s.add(match)
        await s.flush()
        s.add(SiteListing(match_id=match.id, is_public=public))
    return paths


def _cleaned(paths: list[str]) -> bool:
    return all(store.clean_or_original(Path(p)) != Path(p).resolve() for p in paths)


async def test_sweep_cleans_public_photos_once(db, inbound):
    public = await _apartment(inbound, 31, public=True)
    hidden = await _apartment(inbound, 32, public=False)
    assert await published.sweep_once() == 2
    assert _cleaned(public)
    assert published.pending(hidden) == hidden  # apartments not on the site are left alone
    assert await published.sweep_once() == 0


def test_wm_revert_keeps_the_original(inbound, monkeypatch):
    paths = _write_photos(inbound, 33)
    for stored in paths:
        store.ensure_clean(stored)
    assert _cleaned(paths)

    async def fake_paths(_match_id: int) -> list[str]:
        return paths

    monkeypatch.setattr(wm_cli, "_match_photo_paths", fake_paths)
    result = CliRunner().invoke(cli, ["wm-revert", "7"])
    assert result.exit_code == 0, result.output
    assert not any(store.clean_or_original(Path(p)) != Path(p).resolve() for p in paths)
    assert published.pending(paths) == []  # the decision sticks: no automatic re-cleaning
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `uv run pytest -q tests/watermark/test_cli.py tests/watermark/test_published.py`
Expected: `ModuleNotFoundError: No module named 'src.watermark.cli'` и `Interrupted: 1 error during collection`.

- [ ] **Step 3: Проход по публичным карточкам**

`src/watermark/published.py`:

```python
"""Clean copies for photos of public site listings (used with WATERMARK_SITE_USE_CLEAN=1).

`otz wm-clean-published` runs this once by hand; the bot daemon (`otz bot`) repeats
`sweep_once` every SWEEP_INTERVAL_SECONDS, so newly published apartments get clean copies and
fresh site variants without manual steps. The heavy work stays in the bot process (its unit has
Nice=10 and MemoryMax) and goes through the caller's `run` (wm_bot.run_exclusive: one CPU job
at a time, a few photos per call, so /clean requests are not blocked for long).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from src.config import get_settings
from src.watermark.store import ensure_clean, is_current, resolve_stored

logger = logging.getLogger(__name__)

SWEEP_INTERVAL_SECONDS = 600
SWEEP_BATCH = 4

Runner = Callable[..., Awaitable[Any]]


async def published_photo_paths() -> list[str]:
    """InboundPost.photo_paths of every public SiteListing whose match is PUBLISHED."""
    from sqlalchemy import select

    from src.db import get_session
    from src.db.models import InboundPost, Match, MatchStatus, SiteListing

    async with get_session() as session:
        rows = (
            await session.execute(
                select(InboundPost.photo_paths)
                .join(Match, Match.inbound_post_id == InboundPost.id)
                .join(SiteListing, SiteListing.match_id == Match.id)
                .where(SiteListing.is_public.is_(True), Match.status == MatchStatus.PUBLISHED)
            )
        ).scalars().all()
    return [str(p) for paths in rows for p in (paths or []) if p]


def pending(paths: list[str]) -> list[str]:
    """Stored paths whose clean copy is missing or outdated (cheap: stat + sidecar read)."""
    out = []
    for stored in paths:
        original = resolve_stored(stored)
        if original is not None and not is_current(original):
            out.append(stored)
    return out


def clean_batch(paths: list[str], *, force: bool = False) -> tuple[int, int]:
    """ensure_clean for each path -> (photos that had marks, photos missing on disk)."""
    changed = missing = 0
    for stored in paths:
        result = ensure_clean(stored, force=force)
        if result is None:
            missing += 1
        elif result.clean is not None:
            changed += 1
    return changed, missing


async def _in_thread(func: Callable[..., Any], *args: Any) -> Any:
    return await asyncio.to_thread(func, *args)


async def sweep_once(run: Runner | None = None) -> int:
    """Clean new photos of public listings and refresh their site variants; returns their count."""
    runner = run or _in_thread
    todo = await asyncio.to_thread(pending, await published_photo_paths())
    for start in range(0, len(todo), SWEEP_BATCH):
        await runner(clean_batch, todo[start : start + SWEEP_BATCH])
    if todo and get_settings().watermark_site_use_clean:
        from src.site.photos import generate_for_stored_paths

        await asyncio.to_thread(generate_for_stored_paths, todo)
    return len(todo)


async def sweep_forever(run: Runner | None = None) -> None:
    while True:
        try:
            done = await sweep_once(run)
            if done:
                logger.info("[watermark] site sweep cleaned %d new photos", done)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[watermark] site sweep failed; retrying later")
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
```

- [ ] **Step 4: Команды**

`src/watermark/cli.py`:

```python
"""`otz wm-*` commands; registered on the main `otz` group in src/cli.py.

Heavy imports (numpy/cv2/torch) stay inside the commands so other `otz` commands start fast.
"""

from __future__ import annotations

import asyncio
import csv
from pathlib import Path

import click

_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".avif"}


def register_watermark_commands(group: click.Group) -> None:
    for command in (
        wm_clean, wm_qa, wm_clean_match, wm_revert, wm_fetch_model, wm_clean_published
    ):
        group.add_command(command)


def _report_line(name: str, report: object) -> str:
    from src.watermark.pipeline import CleanReport

    assert isinstance(report, CleanReport)
    if not report.changed:
        return f"{name}: no marks ({report.seconds:.2f}s)"
    parts = [f"{m.template_id}#{m.instance} score={m.score:.2f} {m.method}" for m in report.marks]
    return f"{name}: " + "; ".join(parts) + f" ({report.seconds:.2f}s)"


def _marks_key(report: object) -> str:
    """Stable text form of the found marks: "halo_corner#0;zida_logo#0" ("" when none)."""
    from src.watermark.pipeline import CleanReport

    assert isinstance(report, CleanReport)
    return ";".join(sorted(f"{m.template_id}#{m.instance}" for m in report.marks))


def _marks_sheet(before: object, after: object, report: object) -> object | None:
    """before|after crops around every found mark, stacked; None when nothing was found."""
    import numpy as np
    from PIL import Image

    from src.watermark.pipeline import CleanReport
    from src.watermark.templates import load_templates

    assert isinstance(report, CleanReport)
    assert isinstance(before, np.ndarray) and isinstance(after, np.ndarray)
    sizes = {t.id: (t.width, t.height) for t in load_templates()}
    h, w = before.shape[:2]
    rows = []
    for m in report.marks:
        tw, th = sizes.get(m.template_id, (0, 0))
        x0, y0 = max(0, int(m.x) - 12), max(0, int(m.y) - 12)
        x1, y1 = min(w, int(m.x + tw * m.scale) + 12), min(h, int(m.y + th * m.scale) + 12)
        if x1 <= x0 or y1 <= y0:
            continue
        gap = np.full((y1 - y0, 6, 3), 255, np.uint8)
        row = Image.fromarray(np.concatenate([before[y0:y1, x0:x1], gap, after[y0:y1, x0:x1]], 1))
        scale = min(300 / row.height, 1600 / row.width)
        rows.append(row.resize((round(row.width * scale), round(row.height * scale))))
    if not rows:
        return None
    sheet = Image.new("RGB", (max(r.width for r in rows), sum(r.height + 6 for r in rows)), "white")
    y = 0
    for row in rows:
        sheet.paste(row, (0, y))
        y += row.height + 6
    return sheet


def _read_expectations(path: Path) -> dict[str, str]:
    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if rows and not {"file", "marks"} <= set(rows[0]):
        raise click.ClickException(f"{path}: expected columns file,marks")
    return {row["file"]: ";".join(sorted(filter(None, row["marks"].split(";")))) for row in rows}


@click.command("wm-clean")
@click.argument(
    "paths", nargs=-1, required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path)
)
@click.option(
    "--out-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Where to write <name>.clean.jpg (default: next to the source).",
)
def wm_clean(paths: tuple[Path, ...], out_dir: Path | None) -> None:
    """Remove platform marks from arbitrary files (sources are never modified)."""
    from src.watermark.pipeline import NotAnImageError, clean_file

    for src in paths:
        dst = (out_dir or src.parent) / f"{src.stem}.clean.jpg"
        try:
            report = clean_file(src, dst)
        except NotAnImageError as error:
            click.echo(f"{src}: not an image ({error})")
            continue
        suffix = f" -> {dst}" if report.changed else ""
        click.echo(_report_line(str(src), report) + suffix)


@click.command("wm-qa")
@click.argument("folder", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--out-dir", type=click.Path(file_okay=False, path_type=Path), required=True)
@click.option(
    "--expect",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="CSV file,marks (marks like 'zida_logo#0;zida_logo#1'); exit 1 on any mismatch.",
)
def wm_qa(folder: Path, out_dir: Path, expect: Path | None) -> None:
    """Run over a folder: before|after pairs, zoomed mark crops and report.csv to review."""
    import numpy as np
    from PIL import Image

    from src.watermark.pipeline import NotAnImageError, clean_array, decode_image

    out_dir.mkdir(parents=True, exist_ok=True)
    rows: list[list[str]] = []
    actual: dict[str, str] = {}
    files = sorted(p for p in folder.rglob("*") if p.suffix.lower() in _IMAGE_SUFFIXES)
    for src in files:
        rel = src.relative_to(folder).as_posix()
        try:
            img = decode_image(src.read_bytes())
        except NotAnImageError:
            click.echo(f"{rel}: not an image, skipped")
            continue
        cleaned, report = clean_array(img)
        actual[rel] = _marks_key(report)
        gap = np.full((img.shape[0], 8, 3), 255, np.uint8)
        pair = np.concatenate([img, gap, cleaned], axis=1)
        flat = rel.replace("/", "__")
        Image.fromarray(pair).save(out_dir / f"{Path(flat).stem}.pair.jpg", quality=88)
        sheet = _marks_sheet(img, cleaned, report)
        if sheet is not None:
            sheet.save(out_dir / f"{Path(flat).stem}.marks.jpg", quality=90)  # type: ignore[attr-defined]
        if not report.marks:
            rows.append([rel, "", "", "", "", f"{report.seconds:.2f}"])
        for m in report.marks:
            rows.append(
                [rel, f"{m.template_id}#{m.instance}", f"{m.score:.3f}", m.method,
                 f"{m.residual:.3f}", f"{report.seconds:.2f}"]
            )
        click.echo(_report_line(rel, report))
    with (out_dir / "report.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["file", "mark", "score", "method", "residual", "seconds"])
        writer.writerows(rows)
    marked = sum(1 for key in actual.values() if key)
    click.echo(f"=== {len(actual)} images, marks in {marked}, report: {out_dir / 'report.csv'} ===")
    if expect is None:
        return
    problems = []
    for rel, wanted in sorted(_read_expectations(expect).items()):
        got = actual.get(rel)
        if got is None:
            problems.append(f"{rel}: missing from {folder}")
        elif got != wanted:
            problems.append(f"{rel}: expected [{wanted}] got [{got}]")
    for line in problems:
        click.echo(f"MISMATCH {line}")
    if problems:
        raise click.ClickException(f"QA failed: {len(problems)} mismatches")
    click.echo("QA expectations: all matched")


async def _match_photo_paths(match_id: int) -> list[str] | None:
    from src.db import get_session
    from src.db.models import InboundPost, Match

    async with get_session() as session:
        match = await session.get(Match, match_id)
        if match is None:
            return None
        inbound = await session.get(InboundPost, match.inbound_post_id)
        return list(inbound.photo_paths or []) if inbound is not None else []


@click.command("wm-clean-match")
@click.argument("match_id", type=int)
@click.option("--force", is_flag=True, help="Recompute even if the clean copy is current.")
def wm_clean_match(match_id: int, force: bool) -> None:
    """Make clean copies (data/clean/...) of one match's photos."""
    from src.watermark.store import ensure_clean

    paths = asyncio.run(_match_photo_paths(match_id))
    if paths is None:
        raise click.ClickException(f"Match {match_id} not found")
    for stored in paths:
        result = ensure_clean(stored, force=force)
        if result is None:
            click.echo(f"{stored}: file not on disk")
        elif result.clean is None:
            click.echo(f"{stored}: no marks")
        else:
            state = "reused" if result.reused else "cleaned"
            click.echo(f"{stored}: {state} -> {result.clean}")


@click.command("wm-revert")
@click.argument("match_id", type=int)
def wm_revert(match_id: int) -> None:
    """Go back to the original photos of one match (site variants are rebuilt from them).

    For a cleaned photo that looks wrong: removes its clean copies, remembers the decision
    (sweeps will not recreate them) and regenerates the site card/detail images.
    """
    from src.site.photos import VARIANT_WIDTHS, generate_for_stored_paths
    from src.watermark.store import discard, resolve_stored

    paths = asyncio.run(_match_photo_paths(match_id))
    if paths is None:
        raise click.ClickException(f"Match {match_id} not found")
    reverted = []
    for stored in paths:
        original = resolve_stored(stored)
        if original is None or not discard(original):
            click.echo(f"{stored}: skipped (not on disk or outside inbound)")
            continue
        for kind in VARIANT_WIDTHS:  # generated files; rebuilt right below from the original
            original.with_name(f"{original.name}.{kind}.webp").unlink(missing_ok=True)
        reverted.append(stored)
        click.echo(f"{stored}: original restored")
    written = generate_for_stored_paths(reverted) if reverted else 0
    click.echo(f"=== reverted {len(reverted)} photos, site variants rebuilt: {written} ===")


@click.command("wm-fetch-model")
@click.option("--dest", type=click.Path(dir_okay=False, path_type=Path), default=None)
def wm_fetch_model(dest: Path | None) -> None:
    """Download the LaMa model (~206 MB) and verify its sha256."""
    from src.config import get_settings
    from src.watermark.lama import MODEL_SHA256, download_model

    target = dest or Path(get_settings().watermark_lama_model_path)
    path = download_model(target)
    size = path.stat().st_size
    click.echo(f"LaMa: {path} ({size} bytes, sha256 {MODEL_SHA256[:12]}...)")


@click.command("wm-clean-published")
@click.option("--limit", type=int, default=None, help="Process at most N photos.")
@click.option("--force", is_flag=True, help="Recompute even current clean copies.")
@click.option("--dry-run", is_flag=True, help="Only count photos, write nothing.")
def wm_clean_published(limit: int | None, force: bool, dry_run: bool) -> None:
    """Clean copies for all public site listings + rebuild their webp variants.

    Variants are rebuilt only with WATERMARK_SITE_USE_CLEAN=1 (otherwise the site ignores
    clean copies). The bot daemon repeats this every 10 minutes for new publications; run it
    by hand once after enabling the flag. Reruns only process new or changed photos.
    """
    from src.config import get_settings
    from src.watermark.published import clean_batch, pending, published_photo_paths

    paths = asyncio.run(published_photo_paths())
    if not force:
        paths = pending(paths)
    if limit is not None:
        paths = paths[:limit]
    click.echo(f"wm-clean-published: {len(paths)} photos{' [DRY-RUN]' if dry_run else ''}")
    if dry_run:
        return
    changed = missing = 0
    for start in range(0, len(paths), 50):
        batch_changed, batch_missing = clean_batch(paths[start : start + 50], force=force)
        changed, missing = changed + batch_changed, missing + batch_missing
        click.echo(f"  ... {min(start + 50, len(paths))}/{len(paths)}")
    if get_settings().watermark_site_use_clean:
        from src.site.photos import generate_for_stored_paths

        written = generate_for_stored_paths(paths)
        click.echo(f"  site variants rebuilt: {written}")
    else:
        click.echo("  WATERMARK_SITE_USE_CLEAN=0: site variants left untouched")
    click.echo(f"=== DONE: with_marks={changed} missing={missing} total={len(paths)} ===")
```

- [ ] **Step 5: Зарегистрировать команды в `otz`**

В `src/cli.py` после строки `from src.scrapers.shadow_audit import run_shadow_audit, write_report` добавить:

```python
from src.watermark.cli import register_watermark_commands
```

Сразу после функции группы

```python
@click.group()
def cli() -> None:
    """otz-rent-finder CLI."""
    _setup_logging()
```

добавить (с двумя пустыми строками до и после):

```python
register_watermark_commands(cli)
```

Run: `uv run otz --help | grep wm-`
Expected: шесть строк — `wm-clean`, `wm-clean-match`, `wm-clean-published`, `wm-fetch-model`, `wm-qa`, `wm-revert`.

- [ ] **Step 6: Тесты проходят**

Run: `uv run pytest -q tests/watermark`
Expected: `79 passed, 3 skipped`.

- [ ] **Step 7: Lint-гейт и commit**

```bash
uv run python scripts/check_changed_lint.py
git add src/watermark src/cli.py tests/watermark
git commit -m "feat(watermark): otz wm-* commands and clean copies for public listings"
```

---

### Task 6: Обработчики `/clean` и фото в боте (сессия B)

**Files:**
- Create: `src/telegram/wm_bot.py`
- Test: `tests/test_wm_bot.py`

**Interfaces:**
- Consumes: `clean_bytes`, `CleanReport`, `MarkResult`, `NotAnImageError`, `PLATFORM_TITLES` (Task 2); `ensure_clean`, `StoredResult` (Task 4); модели `InboundPost`, `Match`, `MatchDelivery`; настройки `tg_bot_recipients`, `watermark_allowed_chat_ids`, `watermark_site_use_clean`.
- Produces: `src.telegram.wm_bot` — тексты `HELP_TEXT`, `NOTHING_FOUND`, `NOT_FOUND_MATCH`, `ALREADY_CLEAN`, `MATCH_NOT_FOUND` (шаблон с `{match_id}`) и др.; `parse_chat_ids(*raw) -> frozenset[int]`, `allowed_chat_ids()`, `summarize(reports) -> str`, `is_image_document(document) -> bool`, `async run_exclusive(func, *args)` (CPU-работа в потоке, по одной задаче), `async process_photo_messages(messages)`, `async find_match_for_message(chat_id, message_id) -> int | None`, `async match_photo_paths(match_id, chat_id) -> list[str] | None`, `clean_stored_paths(paths) -> list[StoredResult]`, `async clean_match_and_reply(message, match_id)`, `async handle_clean_command(message, args)`, `AlbumCollector(handler, wait=1.5)` с `async add(message)`, `build_watermark_router(allowed) -> aiogram.Router` (имя `"watermark"`).

- [ ] **Step 1: Написать падающие тесты**

`tests/test_wm_bot.py`:

```python
"""RentFinderBot /clean: fake messages and bot, no network (like test_bot_media_contract.py)."""

from __future__ import annotations

import asyncio
import io
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from src.config import get_settings
from src.db import get_session
from src.db.models import InboundPost, Listing, Match, MatchDelivery, Platform, SourceChannel
from src.telegram import wm_bot
from src.watermark.pipeline import decode_image, find_marks
from src.watermark.templates import load_templates
from tests.watermark.synth import apply_mark, make_background

CHAT = 1003207518


def _jpeg(marked: bool, seed: int = 1) -> bytes:
    img = make_background(1440, 1080, seed=seed)
    if marked:
        img = apply_mark(img, {t.id: t for t in load_templates()}["zida_logo"])
    buf = io.BytesIO()
    Image.fromarray(img).save(buf, "JPEG", quality=92)
    return buf.getvalue()


class FakeBot:
    def __init__(self, files: dict[str, bytes]):
        self.files = files

    async def download(self, file_obj):
        return io.BytesIO(self.files[file_obj.file_id])


class FakeMessage:
    def __init__(self, message_id, *, bot=None, photo_id=None, document=None, group=None, reply=None):
        self.message_id = message_id
        self.chat = SimpleNamespace(id=CHAT, type="private")
        self.bot = bot
        self.photo = [SimpleNamespace(file_id=photo_id)] if photo_id else None
        self.document = document
        self.media_group_id = group
        self.reply_to_message = reply
        self.texts: list[str] = []
        self.photos: list[tuple[bytes, str | None]] = []
        self.documents: list[tuple[bytes, str | None]] = []
        self.albums: list[list] = []

    async def answer(self, text, **_kw):
        self.texts.append(text)

    async def answer_photo(self, photo, caption=None, **_kw):
        self.photos.append((photo.data, caption))

    async def answer_document(self, document, caption=None, **_kw):
        self.documents.append((document.data, caption))

    async def answer_media_group(self, media, **_kw):
        self.albums.append(media)


@pytest.fixture(autouse=True)
def _settings(monkeypatch, tmp_path):
    s = get_settings()
    monkeypatch.setattr(s, "watermark_lama_enabled", False)
    monkeypatch.setattr(s, "inbound_photos_dir", tmp_path / "inbound")
    monkeypatch.setattr(s, "watermark_clean_dir", tmp_path / "clean")
    monkeypatch.setattr(s, "watermark_site_use_clean", False)


def test_parse_chat_ids_ignores_garbage():
    assert wm_bot.parse_chat_ids("1, 2", "x,3,", "") == frozenset({1, 2, 3})


async def test_single_photo_comes_back_clean():
    bot = FakeBot({"f1": _jpeg(marked=True)})
    msg = FakeMessage(10, bot=bot, photo_id="f1")
    await wm_bot.process_photo_messages([msg])
    assert len(msg.photos) == 1
    data, caption = msg.photos[0]
    assert "4zida — 2" in (caption or "")
    assert find_marks(decode_image(data)) == []


async def test_photo_without_marks_is_reported():
    bot = FakeBot({"f1": _jpeg(marked=False)})
    msg = FakeMessage(11, bot=bot, photo_id="f1")
    await wm_bot.process_photo_messages([msg])
    assert msg.photos == [] and msg.texts[-1] == wm_bot.NOTHING_FOUND


async def test_image_document_comes_back_as_document():
    bot = FakeBot({"d1": _jpeg(marked=True)})
    doc = SimpleNamespace(file_id="d1", mime_type="image/jpeg", file_size=200_000, file_name="flat.jpg")
    msg = FakeMessage(12, bot=bot, document=doc)
    await wm_bot.process_photo_messages([msg])
    assert len(msg.documents) == 1


async def test_album_is_collected_into_one_reply():
    bot = FakeBot({"a": _jpeg(True, 1), "b": _jpeg(True, 2)})
    handled: list[list] = []

    async def handler(messages):
        handled.append(messages)

    collector = wm_bot.AlbumCollector(handler, wait=0.05)
    await collector.add(FakeMessage(21, bot=bot, photo_id="a", group="g1"))
    await collector.add(FakeMessage(22, bot=bot, photo_id="b", group="g1"))
    await asyncio.sleep(0.2)
    assert [[m.message_id for m in batch] for batch in handled] == [[21, 22]]


async def test_album_handler_errors_are_logged_not_raised(monkeypatch):
    logged: list[str] = []
    monkeypatch.setattr(
        wm_bot.logger, "exception", lambda msg, *args, **_kw: logged.append(msg % args)
    )

    async def boom(_messages):
        raise RuntimeError("telegram is down")

    collector = wm_bot.AlbumCollector(boom, wait=0.01)
    await collector.add(FakeMessage(60, photo_id="x", group="g9"))
    await asyncio.sleep(0.1)
    assert logged == [f"[wm-bot] album handler failed chat={CHAT}"]


async def test_clean_without_reply_shows_help():
    msg = FakeMessage(30)
    await wm_bot.handle_clean_command(msg, None)
    assert msg.texts == [wm_bot.HELP_TEXT]


def _all_exist(paths: list[str]) -> bool:
    return all(Path(p).is_file() for p in paths)


async def _seed_delivery(tmp_path: Path) -> tuple[int, list[str]]:
    inbound = get_settings().inbound_photos_dir
    paths = []
    for i in range(2):
        p = Path(inbound) / "1" / "77" / f"{i:02d}.jpg"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(_jpeg(marked=True, seed=10 + i))
        paths.append(str(p))
    async with get_session() as s:
        s.add(SourceChannel(id=1, tg_id=-100, username="src"))
        listing = Listing(platform=Platform.CETIRI_ZIDA, external_id="z1", url="https://www.4zida.rs/x")
        inbound_post = InboundPost(source_channel_id=1, tg_message_id=77, raw_text="", photo_paths=paths)
        s.add_all([listing, inbound_post])
        await s.flush()
        match = Match(listing_id=listing.id, inbound_post_id=inbound_post.id, confidence=1.0,
                      matched_photo_count=2, method="phash")
        s.add(match)
        await s.flush()
        s.add(MatchDelivery(match_id=match.id, recipient_id=CHAT, status="sent", message_ids=[501, 502],
                            payload={"caption": "c", "photo_paths": paths}))
        return match.id, paths


async def test_reply_to_delivered_album_cleans_that_match(db, tmp_path):
    match_id, paths = await _seed_delivery(tmp_path)
    msg = FakeMessage(40, reply=SimpleNamespace(message_id=502, photo=None, document=None))
    await wm_bot.handle_clean_command(msg, None)
    assert len(msg.albums) == 1 and len(msg.albums[0]) == 2
    assert "4zida — 4" in msg.albums[0][0].caption
    clean_copy = Path(get_settings().watermark_clean_dir) / "1" / "77" / "00.jpg"
    assert _all_exist([str(clean_copy), *paths])  # clean copy written, originals kept
    again = FakeMessage(41)
    await wm_bot.handle_clean_command(again, str(match_id))
    assert len(again.albums) == 1


async def test_reply_to_unknown_message(db):
    msg = FakeMessage(50, reply=SimpleNamespace(message_id=999, photo=None, document=None))
    await wm_bot.handle_clean_command(msg, None)
    assert msg.texts == [wm_bot.NOT_FOUND_MATCH]


async def test_unknown_match_id(db):
    msg = FakeMessage(51)
    await wm_bot.handle_clean_command(msg, "424242")
    assert msg.texts == [wm_bot.MATCH_NOT_FOUND.format(match_id=424242)]
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `uv run pytest -q tests/test_wm_bot.py`
Expected: `ImportError` / `ModuleNotFoundError` для `src.telegram.wm_bot`.

- [ ] **Step 3: Реализация**

`src/telegram/wm_bot.py`:

```python
"""RentFinderBot: the /clean command and incoming photos - remove listing-platform marks.

Only private chats whose chat_id is in TG_BOT_RECIPIENTS or WATERMARK_ALLOWED_CHAT_IDS:
- send/forward 1-10 photos (album, or image files) -> the bot replies with them cleaned;
- reply /clean to a candidate album the bot delivered -> clean copies of that match's
  originals (full resolution from disk; also stored in data/clean for the site);
- `/clean <match_id>` -> the same by match number;
- `/clean` with neither -> help text.

CPU work runs in a worker thread, one job at a time (the server CPU is shared with the
monitor and nightly jobs). Handlers are plain async functions so tests can call them with
fake messages and no live bot.
"""

from __future__ import annotations

import asyncio
import io
import logging
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import Any

from sqlalchemy import select

from src.config import get_settings
from src.db import get_session
from src.db.models import InboundPost, Match, MatchDelivery
from src.watermark.pipeline import (
    PLATFORM_TITLES,
    CleanReport,
    MarkResult,
    NotAnImageError,
    clean_bytes,
)
from src.watermark.store import StoredResult, ensure_clean

logger = logging.getLogger(__name__)

ALBUM_WAIT_SECONDS = 1.5
MAX_ALBUM = 10
MAX_DOCUMENT_BYTES = 20 * 1024 * 1024  # Bot API downloads files up to 20 MB
_WORK_LOCK = asyncio.Semaphore(1)

# User-facing texts (Russian, for Darya).
HELP_TEXT = (
    "🧽 Убираю водяные знаки Halo Oglasi, 4zida и CityExpert.\n\n"
    "• Пришлите или перешлите мне фото (можно альбомом до 10 штук) — верну без знаков.\n"
    "• Ответьте /clean на альбом кандидата — пришлю его фото без знаков в полном качестве.\n"  # noqa: RUF001
    "• /clean 123 — то же для кандидата №123.\n\n"
    "Знаки агентств (BEOSTAN и т. п.) пока не убираю."
)
NOTHING_FOUND = "Знаков площадок не нашёл — фото без изменений."
NOT_FOUND_MATCH = "Не нашёл кандидата для этого сообщения. Ответьте /clean на альбом кандидата."  # noqa: RUF001
ALREADY_CLEAN = "Фото этого кандидата уже были отправлены без знаков."
WORKING_PHOTOS = "🧽 Обрабатываю {n} фото…"
WORKING_MATCH = "🧽 Чищу фото кандидата №{match_id}…"
MATCH_NOT_FOUND = "Кандидат №{match_id} не найден."
MATCH_NO_PHOTOS = "У кандидата №{match_id} нет фото на диске."  # noqa: RUF001
NOT_A_PHOTO = "Файл {name} не похож на фото — пропускаю."
FAILED_ONE = "Не получилось обработать {name} — попробуйте ещё раз позже."  # noqa: RUF001
FAILED_MATCH = "Не получилось обработать фото — попробуйте ещё раз позже."  # noqa: RUF001
DONE_PREFIX = "🧽 Готово. Убрал знаки: "
UNMARKED_SUFFIX = " Без знаков: {clean} из {total}."


def parse_chat_ids(*raw_values: str) -> frozenset[int]:
    ids: set[int] = set()
    for raw in raw_values:
        for piece in (raw or "").split(","):
            piece = piece.strip()
            if not piece:
                continue
            try:
                ids.add(int(piece))
            except ValueError:
                logger.warning("[wm-bot] ignoring non-numeric chat id %r", piece)
    return frozenset(ids)


def allowed_chat_ids() -> frozenset[int]:
    settings = get_settings()
    return parse_chat_ids(settings.tg_bot_recipients, settings.watermark_allowed_chat_ids)


def summarize(reports: Iterable[CleanReport]) -> str:
    """Caption for the cleaned album, e.g. 'Halo Oglasi - 1, 4zida - 2' in Russian wording."""
    reports = list(reports)
    counts: dict[str, int] = {}
    for report in reports:
        for platform, n in report.platform_counts().items():
            counts[platform] = counts.get(platform, 0) + n
    clean = sum(1 for r in reports if not r.changed)
    if not counts:
        return NOTHING_FOUND
    order = [PLATFORM_TITLES[k] for k in ("halooglasi", "4zida", "cityexpert")]
    parts = [f"{name} — {counts[name]}" for name in order if name in counts]
    parts += [f"{name} — {n}" for name, n in counts.items() if name not in order]
    text = DONE_PREFIX + ", ".join(parts) + "."
    if clean:
        text += UNMARKED_SUFFIX.format(clean=clean, total=len(reports))
    return text


def is_image_document(document: Any) -> bool:
    mime = (getattr(document, "mime_type", "") or "").lower()
    size = getattr(document, "file_size", 0) or 0
    return mime.startswith("image/") and size <= MAX_DOCUMENT_BYTES


async def _download(bot: Any, file_obj: Any) -> bytes:
    buf = await bot.download(file_obj)
    if buf is None:
        raise RuntimeError("download returned nothing")
    data = buf.getvalue() if isinstance(buf, io.BytesIO) else buf.read()
    return bytes(data)


async def run_exclusive(func: Callable[..., Any], *args: Any) -> Any:
    """CPU work in a thread, one job at a time."""
    async with _WORK_LOCK:
        return await asyncio.to_thread(func, *args)


async def _send_files(
    message: Any, files: list[tuple[bytes, str]], caption: str, as_documents: bool
) -> None:
    from aiogram.types import BufferedInputFile, InputMediaDocument, InputMediaPhoto

    for start in range(0, len(files), MAX_ALBUM):
        chunk = files[start : start + MAX_ALBUM]
        cap = caption if start == 0 else None
        if len(chunk) == 1:
            data, name = chunk[0]
            upload = BufferedInputFile(data, filename=name)
            if as_documents:
                await message.answer_document(upload, caption=cap)
            else:
                await message.answer_photo(upload, caption=cap)
            continue
        media: list[Any] = []
        for i, (data, name) in enumerate(chunk):
            upload = BufferedInputFile(data, filename=name)
            kind = InputMediaDocument if as_documents else InputMediaPhoto
            media.append(kind(media=upload, caption=cap if i == 0 else None))
        await message.answer_media_group(media=media)


async def process_photo_messages(messages: list[Any]) -> None:
    """Photos sent to the bot: download -> clean -> send back (as photos or as files)."""
    first = messages[0]
    bot = first.bot
    items: list[tuple[Any, str]] = []
    as_documents = False
    for m in sorted(messages, key=lambda x: x.message_id)[:MAX_ALBUM]:
        if getattr(m, "photo", None):
            items.append((m.photo[-1], f"photo_{m.message_id}.jpg"))
        elif getattr(m, "document", None) is not None and is_image_document(m.document):
            as_documents = True
            stem = Path(m.document.file_name or f"photo_{m.message_id}").stem
            items.append((m.document, f"{stem}_clean.jpg"))
    if not items:
        await first.answer(HELP_TEXT)
        return
    await first.answer(WORKING_PHOTOS.format(n=len(items)))
    outputs: list[tuple[bytes, str]] = []
    reports: list[CleanReport] = []
    for file_obj, name in items:
        try:
            data = await _download(bot, file_obj)
            cleaned, report = await run_exclusive(clean_bytes, data)
        except NotAnImageError:
            await first.answer(NOT_A_PHOTO.format(name=name))
            continue
        except Exception:
            logger.exception("[wm-bot] cleaning failed chat=%s", first.chat.id)
            await first.answer(FAILED_ONE.format(name=name))
            continue
        outputs.append((cleaned, name))
        reports.append(report)
    if not outputs:
        return
    if not any(r.changed for r in reports):
        await first.answer(NOTHING_FOUND)
        return
    await _send_files(first, outputs, summarize(reports), as_documents)


async def find_match_for_message(chat_id: int, message_id: int) -> int | None:
    """The match whose album the bot delivered to this chat and that contains message_id."""
    async with get_session() as session:
        rows = (
            await session.execute(
                select(MatchDelivery.match_id, MatchDelivery.message_ids)
                .where(MatchDelivery.recipient_id == chat_id, MatchDelivery.status == "sent")
                .order_by(MatchDelivery.id.desc())
                .limit(5000)
            )
        ).all()
    for match_id, message_ids in rows:
        if message_id in (message_ids or []):
            return int(match_id)
    return None


async def match_photo_paths(match_id: int, chat_id: int) -> list[str] | None:
    """Match photos as delivered to this chat (frozen payload), else the inbound post photos.

    None: no such match.
    """
    async with get_session() as session:
        match = await session.get(Match, match_id)
        if match is None:
            return None
        payload = (
            await session.execute(
                select(MatchDelivery.payload).where(
                    MatchDelivery.match_id == match_id, MatchDelivery.recipient_id == chat_id
                )
            )
        ).scalar_one_or_none()
        if isinstance(payload, dict) and payload.get("photo_paths"):
            return [str(p) for p in payload["photo_paths"] if p]
        inbound = await session.get(InboundPost, match.inbound_post_id)
        return [str(p) for p in (inbound.photo_paths or [])] if inbound is not None else []


def clean_stored_paths(paths: list[str]) -> list[StoredResult]:
    results: list[StoredResult] = []
    for stored in paths:
        result = ensure_clean(stored)
        if result is not None:
            results.append(result)
    return results


async def _refresh_site_variants(paths: list[str]) -> None:
    if not get_settings().watermark_site_use_clean:
        return
    try:
        from src.site.photos import generate_for_stored_paths

        await asyncio.to_thread(generate_for_stored_paths, paths)
    except Exception:
        logger.warning("[wm-bot] site variant refresh failed (cleaning kept)", exc_info=True)


def _report_from_dict(result: StoredResult) -> CleanReport:
    """StoredResult keeps the report as a dict (from the sidecar); rebuild it for summarize()."""
    report = result.report if isinstance(result.report, dict) else {}
    raw_marks = report.get("marks")
    marks = [MarkResult(**m) for m in raw_marks or [] if isinstance(m, dict)]  # type: ignore[union-attr]
    width = int(report.get("width", 0) or 0)  # type: ignore[call-overload]
    height = int(report.get("height", 0) or 0)  # type: ignore[call-overload]
    return CleanReport(width=width, height=height, marks=marks if result.clean else [])


def _read_files(results: list[StoredResult], match_id: int) -> list[tuple[bytes, str]]:
    return [
        (r.best.read_bytes(), f"match{match_id}_{i + 1:02d}.jpg") for i, r in enumerate(results)
    ]


async def clean_match_and_reply(message: Any, match_id: int) -> None:
    paths = await match_photo_paths(match_id, message.chat.id)
    if paths is None:
        await message.answer(MATCH_NOT_FOUND.format(match_id=match_id))
        return
    if not paths:
        await message.answer(MATCH_NO_PHOTOS.format(match_id=match_id))
        return
    await message.answer(WORKING_MATCH.format(match_id=match_id))
    try:
        results: list[StoredResult] = await run_exclusive(clean_stored_paths, paths[:MAX_ALBUM])
    except Exception:
        logger.exception("[wm-bot] match %s cleaning failed", match_id)
        await message.answer(FAILED_MATCH)
        return
    if not results:
        await message.answer(MATCH_NO_PHOTOS.format(match_id=match_id))
        return
    if all(r.report.get("outside_inbound") for r in results):
        await message.answer(ALREADY_CLEAN)
        return
    reports = [_report_from_dict(r) for r in results]
    if not any(r.changed for r in reports):
        await message.answer(NOTHING_FOUND)
        return
    files = await asyncio.to_thread(_read_files, results, match_id)
    await _send_files(message, files, summarize(reports), as_documents=False)
    await _refresh_site_variants([str(r.original) for r in results])


async def handle_clean_command(message: Any, args: str | None) -> None:
    arg = (args or "").strip()
    if arg:
        if not arg.isdigit():
            await message.answer(HELP_TEXT)
            return
        await clean_match_and_reply(message, int(arg))
        return
    reply = getattr(message, "reply_to_message", None)
    if reply is None:
        await message.answer(HELP_TEXT)
        return
    match_id = await find_match_for_message(message.chat.id, reply.message_id)
    if match_id is not None:
        await clean_match_and_reply(message, match_id)
        return
    if getattr(reply, "photo", None) or (
        getattr(reply, "document", None) is not None and is_image_document(reply.document)
    ):
        await process_photo_messages([reply])
        return
    await message.answer(NOT_FOUND_MATCH)


class AlbumCollector:
    """Telegram delivers an album as separate messages sharing media_group_id: collect them
    for ALBUM_WAIT_SECONDS and handle them together."""

    def __init__(
        self,
        handler: Callable[[list[Any]], Awaitable[None]],
        wait: float = ALBUM_WAIT_SECONDS,
    ) -> None:
        self._handler = handler
        self._wait = wait
        self._groups: dict[tuple[int, str], list[Any]] = {}
        self._tasks: dict[tuple[int, str], asyncio.Task[None]] = {}

    async def add(self, message: Any) -> None:
        group_id = getattr(message, "media_group_id", None)
        if not group_id:
            await self._handler([message])
            return
        key = (int(message.chat.id), str(group_id))
        self._groups.setdefault(key, []).append(message)
        if key not in self._tasks:
            self._tasks[key] = asyncio.create_task(self._flush(key))

    async def _flush(self, key: tuple[int, str]) -> None:
        await asyncio.sleep(self._wait)
        messages = self._groups.pop(key, [])
        self._tasks.pop(key, None)
        if not messages:
            return
        try:
            await self._handler(messages)
        except Exception:  # a background task has nobody to report to: log it
            logger.exception("[wm-bot] album handler failed chat=%s", key[0])


def build_watermark_router(allowed: frozenset[int]) -> Any:
    """aiogram Router: private chats from `allowed` only; other updates fall through."""
    from aiogram import F, Router
    from aiogram.filters import Command, CommandObject
    from aiogram.types import Message

    router = Router(name="watermark")
    router.message.filter(F.chat.type == "private", F.chat.id.in_(allowed))
    collector = AlbumCollector(process_photo_messages)

    @router.message(Command("clean", "watermark"))
    async def _on_clean(message: Message, command: CommandObject) -> None:  # pragma: no cover
        await handle_clean_command(message, command.args)

    @router.message(F.photo | F.document)
    async def _on_media(message: Message) -> None:  # pragma: no cover
        await collector.add(message)

    return router
```

- [ ] **Step 4: Тесты проходят**

Run: `uv run pytest -q tests/test_wm_bot.py`
Expected: `10 passed`.

- [ ] **Step 5: Lint-гейт и commit**

```bash
uv run python scripts/check_changed_lint.py
git add src/telegram/wm_bot.py tests/test_wm_bot.py
git commit -m "feat(bot): /clean command and photo cleaning handlers"
```

---

### Task 7: Единый демон `otz bot` (сессия B)

**Files:**
- Create: `src/telegram/bot_daemon.py`
- Modify: `src/telegram/subscription_bot.py` (функция `run_subscription_bot` → роутер + совместимость)
- Modify: `src/cli.py` (команда `subscription-bot` → docstring; новая команда `bot`)
- Test: `tests/test_bot_daemon.py`

**Interfaces:**
- Consumes: `build_watermark_router`, `allowed_chat_ids`, `run_exclusive` (Task 6); `sweep_forever` (Task 5); `handle_start` (существует в `subscription_bot.py`).
- Produces: `src.telegram.bot_daemon` — `build_dispatcher()` (роутеры `"watermark"` при `WATERMARK_BOT_ENABLED` и непустом списке chat_id, `"subscription"` при `SUBSCRIPTION_BOT_ENABLED`, в этом порядке), `async run_bot_daemon()` (оба флага выключены — сразу return; нет токена — `RuntimeError`; при `WATERMARK_BOT_ENABLED` и `WATERMARK_SITE_USE_CLEAN` запускает `sweep_forever(run_exclusive)` фоном). `src.telegram.subscription_bot.build_subscription_router()` (имя `"subscription"`). CLI: `otz bot`.

- [ ] **Step 1: Написать падающие тесты**

`tests/test_bot_daemon.py`:

```python
"""One poller: routers follow the flags; with every flag off the daemon does nothing."""

from __future__ import annotations

import pytest

from src.config import get_settings
from src.telegram import bot_daemon


@pytest.mark.parametrize(
    ("wm", "sub", "recipients", "expected"),
    [
        (False, False, "1", []),
        (True, False, "1,2", ["watermark"]),
        (True, False, "", []),
        (False, True, "", ["subscription"]),
        (True, True, "5", ["watermark", "subscription"]),
    ],
)
def test_dispatcher_routers_follow_flags(monkeypatch, wm, sub, recipients, expected):
    s = get_settings()
    monkeypatch.setattr(s, "watermark_bot_enabled", wm)
    monkeypatch.setattr(s, "subscription_bot_enabled", sub)
    monkeypatch.setattr(s, "tg_bot_recipients", recipients)
    monkeypatch.setattr(s, "watermark_allowed_chat_ids", "")
    dp = bot_daemon.build_dispatcher()
    assert [r.name for r in dp.sub_routers] == expected


async def test_daemon_noops_when_everything_disabled(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "watermark_bot_enabled", False)
    monkeypatch.setattr(s, "subscription_bot_enabled", False)
    monkeypatch.setattr(s, "tg_bot_token", "")
    assert await bot_daemon.run_bot_daemon() is None


async def test_daemon_requires_token_when_enabled(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "watermark_bot_enabled", True)
    monkeypatch.setattr(s, "tg_bot_token", "")
    with pytest.raises(RuntimeError):
        await bot_daemon.run_bot_daemon()
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `uv run pytest -q tests/test_bot_daemon.py`
Expected: `ImportError` для `src.telegram.bot_daemon`.

- [ ] **Step 3: Демон**

`src/telegram/bot_daemon.py`:

```python
"""Single long-polling daemon for RentFinderBot (`otz bot`, unit deploy/otz-bot.service).

A bot token can have only ONE getUpdates consumer: a second poller gets
TelegramConflictError. So every interactive bot feature lives in one Dispatcher and is
switched on by flags:
- WATERMARK_BOT_ENABLED    - /clean and incoming photos (src/telegram/wm_bot.py);
- SUBSCRIPTION_BOT_ENABLED - /start sub_<token> site subscriptions (subscription_bot.py).
With WATERMARK_BOT_ENABLED and WATERMARK_SITE_USE_CLEAN both on, the daemon also keeps clean
copies of public site photos up to date (src/watermark/published.py, every 10 minutes).
Candidate delivery (bot_delivery.py) only sends messages and does not need this daemon.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from src.config import get_settings

logger = logging.getLogger(__name__)


def build_dispatcher() -> Any:
    from aiogram import Dispatcher

    from src.telegram.subscription_bot import build_subscription_router
    from src.telegram.wm_bot import allowed_chat_ids, build_watermark_router

    settings = get_settings()
    dp = Dispatcher()
    if settings.watermark_bot_enabled:
        allowed = allowed_chat_ids()
        if not allowed:
            logger.warning("[bot] WATERMARK_BOT_ENABLED but no allowed chat ids: /clean disabled")
        else:
            dp.include_router(build_watermark_router(allowed))
    if settings.subscription_bot_enabled:
        dp.include_router(build_subscription_router())
    return dp


async def run_bot_daemon() -> None:
    """Blocks until stopped. Does nothing when both features are disabled."""
    from aiogram import Bot

    settings = get_settings()
    if not (settings.watermark_bot_enabled or settings.subscription_bot_enabled):
        logger.warning("[bot] nothing enabled: WATERMARK_BOT_ENABLED=SUBSCRIPTION_BOT_ENABLED=0")
        return
    if not settings.tg_bot_token:
        raise RuntimeError("TG_BOT_TOKEN not set - cannot run the bot daemon")
    dp = build_dispatcher()
    bot = Bot(token=settings.tg_bot_token)
    sweep: asyncio.Task[None] | None = None
    if settings.watermark_bot_enabled and settings.watermark_site_use_clean:
        from src.telegram.wm_bot import run_exclusive
        from src.watermark.published import sweep_forever

        sweep = asyncio.create_task(sweep_forever(run_exclusive))
    try:
        # Skip updates that queued up while the daemon was down: otherwise the bot would
        # suddenly start cleaning photos that were sent to it a day ago.
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("[bot] long-polling started")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        if sweep is not None:
            sweep.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sweep
        await bot.session.close()
```

- [ ] **Step 4: Подписки — роутер вместо своего поллера**

В `src/telegram/subscription_bot.py` заменить функцию целиком (от `async def run_subscription_bot` до конца файла). Было:

```python
async def run_subscription_bot() -> None:
    """Run the aiogram long-polling Dispatcher (blocks). Gated by subscription_bot_enabled.

    Imported lazily inside so the heavy aiogram Dispatcher machinery only loads when the daemon
    is actually started — the send path and the unit-tested handle_start never trigger it.
    """
    from aiogram import Bot, Dispatcher
    from aiogram.filters import CommandStart

    settings = get_settings()
    if not settings.subscription_bot_enabled:
        logger.warning("[subscription-bot] disabled (SUBSCRIPTION_BOT_ENABLED=0) — not starting")
        return
    if not settings.tg_bot_token:
        raise RuntimeError("TG_BOT_TOKEN not set — cannot run the subscription linking bot")

    bot = Bot(token=settings.tg_bot_token)
    dp = Dispatcher()

    @dp.message(CommandStart(deep_link=True))
    async def _on_start_with_payload(message):  # pragma: no cover - exercised live only
        await handle_start(message)

    @dp.message(CommandStart())
    async def _on_plain_start(message):  # pragma: no cover - exercised live only
        await handle_start(message)

    logger.info("[subscription-bot] starting long-polling for /start linking")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
```

Стало:

```python
def build_subscription_router():
    """aiogram Router with /start subscription linking (included by src/telegram/bot_daemon.py)."""
    from aiogram import Router
    from aiogram.filters import CommandStart

    router = Router(name="subscription")

    @router.message(CommandStart(deep_link=True))
    async def _on_start_with_payload(message):  # pragma: no cover - exercised live only
        await handle_start(message)

    @router.message(CommandStart())
    async def _on_plain_start(message):  # pragma: no cover - exercised live only
        await handle_start(message)

    return router


async def run_subscription_bot() -> None:
    """Compatibility for the old `otz subscription-bot` command: runs the unified daemon.

    A bot token allows only one poller, so there is no separate Dispatcher here any more:
    /start subscription linking lives in src/telegram/bot_daemon.py together with /clean.
    """
    from src.telegram.bot_daemon import run_bot_daemon

    settings = get_settings()
    if not settings.subscription_bot_enabled:
        logger.warning("[subscription-bot] disabled (SUBSCRIPTION_BOT_ENABLED=0) — not starting")
        return
    await run_bot_daemon()
```

- [ ] **Step 5: Команда `otz bot`**

В `src/cli.py` заменить блок команды `subscription-bot`. Было:

```python
@cli.command("subscription-bot")
def subscription_bot() -> None:
    """C2 — run the Telegram linking daemon for saved-search subscriptions (long-polling).

    Listens for /start sub_<token> and links the sender's chat to the subscription. Config-gated:
    when SUBSCRIPTION_BOT_ENABLED=0 (default) the command no-ops. Runs forever until interrupted.
    """
    from src.telegram.subscription_bot import run_subscription_bot

    run_cooperatively(run_subscription_bot())
```

Стало:

```python
@cli.command("subscription-bot")
def subscription_bot() -> None:
    """C2 legacy name: runs the unified `otz bot` daemon (if SUBSCRIPTION_BOT_ENABLED=1).

    A bot token allows only one poller, so /start subscription linking now lives in
    `otz bot` together with /clean. Runs forever until interrupted.
    """
    from src.telegram.subscription_bot import run_subscription_bot

    run_cooperatively(run_subscription_bot())


@cli.command("bot")
def bot() -> None:
    """Unified RentFinderBot long-polling daemon: /clean (platform marks) + /start subscriptions.

    WATERMARK_BOT_ENABLED and SUBSCRIPTION_BOT_ENABLED decide what runs; with both off the
    command exits immediately. Unit: deploy/otz-bot.service.
    """
    from src.telegram.bot_daemon import run_bot_daemon

    run_cooperatively(run_bot_daemon())
```

- [ ] **Step 6: Тесты проходят (новые и прежние тесты подписок)**

Run: `uv run pytest -q tests/test_bot_daemon.py tests/test_subscription_cli.py tests/test_subscription_telegram.py tests/test_wm_bot.py`
Expected: `25 passed`.

Run: `uv run otz --help | grep -E "^  (bot|subscription-bot) "`
Expected: обе команды в списке.

- [ ] **Step 7: Lint-гейт и commit**

```bash
uv run python scripts/check_changed_lint.py
git add src/telegram/bot_daemon.py src/telegram/subscription_bot.py src/cli.py tests/test_bot_daemon.py
git commit -m "feat(bot): one long-polling daemon for /clean and subscription linking"
```

---

### Task 8: Юнит `otz-bot.service` и релиз (сессия B)

**Files:**
- Create: `deploy/otz-bot.service`
- Modify: `scripts/release.py` (`SERVICES`), `scripts/vps_install.sh` (блок юнита поллера), `docs/operations/releases.md` (одна строка)
- Test: `tests/test_bot_unit.py`

**Interfaces:**
- Consumes: `release.effective_units(deploy)`, `release.SERVICES`, `release.ROOT` (существуют).
- Produces: юнит `otz-bot` (`ExecStart=/opt/otz/.venv/bin/python -m src.cli bot`, `User=otz`, `Type=simple`, `WorkingDirectory=/opt/otz`), который релиз копирует, проверяет и останавливает/поднимает вместе с остальными сервисами.

- [ ] **Step 1: Написать падающий тест**

`tests/test_bot_unit.py`:

```python
"""deploy/otz-bot.service satisfies the release activation checks (scripts/release.py)."""

from __future__ import annotations

from scripts import release


def test_bot_unit_passes_the_release_effective_unit_check(tmp_path, monkeypatch):
    source = (release.ROOT / "deploy" / "otz-bot.service").read_text()
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    (deploy / "otz-bot.service").write_text(source)
    configured = dict(
        line.split("=", 1)
        for line in source.splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    )
    assert configured["ExecStart"] == "/opt/otz/.venv/bin/python -m src.cli bot"
    effective = {
        "ExecStart": f"{{ argv[]={configured['ExecStart']} ; ignore_errors=no ; }}",
        "ExecStartPre": "",
        "User": "otz",
        "Type": "simple",
        "WorkingDirectory": "/opt/otz",
        "TimeoutStartUSec": "1min 30s",
        "DropInPaths": "",
    }
    monkeypatch.setattr(
        release, "run", lambda args, **_kw: effective[args[3].removeprefix("--property=")]
    )
    assert release.effective_units(deploy)["otz-bot.service"]["User"] == "otz"
    assert "otz-bot" in release.SERVICES
```

- [ ] **Step 2: Убедиться, что тест падает**

Run: `uv run pytest -q tests/test_bot_unit.py`
Expected: `FileNotFoundError: … deploy/otz-bot.service`.

- [ ] **Step 3: Юнит**

`deploy/otz-bot.service`:

```ini
# otz-bot.service — unified RentFinderBot long-polling daemon (`otz bot`):
#   /clean + incoming photos: remove listing-platform watermarks (WATERMARK_BOT_ENABLED=1),
#   /start sub_<token>: site subscription linking (SUBSCRIPTION_BOT_ENABLED=1).
# A bot token allows only ONE poller: never run it together with otz-subscription-bot.
# Enable once, after the release and the flags in /opt/otz/.env:
#   systemctl disable --now otz-subscription-bot 2>/dev/null || true
#   systemctl enable --now otz-bot
# With both flags off the command exits immediately (exit 0, unit becomes inactive).

[Unit]
Description=otz-rent-finder RentFinderBot daemon (watermark /clean + subscription linking)
After=network-online.target
Wants=network-online.target
OnFailure=otz-notify-failure@%n.service
StartLimitIntervalSec=600
StartLimitBurst=5

[Service]
Type=simple
User=otz
Group=otz
WorkingDirectory=/opt/otz
EnvironmentFile=/opt/otz/.env
Environment=PATH=/opt/otz/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ExecStart=/opt/otz/.venv/bin/python -m src.cli bot
Restart=on-failure
RestartSec=30
# Photo cleaning (OpenCV + LaMa, ~1.1 GB peak) must not starve the monitor or the site.
Nice=10
MemoryMax=2G
StandardOutput=append:/var/log/otz/bot.log
StandardError=append:/var/log/otz/bot.log

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 4: Релиз и установщик**

В `scripts/release.py` строку

```python
SERVICES = ("otz-monitor", "otz-site", "otz-admin", "otz-subscription-bot")
```

заменить на

```python
SERVICES = ("otz-monitor", "otz-site", "otz-admin", "otz-subscription-bot", "otz-bot")
```

(`otz-subscription-bot` остаётся: если он где-то активен, релиз всё равно его остановит и поднимет.)

В `scripts/vps_install.sh` заменить блок юнита поллера (от комментария `# ─── C2: saved-search subscription LINKING daemon` до строки перед `# Triggered via OnFailure=…`). Было:

```bash
# ─── C2: saved-search subscription LINKING daemon (OPTIONAL, OFF by default) ───
# A NEW long-running aiogram poller that handles /start sub_<token> to link a Telegram chat to a
# saved-search subscription. It is OPTIONAL: enabling it is a deploy choice (it needs TG_BOT_TOKEN
# + SITE_TELEGRAM_BOT_USERNAME set, and SUBSCRIPTION_BOT_ENABLED=1 in .env — without that the
# command no-ops). The unit is written but NOT enabled here; enable it manually only when running
# the Telegram subscription channel. e-mail subscriptions need NONE of this.
cat >/etc/systemd/system/otz-subscription-bot.service <<UNIT
[Unit]
Description=otz-rent-finder — Telegram saved-search subscription linking daemon (C2, optional)
After=network-online.target
Wants=network-online.target
OnFailure=otz-notify-failure@%n.service

[Service]
Type=simple
User=$OTZ_USER
WorkingDirectory=$OTZ_HOME
ExecStart=$OTZ_HOME/.venv/bin/python -m src.cli subscription-bot
Restart=on-failure
RestartSec=30
StartLimitBurst=5
StartLimitIntervalSec=600
StandardOutput=append:/var/log/otz/subscription-bot.log
StandardError=append:/var/log/otz/subscription-bot.log

[Install]
WantedBy=multi-user.target
UNIT
# NOTE: intentionally NOT enabled. To turn on the Telegram subscription channel:
#   1) set SUBSCRIPTION_BOT_ENABLED=1, TG_BOT_TOKEN=…, SITE_TELEGRAM_BOT_USERNAME=<botname> in .env
#   2) systemctl enable --now otz-subscription-bot
```

Стало:

```bash
# ─── RentFinderBot interactive daemon (OPTIONAL, OFF by default) ───
# ONE long-running aiogram poller (a bot token allows only one) for both interactive features:
#   /clean + incoming photos → remove listing-platform watermarks (WATERMARK_BOT_ENABLED=1);
#   /start sub_<token> → link a Telegram chat to a saved-search subscription
#   (SUBSCRIPTION_BOT_ENABLED=1, needs SITE_TELEGRAM_BOT_USERNAME too).
# It replaces the old otz-subscription-bot unit; with both flags off the command no-ops.
# The canonical unit is deploy/otz-bot.service (release.py installs it); written here for
# fresh hosts but NOT enabled. e-mail subscriptions need NONE of this.
cat >/etc/systemd/system/otz-bot.service <<UNIT
[Unit]
Description=otz-rent-finder RentFinderBot daemon (watermark /clean + subscription linking)
After=network-online.target
Wants=network-online.target
OnFailure=otz-notify-failure@%n.service
StartLimitIntervalSec=600
StartLimitBurst=5

[Service]
Type=simple
User=$OTZ_USER
Group=$OTZ_USER
WorkingDirectory=$OTZ_HOME
EnvironmentFile=$OTZ_HOME/.env
Environment=PATH=$OTZ_HOME/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ExecStart=$OTZ_HOME/.venv/bin/python -m src.cli bot
Restart=on-failure
RestartSec=30
Nice=10
MemoryMax=2G
StandardOutput=append:/var/log/otz/bot.log
StandardError=append:/var/log/otz/bot.log

[Install]
WantedBy=multi-user.target
UNIT
# NOTE: intentionally NOT enabled. To turn it on:
#   1) set WATERMARK_BOT_ENABLED=1 and/or SUBSCRIPTION_BOT_ENABLED=1 (+ TG_BOT_TOKEN) in .env
#   2) systemctl disable --now otz-subscription-bot 2>/dev/null || true
#   3) systemctl enable --now otz-bot
```

В `docs/operations/releases.md` фразу `monitor/site/admin/` + перенос + `subscription-bot services together.` заменить на `monitor/site/admin/` + перенос + `subscription-bot/bot services together.` (пункт 3 раздела «Activation behavior»).

- [ ] **Step 5: Тесты и проверки проходят**

```bash
uv run pytest -q tests/test_bot_unit.py tests/test_release.py tests/test_release_data_only.py tests/test_release_first_transition.py
for s in scripts/*.sh; do bash -n "$s"; done && echo shell-ok
```

Expected: `85 passed`; `shell-ok`.

- [ ] **Step 6: Lint-гейт и commit**

```bash
uv run python scripts/check_changed_lint.py
git add deploy/otz-bot.service scripts/release.py scripts/vps_install.sh docs/operations/releases.md tests/test_bot_unit.py
git commit -m "feat(deploy): otz-bot.service replaces the subscription poller unit"
```

---

### Task 9: Сайт показывает чистые копии (сессия C)

**Files:**
- Modify: `src/site/photos.py` (источник пикселей для превью), `src/site/media.py` (оригинал по `/photos/<rel>`), `src/site/og_image.py` (картинка для превью ссылок)
- Test: `tests/site/test_watermark_site.py`

**Interfaces:**
- Consumes: `site_source(original) -> Path` (Task 4), `clean_batch` (Task 5), команда `wm-revert` (Task 5).
- Produces: при `WATERMARK_SITE_USE_CLEAN=1` превью `card`/`detail`, оригинал `/photos/<rel>` и OG-картинка берутся из чистой копии, если она есть; превью, которое старше чистой копии, пересобирается. При выключенном флаге — всё как раньше.

- [ ] **Step 1: Написать падающие тесты**

`tests/site/test_watermark_site.py`:

```python
"""The site shows watermark-free photos with WATERMARK_SITE_USE_CLEAN=1 (and only then)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from click.testing import CliRunner
from PIL import Image

import src.site.photos as photos
import src.watermark.cli as wm_cli
from src.cli import cli
from src.config import get_settings
from src.site.media import _permitted_file
from src.site.og_image import _photo_file
from src.watermark.pipeline import decode_image, find_marks
from src.watermark.published import clean_batch
from src.watermark.store import ensure_clean
from src.watermark.templates import load_templates
from tests.watermark.synth import apply_mark, make_background


@pytest.fixture
def root(tmp_path: Path, monkeypatch) -> Path:
    inbound = tmp_path / "inbound"
    inbound.mkdir()
    s = get_settings()
    monkeypatch.setattr(s, "inbound_photos_dir", inbound)
    monkeypatch.setattr(s, "site_photos_root", inbound)
    monkeypatch.setattr(s, "watermark_clean_dir", tmp_path / "clean")
    monkeypatch.setattr(s, "watermark_lama_enabled", False)
    monkeypatch.setattr(s, "watermark_site_use_clean", False)
    photos.invalidate_manifest_cache()
    return inbound


def _marked_photo(root: Path) -> Path:
    path = root / "1" / "9" / "00.jpg"
    path.parent.mkdir(parents=True, exist_ok=True)
    img = apply_mark(make_background(1440, 1080, seed=8), {t.id: t for t in load_templates()}["zida_logo"])
    Image.fromarray(img).save(path, "JPEG", quality=92)
    return path


def _detail_has_marks(root: Path, rel: str) -> bool:
    detail = decode_image((root / f"{rel}.detail.webp").read_bytes())
    return bool(find_marks(detail))


def test_variants_come_from_clean_copy_only_with_flag(root, monkeypatch):
    orig = _marked_photo(root)
    rel = orig.relative_to(root).as_posix()
    ensure_clean(str(orig))
    photos.process_photo(orig, rel, root)
    assert _detail_has_marks(root, rel)  # flag off -> variants come from the original
    monkeypatch.setattr(get_settings(), "watermark_site_use_clean", True)
    clean = Path(get_settings().watermark_clean_dir) / rel
    later = (root / f"{rel}.detail.webp").stat().st_mtime_ns + 10_000_000
    os.utime(clean, ns=(later, later))  # clean copy is newer than the old variants
    _entry, generated, _skipped = photos.process_photo(orig, rel, root)
    assert generated == 2
    assert not _detail_has_marks(root, rel)
    _entry, generated, skipped = photos.process_photo(orig, rel, root)
    assert (generated, skipped) == (0, 2)  # idempotent again afterwards


def test_original_url_serves_clean_copy_with_flag(root, monkeypatch):
    orig = _marked_photo(root)
    rel = orig.relative_to(root).as_posix()
    ensure_clean(str(orig))
    assert _permitted_file(rel, [str(orig)]) == orig
    monkeypatch.setattr(get_settings(), "watermark_site_use_clean", True)
    served = _permitted_file(rel, [str(orig)])
    assert served is not None and served != orig and not find_marks(decode_image(served.read_bytes()))
    assert _photo_file([f"/photos/{rel}"]) == served
    assert _permitted_file("1/9/other.jpg", [str(orig)]) is None


def test_without_clean_copy_everything_stays_original(root, monkeypatch):
    orig = _marked_photo(root)
    rel = orig.relative_to(root).as_posix()
    monkeypatch.setattr(get_settings(), "watermark_site_use_clean", True)
    assert _permitted_file(rel, [str(orig)]) == orig


def test_cleaned_then_reverted_apartment_on_the_site(root, monkeypatch):
    monkeypatch.setattr(get_settings(), "watermark_site_use_clean", True)
    orig = _marked_photo(root)
    rel = orig.relative_to(root).as_posix()
    assert clean_batch([str(orig)]) == (1, 0)  # what the bot's site sweep does
    photos.generate_for_stored_paths([str(orig)])
    assert not _detail_has_marks(root, rel)

    async def fake_paths(_match_id: int) -> list[str]:
        return [str(orig)]

    monkeypatch.setattr(wm_cli, "_match_photo_paths", fake_paths)
    result = CliRunner().invoke(cli, ["wm-revert", "5"])
    assert result.exit_code == 0, result.output
    assert _detail_has_marks(root, rel)  # card/detail rebuilt from the original
    assert _permitted_file(rel, [str(orig)]) == orig
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `uv run pytest -q tests/site/test_watermark_site.py`
Expected: падают `test_variants_come_from_clean_copy_only_with_flag`, `test_original_url_serves_clean_copy_with_flag`, `test_cleaned_then_reverted_apartment_on_the_site` (сайт пока берёт оригиналы); `test_without_clean_copy_everything_stays_original` проходит.

- [ ] **Step 3: Превью из чистой копии**

В `src/site/photos.py` перед строкой `def process_photo(` вставить:

```python
def _variant_source(orig: Path) -> Path:
    """Pixels for the variants: the watermark-free copy (WATERMARK_SITE_USE_CLEAN=1) or orig."""
    from src.watermark.store import site_source

    return site_source(orig)


def _stale(target: Path, source: Path) -> bool:
    try:
        return target.stat().st_mtime_ns < source.stat().st_mtime_ns
    except OSError:
        return True
```

В теле `process_photo` заменить

```python
    generated = skipped = 0
    try:
        with Image.open(orig) as im:
```

на

```python
    generated = skipped = 0
    # Watermark-free source: a variant older than the clean copy is rebuilt from it.
    source = _variant_source(orig)
    from_clean = source != orig
    try:
        with Image.open(source) as im:
```

и строку

```python
                if target.exists():
```

на

```python
                if target.exists() and not (from_clean and _stale(target, source)):
```

- [ ] **Step 4: Оригинал и OG-картинка**

В `src/site/media.py` (функция `_permitted_file`) заменить

```python
        if path == rel or path in {f"{rel}.{kind}.webp" for kind in VARIANT_WIDTHS}:
            return target
```

на

```python
        if path == rel:
            from src.watermark.store import site_source

            return site_source(target)
        if path in {f"{rel}.{kind}.webp" for kind in VARIANT_WIDTHS}:
            return target
```

В `src/site/og_image.py` (функция `_photo_file`) заменить

```python
        if candidate.is_file():
            return candidate
```

на

```python
        if candidate.is_file():
            from src.watermark.store import site_source

            return site_source(candidate)
```

- [ ] **Step 5: Тесты проходят (новые и весь сайт)**

Run: `uv run pytest -q tests/site tests/test_auto_publish.py tests/test_admin_publish.py`
Expected: `608 passed`, 1 skipped.

- [ ] **Step 6: Lint-гейт и commit**

```bash
uv run python scripts/check_changed_lint.py
git add src/site/photos.py src/site/media.py src/site/og_image.py tests/site/test_watermark_site.py
git commit -m "feat(site): serve watermark-free copies when WATERMARK_SITE_USE_CLEAN is on"
```

---

### Task 10: Кандидаты Дарье без знаков (сессия C)

**Files:**
- Modify: `src/telegram/bot_delivery.py`
- Test: `tests/test_bot_delivery_clean.py`

**Interfaces:**
- Consumes: `ensure_clean`, `StoredResult.best` (Task 4); `BOT_PHOTO_LIMIT_BYTES`, `send_match_via_bot`, `_make_bot` (существуют в `bot_delivery.py`).
- Produces: при `WATERMARK_CLEAN_ON_DELIVERY=1` первая отправка кандидата идёт с чистыми копиями, замороженный payload запоминает именно их; ошибка чистки или чистая копия больше лимита Bot API 10 МБ — уходят оригиналы.

- [ ] **Step 1: Написать падающие тесты**

`tests/test_bot_delivery_clean.py`:

```python
"""WATERMARK_CLEAN_ON_DELIVERY: the first album goes out clean; on failure, the originals."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image
from sqlalchemy import select

from src.config import get_settings
from src.db import get_session
from src.db.models import InboundPost, Listing, Match, MatchDelivery, Platform, SourceChannel
from src.telegram import bot_delivery
from src.watermark import store
from src.watermark.templates import load_templates
from tests.watermark.synth import apply_mark, make_background


class AlbumBot:
    def __init__(self):
        self.albums: list[list[str]] = []
        self.session = SimpleNamespace(close=AsyncMock())

    async def send_media_group(self, chat_id, media):
        self.albums.append([m.media.path for m in media])
        return [SimpleNamespace(message_id=700 + i) for i in range(len(media))]

    async def send_photo(self, chat_id, photo, caption=None):
        self.albums.append([photo.path])
        return SimpleNamespace(message_id=700)


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    s = get_settings()
    inbound = tmp_path / "inbound"
    monkeypatch.setattr(s, "inbound_photos_dir", inbound)
    monkeypatch.setattr(s, "watermark_clean_dir", tmp_path / "clean")
    monkeypatch.setattr(s, "watermark_lama_enabled", False)
    monkeypatch.setattr(s, "tg_bot_recipients", "555")
    monkeypatch.setattr(s, "auto_publish_on_send", False)
    bot = AlbumBot()
    monkeypatch.setattr(bot_delivery, "_make_bot", lambda: bot)
    return inbound, bot


async def _seed(inbound: Path) -> tuple[int, list[str]]:
    tpl = {t.id: t for t in load_templates()}["zida_logo"]
    paths = []
    for i in range(2):
        p = inbound / "1" / "88" / f"{i:02d}.jpg"
        p.parent.mkdir(parents=True, exist_ok=True)
        Image.fromarray(apply_mark(make_background(1440, 1080, seed=20 + i), tpl)).save(p, "JPEG", quality=92)
        paths.append(str(p))
    async with get_session() as s:
        s.add(SourceChannel(id=1, tg_id=-100, username="src"))
        listing = Listing(platform=Platform.CETIRI_ZIDA, external_id="z9", url="https://www.4zida.rs/y")
        inbound_post = InboundPost(source_channel_id=1, tg_message_id=88, raw_text="500€ 50m²", photo_paths=paths)
        s.add_all([listing, inbound_post])
        await s.flush()
        match = Match(listing_id=listing.id, inbound_post_id=inbound_post.id, confidence=1.0,
                      matched_photo_count=2, method="phash")
        s.add(match)
        await s.flush()
        return match.id, paths


async def _frozen_paths(match_id: int) -> list[str]:
    async with get_session() as s:
        payload = (await s.execute(select(MatchDelivery.payload).where(MatchDelivery.match_id == match_id))).scalar_one()
    return payload["photo_paths"]


async def test_flag_off_sends_originals(db, env):
    inbound, bot = env
    match_id, paths = await _seed(inbound)
    await bot_delivery.send_match_via_bot(match_id)
    assert bot.albums == [paths]


def _all_under(paths: list[str], root: Path) -> bool:
    return all(Path(p).resolve().is_relative_to(root.resolve()) for p in paths)


def _all_exist(paths: list[str]) -> bool:
    return all(Path(p).is_file() for p in paths)


async def test_flag_on_sends_clean_copies(db, env, monkeypatch):
    inbound, bot = env
    monkeypatch.setattr(get_settings(), "watermark_clean_on_delivery", True)
    match_id, paths = await _seed(inbound)
    await bot_delivery.send_match_via_bot(match_id)
    assert _all_under(bot.albums[0], Path(get_settings().watermark_clean_dir))
    assert await _frozen_paths(match_id) == bot.albums[0]
    assert _all_exist(paths)  # originals are still there


async def test_cleaning_failure_falls_back_to_originals(db, env, monkeypatch):
    inbound, bot = env
    monkeypatch.setattr(get_settings(), "watermark_clean_on_delivery", True)

    def boom(_stored, **_kw):
        raise RuntimeError("cv2 exploded")

    monkeypatch.setattr(store, "ensure_clean", boom)
    match_id, paths = await _seed(inbound)
    await bot_delivery.send_match_via_bot(match_id)
    assert bot.albums == [paths]


def _sizes(paths: list[str]) -> list[int]:
    return [Path(p).stat().st_size for p in paths]


async def test_clean_copy_over_the_bot_limit_falls_back_to_the_original(db, env, monkeypatch):
    inbound, bot = env
    monkeypatch.setattr(get_settings(), "watermark_clean_on_delivery", True)
    match_id, paths = await _seed(inbound)
    clean = [str(store.ensure_clean(p).best) for p in paths]  # type: ignore[union-attr]
    limit = min(_sizes(clean)) - 1
    assert max(_sizes(paths)) <= limit  # q95 clean copies are larger than the q92 originals
    monkeypatch.setattr(bot_delivery, "BOT_PHOTO_LIMIT_BYTES", limit)
    await bot_delivery.send_match_via_bot(match_id)
    assert bot.albums == [paths]
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `uv run pytest -q tests/test_bot_delivery_clean.py`
Expected: `test_flag_on_sends_clean_copies` падает (уходят оригиналы); остальные три проходят.

- [ ] **Step 3: Реализация**

В `src/telegram/bot_delivery.py` перед строкой `async def send_match_via_bot(` вставить:

```python
async def _clean_delivery_paths(match_id: int, photo_paths: list[Path]) -> list[Path]:
    """Swap in watermark-free copies (WATERMARK_CLEAN_ON_DELIVERY); originals on any problem."""
    import asyncio

    from src.watermark.store import ensure_clean

    def _clean_all() -> list[Path]:
        out: list[Path] = []
        for path in photo_paths:
            result = ensure_clean(str(path))
            best = result.best if result is not None else path
            # A re-encoded copy must not break the Bot API 10 MB limit the original passed.
            if best != path and best.stat().st_size > BOT_PHOTO_LIMIT_BYTES:
                best = path
            out.append(best)
        return out

    try:
        return await asyncio.to_thread(_clean_all)
    except Exception:
        logger.warning(
            "[bot_delivery] match=%d watermark cleaning failed — sending originals",
            match_id, exc_info=True,
        )
        return photo_paths
```

В `send_match_via_bot` заменить

```python
        if any(not p.is_file() for p in photo_paths):
            raise BotDeliveryError(f"Match {match_id}: missing inbound photo files")

    rendered_text = (
```

на

```python
        if any(not p.is_file() for p in photo_paths):
            raise BotDeliveryError(f"Match {match_id}: missing inbound photo files")

    if saved_payload is None and get_settings().watermark_clean_on_delivery:
        # First send only: the frozen payload then records the clean copies, so retries and
        # /clean see the same album. Any cleaning failure sends the originals instead.
        photo_paths = await _clean_delivery_paths(match_id, photo_paths)

    rendered_text = (
```

(`get_settings` и `logger` в `bot_delivery.py` уже есть — проверить `grep -nE "^from src.config import|^logger" src/telegram/bot_delivery.py`.)

- [ ] **Step 4: Тесты проходят (новые и прежние тесты доставки)**

Run: `uv run pytest -q tests/test_bot_delivery_clean.py tests/test_bot_media_contract.py tests/test_bot_photo_preflight.py tests/test_delivery_guard.py tests/test_recipient_outbox.py`
Expected: `34 passed`.

- [ ] **Step 5: Lint-гейт и commit**

```bash
uv run python scripts/check_changed_lint.py
git add src/telegram/bot_delivery.py tests/test_bot_delivery_clean.py
git commit -m "feat(delivery): optionally send candidates without platform watermarks"
```

---

### Task 11: Интеграция, полный прогон, проверка на реальных фото, runbook (сессия D)

**Files:**
- Merge: `feat/wm-bot`, `feat/wm-site` → `feat/watermark-remover`
- Create: `docs/operations/watermark-remover.md`, `docs/reviews/2026-10-14-watermark-qa.md`

**Interfaces:**
- Consumes: всё из Task 1–10; образцы `~/Projects/BelgradAppartment/data/wm-samples` (36 фото + `expected.csv`); `data/wm-baseline-tests.txt (в worktree)` (Task 0).
- Produces: ветка со всеми задачами, зелёные тесты, отчёт QA, runbook для включения.

- [ ] **Step 1: Слить ветки сессий B и C**

```bash
cd ~/Projects/BelgradAppartment-wm
git status --short && git branch --show-current
git merge --no-ff feat/wm-bot -m "Merge branch 'feat/wm-bot': watermark bot and daemon"
git merge --no-ff feat/wm-site -m "Merge branch 'feat/wm-site': watermark site and delivery"
```

Expected: чистое дерево, ветка `feat/watermark-remover`, оба слияния без конфликтов (ветки меняли разные файлы). Конфликт — остановиться и описать.

- [ ] **Step 2: Полный прогон тестов и проверок**

```bash
uv sync --extra clip
uv run pytest -q -p no:cacheprovider --ignore=tests/e2e > data/wm-full-tests.txt 2>&1; tail -n 3 data/wm-full-tests.txt
comm -13 <(grep -oE "^(FAILED|ERROR) [^ ]+" data/wm-baseline-tests.txt | sort -u) <(grep -oE "^(FAILED|ERROR) [^ ]+" data/wm-full-tests.txt | sort -u)
uv run python scripts/check_changed_lint.py
for s in scripts/*.sh; do bash -n "$s"; done && echo shell-ok
```

Expected: `comm` ничего не печатает (новых падений нет), `New lint diagnostics: 0`, `shell-ok`. Новые падения — найти причину в изменениях ветки и исправить с тестом; не получается — остановиться и описать.

- [ ] **Step 3: Модель LaMa и её тесты**

```bash
uv run otz wm-fetch-model
uv run pytest -q tests/watermark/test_lama.py
```

Expected: `LaMa: …/data/models/big-lama.pt (205669692 bytes, sha256 344c77bbcb15...)`; `7 passed`.

- [ ] **Step 4: Прогон по реальным фото**

```bash
rm -rf /tmp/wm-qa
uv run otz wm-qa ~/Projects/BelgradAppartment/data/wm-samples --out-dir /tmp/wm-qa --expect ~/Projects/BelgradAppartment/data/wm-samples/expected.csv
grep -c "inpaint:lama" /tmp/wm-qa/report.csv
ls /tmp/wm-qa/*.marks.jpg | wc -l
```

Expected: в конце `=== 36 images, marks in 36, report: /tmp/wm-qa/report.csv ===` и `QA expectations: all matched`; `24` (18 знаков 4zida + 6 логотипов Halo через LaMa); `36`.

- [ ] **Step 5: Посмотреть каждую картинку глазами**

Открыть инструментом Read все 36 файлов `/tmp/wm-qa/*.marks.jpg`. В каждой строке слева — участок со знаком до, справа — после. Критерии:

1. справа знака не видно;
2. справа нет пятен, размазанных участков, оборванных прямых линий (края мебели, рамы, балки, плинтусы);
3. нет светлого или тёмного контура в форме логотипа.

При подготовке плана все 36 выглядели чисто; допустимый максимум — едва заметный контур букв CityExpert на ровной стене при большом увеличении. Записать результат в `docs/reviews/2026-10-14-watermark-qa.md`: дата, команда, итог `report.csv` (сколько знаков какого типа, время), таблица `файл | ok / minor / bad | что видно`. Сами фото в git не добавлять. Параметры алгоритма не менять: если есть `bad` — перечислить в отчёте и в итоговом сообщении.

- [ ] **Step 6: Runbook**

`docs/operations/watermark-remover.md`:

````markdown
# Водяные знаки площадок: команда `/clean` в RentFinderBot — эксплуатация

Справочник для включения, проверки и отката. Как это устроено и почему именно так — в плане
`docs/superpowers/plans/2026-10-14-watermark-remover.md`.

## Что умеет

В личке RentFinderBot (только для chat_id из `TG_BOT_RECIPIENTS` и `WATERMARK_ALLOWED_CHAT_IDS`):

- прислать или переслать фото (альбомом до 10 штук или файлами) — бот вернёт их без знаков
  Halo Oglasi, 4zida и CityExpert (файлы возвращаются файлами, без пережатия Telegram);
- ответить `/clean` на альбом кандидата, который прислал бот, — придут фото этого кандидата
  без знаков в полном разрешении;
- `/clean 123` — то же для кандидата №123; `/clean` без ответа — подсказка.

Не трогаются: знаки агентств внутри фото (BEOSTAN и т. п.); у Nekretnine.rs своих знаков нет.

Оригиналы никогда не меняются. Чистые копии лежат в `data/clean/<тот же путь, что в
data/inbound>` и рядом `<имя>.wm.json` (версия алгоритма, sha256 оригинала, что найдено).
`data/clean` не входит в бэкапы: это производные данные, команды ниже создают их заново.

## Из чего состоит

| Что | Где |
|---|---|
| Демон бота (один поллер на токен: `/clean` + `/start` подписок) | `otz-bot.service` → `python -m src.cli bot`, лог `/var/log/otz/bot.log` |
| Старый демон подписок | `otz-subscription-bot.service` — **не запускать вместе с otz-bot** |
| Модель LaMa (для 4zida и большого логотипа Halo) | `data/models/big-lama.pt`, 206 МБ, `otz wm-fetch-model`; torch уже стоит (extra `clip`) |
| Без модели/torch | работает дальше на OpenCV: Halo — Telea, 4zida — «вычитание» знака (заметно хуже) |

Флаги в `/opt/otz/.env` (все по умолчанию выключены):

| Флаг | Что включает | Кого перезапустить |
|---|---|---|
| `WATERMARK_BOT_ENABLED=true` | `/clean` и приём фото в боте | `otz-bot` |
| `WATERMARK_ALLOWED_CHAT_IDS=` | ещё chat_id (через запятую) кроме `TG_BOT_RECIPIENTS` | `otz-bot` |
| `WATERMARK_CLEAN_ON_DELIVERY=true` | кандидаты приходят Дарье уже без знаков | `otz-monitor` |
| `WATERMARK_SITE_USE_CLEAN=true` | сайт показывает чистые копии; бот раз в 10 минут дочищает новые публикации | `otz-site`, `otz-admin`, `otz-monitor`, `otz-bot` |
| `WATERMARK_LAMA_ENABLED=false` | аварийно выключить нейросеть (останется OpenCV) | все, где включена чистка |

## Команды

На сервере всё запускается от пользователя `otz`:

```bash
sudo -u otz bash -c 'cd /opt/otz && .venv/bin/python -m src.cli <команда>'
```

| Команда | Что делает |
|---|---|
| `wm-fetch-model` | скачать LaMa и сверить sha256 (повторный запуск ничего не качает) |
| `wm-qa <папка> --out-dir <папка> [--expect file.csv]` | прогон по папке без записи в data: `*.pair.jpg` (до/после), `*.marks.jpg` (крупно каждый знак), `report.csv` |
| `wm-clean <файлы> [--out-dir]` | почистить произвольные файлы → `<имя>.clean.jpg` |
| `wm-clean-match <id> [--force]` | чистые копии фото кандидата в `data/clean` |
| `wm-clean-published [--dry-run] [--limit N] [--force]` | чистые копии фото всех публичных карточек + пересборка картинок сайта (при `WATERMARK_SITE_USE_CLEAN=true`) |
| `wm-revert <id>` | вернуть на сайт оригиналы кандидата, если чистка вышла плохой (решение запоминается) |

## Включение по этапам

### Этап 0 — после релиза (флаги выключены)

Ничего для пользователей не меняется. Подготовка и проверка на свежих фото с сервера:

```bash
sudo -u otz bash -c 'cd /opt/otz && .venv/bin/python -m src.cli wm-fetch-model'
sudo -u otz bash -c 'cd /opt/otz && rm -rf /tmp/wm-qa-in /tmp/wm-qa-out && mkdir -p /tmp/wm-qa-in \
  && find data/inbound -name "*.jpg" -mtime -7 | head -60 | xargs -r cp --parents -t /tmp/wm-qa-in \
  && .venv/bin/python -m src.cli wm-qa /tmp/wm-qa-in --out-dir /tmp/wm-qa-out'
```

Скачать `/tmp/wm-qa-out/*.marks.jpg` к себе (`scp -r <сервер>:/tmp/wm-qa-out .`) и посмотреть:
знак исчез, на его месте нет пятен и обрывов линий; на фото без знаков `marks.jpg` нет.
После просмотра: `sudo rm -rf /tmp/wm-qa-in /tmp/wm-qa-out`.

### Этап 1 — команда в боте

1. В `/opt/otz/.env`: `WATERMARK_BOT_ENABLED=true`.
2. Один поллер на токен:
   ```bash
   sudo systemctl disable --now otz-subscription-bot 2>/dev/null || true
   sudo systemctl enable --now otz-bot
   ```
3. Проверка: `systemctl is-active otz-bot` → `active`; в `/var/log/otz/bot.log` есть
   `long-polling started` и нет `TelegramConflictError`. В личке бота `/clean` → подсказка;
   ответ `/clean` на последний альбом кандидата → альбом без знаков с подписью «🧽 Готово…».

`SUBSCRIPTION_BOT_ENABLED=true` (если когда-то включат подписки в Telegram) обслуживает тот же
`otz-bot`.

### Этап 2 — кандидаты сразу без знаков

1. `WATERMARK_CLEAN_ON_DELIVERY=true`, затем в тихое окно `sudo systemctl restart otz-monitor`.
2. Цена: чистка идёт внутри монитора — до ~1 ГБ памяти на LaMa и +5–30 с на альбом.
   После первых кандидатов: `systemctl status otz-monitor --no-pager | grep -i memory`.
3. Любой сбой чистки — уходят оригиналы (доставка важнее). Откат: флаг `false` + рестарт монитора.

### Этап 3 — сайт без знаков

1. `WATERMARK_SITE_USE_CLEAN=true`, затем
   `sudo systemctl restart otz-site otz-admin otz-monitor otz-bot`.
2. Разово: `wm-clean-published --dry-run` (сколько фото), потом `wm-clean-published`.
   Дальше новые публикации бот дочищает сам раз в 10 минут (нужен этап 1).
3. Cloudflare: Caddy отдаёт `/photos/*` с `no-store`, но если в Cache Rules для `/photos/*`
   включён «Cache Everything» с Edge TTL, сделать Purge by prefix `rentbelgrad.com/photos/`,
   иначе до недели будут видны старые картинки.
4. Плохая чистка у конкретной квартиры: `wm-revert <match_id>`.
5. Полный откат: флаг `false` + рестарт тех же сервисов. Уже пересобранные превью остаются
   чистыми (это безвредно); для конкретных квартир — `wm-revert`.

## Диагностика

- Бот не отвечает: `systemctl status otz-bot`, `tail -n 100 /var/log/otz/bot.log`.
  `TelegramConflictError` — запущен второй поллер (старый `otz-subscription-bot`, локальный dev).
- `LaMa unavailable — falling back to Telea` в логе: нет модели или torch → `wm-fetch-model`.
- Нехватка памяти: у юнита `MemoryMax=2G`; systemd перезапустит демон, придёт алерт OnFailure.
- Чистые копии можно пересоздать в любой момент: `wm-clean-match <id> --force`,
  `wm-clean-published --force`.
````

- [ ] **Step 7: Commit и уборка**

```bash
git add docs/operations/watermark-remover.md docs/reviews/2026-10-14-watermark-qa.md
git commit -m "docs: watermark remover runbook and QA report"
git worktree remove ../BelgradAppartment-wm-bot
git worktree remove ../BelgradAppartment-wm-site
```

(Ветки `feat/wm-bot` и `feat/wm-site` остаются; удаляются только их рабочие каталоги.)

---

### Task 12: Push и draft PR (сессия D)

**Files:** нет изменений в коде.

**Interfaces:**
- Produces: ветка `feat/watermark-remover` на GitHub, draft PR в `main`, зелёный CI.

- [ ] **Step 1: Текст PR**

Записать в `/tmp/wm-pr.md`:

```markdown
## Водяные знаки площадок: `/clean` в RentFinderBot

- Новый пакет `src/watermark/`: находит и убирает знаки Halo Oglasi (угол и логотип), 4zida (два логотипа) и CityExpert (логотип по центру). Оригиналы не меняются; чистые копии — `data/clean/` с отчётом `.wm.json`.
- Бот: прислать фото → вернёт без знаков; `/clean` ответом на альбом кандидата или `/clean <id>` → фото кандидата без знаков.
- Один поллер на токен: новый демон `otz bot` (`deploy/otz-bot.service`) обслуживает и `/clean`, и `/start` подписок; заменяет `otz-subscription-bot`.
- Всё выключено флагами по умолчанию: `WATERMARK_BOT_ENABLED`, `WATERMARK_CLEAN_ON_DELIVERY`, `WATERMARK_SITE_USE_CLEAN`.
- Новая зависимость: `opencv-python-headless==4.14.0.94`. LaMa (torch из extra `clip`) — модель скачивается `otz wm-fetch-model`.

Проверки: полный pytest без новых падений, lint-гейт 0, QA на 36 реальных фото — `docs/reviews/2026-10-14-watermark-qa.md`.
Включение и откат: `docs/operations/watermark-remover.md`. План: `docs/superpowers/plans/2026-10-14-watermark-remover.md`.
Не мержить до решения Дмитрия о выпуске.
```

- [ ] **Step 2: Push и PR**

```bash
git push -u origin feat/watermark-remover
gh pr create --draft --base main --head feat/watermark-remover --title "Watermark remover: /clean in RentFinderBot (flags off by default)" --body-file /tmp/wm-pr.md
gh pr checks --watch
```

Expected: ссылка на PR; проверки `unit` и `browser` зелёные. Если упало что-то не связанное с веткой — сравнить с последними прогонами `main` (`gh run list --branch main --limit 3`) и описать в отчёте.

---

### Task 13: Поправки по независимому ревью плана (сессия E)

Отдельный агент проверил план и код до передачи. Ниже его подтверждённые находки и согласованные исправления. Каждый пункт: сначала падающий тест, потом правка, потом `uv run pytest -q tests/watermark tests/test_wm_bot.py tests/test_bot_daemon.py tests/test_bot_unit.py tests/site/test_watermark_site.py tests/test_bot_delivery_clean.py tests/test_subscription_cli.py tests/test_subscription_telegram.py tests/test_release.py` и `uv run python scripts/check_changed_lint.py` (0 новых диагностик), отдельный коммит `fix(watermark): …`. Комментарии и docstrings — на английском.

**13.1 (важно, этап 3) Превью сайта остаются со знаками.** Превью пересобирается только если файл превью старше чистой копии. Если чистая копия появилась раньше (этап 2: копия при доставке, а превью при публикации собраны из оригинала при выключенном флаге сайта), после включения `WATERMARK_SITE_USE_CLEAN` превью со знаками остаются навсегда; то же после обрыва прохода бота между чисткой и пересборкой.
- `src/watermark/store.py`: `SITE_MARKER_SUFFIX = ".site"`, `site_marker_for(clean: Path) -> Path` (`<clean>.site`); `discard()` удаляет и маркер.
- `src/watermark/published.py`: `site_variant_files(stored) -> list[Path]` — `photos_root() / f"{rel}.{kind}.webp"` для `kind in VARIANT_WIDTHS`, где `rel` — путь оригинала относительно `photos_root()` (вне корня — пустой список); `needs_site_variants(stored) -> bool` — чистая копия есть (`clean_or_original(original) != original`), а маркера нет или он старше чистой копии; `rebuild_site_variants(paths) -> int` — при выключенном флаге сайта сразу `0`; иначе для нуждающихся: удалить их превью, `generate_for_stored_paths(todo)`, затем `touch()` маркеров; вернуть число фото.
- `sweep_once`: после чистки, если флаг сайта включён, вызывать `rebuild_site_variants` для ВСЕХ публичных путей кусками по 20 через `runner`. `wm-clean-published`: после чистки при включённом флаге — `rebuild_site_variants(все публичные пути)` и вывести число. `wm_bot._refresh_site_variants` → `rebuild_site_variants` (в потоке). `wm-revert` → `site_variant_files` вместо `original.with_name(...)`.
- Тесты (`tests/site/test_watermark_site.py`): в `test_variants_come_from_clean_copy_only_with_flag` убрать `os.utime`, использовать естественный порядок (превью из оригинала → `ensure_clean` → флаг → `process_photo` пересобирает → повтор идемпотентен); новый тест «копия раньше превью»: `ensure_clean` → `process_photo` при выключенном флаге (превью со знаками) → флаг включён → `rebuild_site_variants([orig]) == 1`, в `detail.webp` знаков нет → повторный вызов `== 0`.

**13.2 (важно) Ложное срабатывание CityExpert испортит фото.** Порог `min_score` CityExpert 0.14, настоящие знаки на образцах 0.157–0.191, лучший «чужой» кандидат 0.111 — запас мал; ложное «вычитание» оставит тёмный негатив логотипа. Надёжный признак: у настоящего знака подобранный множитель прозрачности k ≈ 0.94–1.00, у отсутствующего — упирается в нижнюю границу 0.60.
- `src/watermark/pipeline.py`: константа `MIN_PLAUSIBLE_K = 0.7`; в `clean_array` для ветки вычитания: `candidate, k = remove_reverse_blend(out, tpl, det)`; если `k <= MIN_PLAUSIBLE_K` — пропустить этот знак (картинку не менять, в отчёт не добавлять), иначе `out = candidate`.
- Тест (`tests/watermark/test_pipeline.py`): подменить `pipeline.find_marks` (monkeypatch) так, чтобы на фоне БЕЗ знака возвращалась детекция CityExpert в предсказанном месте (`match_instance(to_gray(img), tpl, tpl.predicted(w, h)[0])`); `clean_array` возвращает картинку без изменений и `report.changed is False`; на фоне со знаком знак по-прежнему убирается.
- Runbook, этап 0: брать 300 последних фото (`head -300`), смотреть все `*.marks.jpg`.

**13.3 `/clean` в подписи к фото.** Фильтр `Command` в aiogram срабатывает и на подписи: фото с подписью `/clean` получает подсказку и не чистится (в альбоме теряется первое фото). В `build_watermark_router`: `@router.message(Command("clean", "watermark"), F.text)`.

**13.4 Демон не должен терять `/start` подписок; старый поллер — не второй поллер.**
- `run_bot_daemon`: `await bot.delete_webhook(drop_pending_updates=False)`; вместо выбрасывания очереди — в `_on_media` пропускать фото старше 30 минут (`message.date`; без даты — считать свежим), с записью в лог.
- `run_subscription_bot()` (старая команда `otz subscription-bot`): только предупреждение в лог (текст: «[subscription-bot] replaced by otz bot (otz-bot.service), not starting») и `return`, без запуска демона: случайно включённый старый юнит не станет вторым поллером. Тесты подписок (`tests/test_subscription_cli.py`) должны остаться зелёными.

**13.5 Доставка: таймаут и замороженные оригиналы.**
- `_clean_delivery_paths`: `await asyncio.wait_for(asyncio.to_thread(_clean_all), timeout=180)`; `TimeoutError` → оригиналы с предупреждением в лог.
- В payload замораживать ОРИГИНАЛЫ (чистые копии — производные данные, не в бэкапе, их удаляет `wm-revert`): убрать подстановку перед `rendered_text`; вместо неё внутри цикла получателей сразу после `photo_paths = [Path(p) for p in frozen["photo_paths"]]` добавить `if get_settings().watermark_clean_on_delivery: photo_paths = await _clean_delivery_paths(match_id, photo_paths)`. Тест `test_flag_on_sends_clean_copies`: альбом — чистые копии, замороженный payload — оригиналы.

**13.6 Без флагов — без OpenCV.** `src/watermark/store.py`: импорт `clean_file` перенести внутрь `ensure_clean` (сайт и админка не должны тянуть cv2 при выключенных флагах). `scripts/release.py`, проверка окружения перед активацией: `"import sqlalchemy,playwright,torch,open_clip,fastapi,cv2"`.

**13.7 Защита оригиналов кодом, а не только документом.**
- `clean_root()`: `RuntimeError`, если `WATERMARK_CLEAN_DIR` совпадает с `inbound_photos_dir` или лежит внутри него. На стороне показа (`clean_or_original`, `is_current`, `site_source`) эта ошибка = «чистой копии нет» (вернуть оригинал), на стороне чистки — пробрасывать.
- `clean_file`: отказ (`ValueError`), если `dst.resolve() == src.resolve()`.
- `wm-clean`: отказ, если итоговый файл попадает внутрь `inbound_photos_dir` (без `--out-dir` рядом с фото из `data/inbound`): сообщение «use --out-dir outside data/inbound».
- Тесты на все три случая (`tests/watermark/test_store.py`, `test_pipeline.py`, `test_cli.py`).

**13.8 Runbook (`docs/operations/watermark-remover.md`).**
- Этап 1: откат — `WATERMARK_BOT_ENABLED=false` и `sudo systemctl disable --now otz-bot` (если нужны подписки — оставить демон с выключенным флагом знаков).
- Этап 3: порядок — флаг → `sudo systemctl restart otz-site otz-admin otz-monitor` → (по желанию `wm-clean-published --dry-run`, чтобы увидеть объём) → `sudo systemctl restart otz-bot`: его проход сам почистит фото и пересоберёт превью (прогресс в `bot.log`). Ручной `wm-clean-published` одновременно с ботом не запускать (две LaMa ≈ 2,2 ГБ); если нужен — при остановленном `otz-bot`.

**13.9 Мелочи.** Тест фильтра доступа через `Dispatcher.feed_update` (`Bot(token="42:TEST")`, подменённые `wm_bot.handle_clean_command` и `wm_bot.AlbumCollector.add`): личка разрешённого chat_id → обработчик вызван; чужая личка и группа → нет; фото с подписью `/clean` → обработчик фото. В `test_album_is_collected_into_one_reply` ждать `asyncio.sleep(0.5)` вместо `0.2`. В `src/watermark/lama.py` убрать `torch.set_num_threads(...)` (это настройка всего процесса и в мониторе поменяла бы CLIP).

---

## Известные ограничения

- Знаки агентств внутри фото (BEOSTAN и т. п.) не убираются — это отдельная задача.
- Если площадка сменит дизайн, размер или место знака, поиск перестанет его находить, и бот вернёт фото без изменений («знаков не нашёл»). Удаление идёт только там, где знак найден с высокой уверенностью ровно на своём месте; у CityExpert запас по уверенности меньше, поэтому Task 13 добавляет вторую проверку. Для нового знака нужен новый шаблон (отдельная небольшая задача: ~50 свежих фото, усреднение, геометрия).
- Под логотипами 4zida и Halo нейросеть дорисовывает правдоподобный фон, а не настоящие пиксели; под CityExpert восстанавливаются настоящие пиксели.
- Telegram сам пережимает фото, присланные как «фото». Для максимального качества — присылать файлами или пользоваться `/clean` на альбоме кандидата (берутся оригиналы с диска).
- На сервере до ~10 с на фото с LaMa: альбом из 10 фото может обрабатываться около минуты; бот сразу пишет «Обрабатываю…».
- `data/clean` не входит в бэкапы (пересоздаётся командами `wm-clean-match`, `wm-clean-published`).

## Выпуск на прод

Только после явного «да» Дмитрия и не силами сессий Haiku: штатный релиз по `docs/operations/releases.md` (пакет, квитанции тестов и бэкапа, активация), затем этапы 0–3 из `docs/operations/watermark-remover.md` по одному, с проверкой после каждого.
