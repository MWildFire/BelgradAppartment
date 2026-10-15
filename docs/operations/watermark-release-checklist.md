# Водяные знаки — чек-лист решения о выпуске, R03

## 1. Статус

Ветка результата — `feat/watermark-remover`, [PR #14](https://github.com/MWildFire/BelgradAppartment/pull/14) в `main` остаётся draft. На снимке D1 (15.10.2026) проверена голова `4c1a39ddfa1ce5eff9247798193b3276be41be38`; слияний R03 на ней ещё нет. Список слияний и итог качества будут дополнены в D2. Это подготовка решения: выпуск только после явного «да» Дмитрия; сессии R03 не выполняют действий на сервере. Источники: PR #14; [план, «Выпуск на прод»](../superpowers/plans/2026-10-14-watermark-remover.md#выпуск-на-прод); [правила R03](/Volumes/StudioData/Home/Projects/codex-plans/2026-10-15/rentfinder-wm/PLAN.md).

- [ ] Дмитрий принял решение по качеству, способу выпуска и публикации очищенных фото.
- [ ] Для точной выпускаемой ревизии есть реальные квитанции тестов и восстановления; порядок подготовки, активации и отката — в [releases.md](releases.md). Локальные тесты и draft PR сами по себе не означают готовность прода.

## 2. Качество чистки

R02 после Task 13: **36 фото, ok 8 / minor 12 / bad 16**. Углы Halo с Telea: **11 bad из 17**. Публичную чистку и чистку доставки до решения не включать. Источник: [QA R02, «После Task 13»](../reviews/2026-10-14-watermark-qa.md), [финальное ревью R02, разделы 2 и 6](../reviews/2026-10-14-watermark-review-final.md).

Итоговая QA R03 появится в D2: `docs/reviews/2026-10-15-watermark-qa-r03.md` (в D1 файл ещё не создан). По [плану R03, «Что уже решено»](/Volumes/StudioData/Home/Projects/codex-plans/2026-10-15/rentfinder-wm/PLAN.md) до ответа Дмитрия выбран `WATERMARK_HALO_CORNER_MODE=skip`: знак в углу остаётся; это `kept`, а не успешное удаление. На снимке D1 настройка ещё не реализована; её наличие и допустимые значения проверяются в D2 по коду. `telea` — прежняя чистка; новые способы не включаются по умолчанию.

- [ ] Просмотрены итоговые таблицы всех 36 фото и всех знаков, отдельно 17 углов каждого режима; выбрано допустимое качество. Отдельно проверены `halo_006` и `halo_012` по оригиналам и результатам (критерии — [план R03, «Оценка качества»](/Volumes/StudioData/Home/Projects/codex-plans/2026-10-15/rentfinder-wm/PLAN.md)).

## 3. Почему лёгкий выкат не подойдёт

`validate_data_only_release` требует побайтового совпадения зависимостей, конфига, схемы и юнитов с текущим выпуском. Sanitized-копия допускается только для узкого code-only режима. Проверка — [scripts/release.py:272](../../scripts/release.py#L272), фиксированные пути и сравнение — строки 285–293; основание для этой ветки — [R2, раздел 5](../reviews/2026-10-14-watermark-review-final.md).

Проверено на голове D1 командой `git diff --stat origin/main...HEAD -- pyproject.toml uv.lock src/config.py deploy/`:

| Файл | Изменение против общей базы с main |
|---|---:|
| [pyproject.toml](../../pyproject.toml) | +2 строки: OpenCV |
| [uv.lock](../../uv.lock) | +22 строки: зависимость OpenCV |
| [src/config.py](../../src/config.py) | +17 строк: настройки чистки |
| [deploy/otz-bot.service](../../deploy/otz-bot.service) | новый юнит, +35 строк |

- [ ] Дмитрий выбрал **полную защищённую копию прода** либо **другой согласованный порядок выпуска**. Полная копия включает защищённые конфигурацию и Telegram-сессии на Mac; ранее Дмитрий от неё отказывался. Этот чек-лист не выбирает вариант за него и не разрешает обход проверки. Источники: [offhost-backup.md, «Что считается принятой копией»](offhost-backup.md); `99 System/claude-streams/sprints/rentfinder-wm/PROJECT.md`, раздел «Выпуск на прод — что учесть» в личном vault.

## 4. Этапы включения 0–3

Порядок: решение по качеству → выпуск кода с тремя флагами запуска чистки `false` → этапы по одному с проверкой результата. LaMa по умолчанию `true`, но сама не включает чистку. Источники: [src/config.py:270](../../src/config.py#L270), [.env.example:266](../../.env.example#L266), [R2, раздел 6](../reviews/2026-10-14-watermark-review-final.md).

| Этап | Что принять перед следующим этапом | Команды для Дмитрия |
|---|---|---|
| 0 | Модель проверена, свежие серверные фото просмотрены | [Runbook, этап 0](watermark-remover.md#этап-0--после-релиза-флаги-выключены) |
| 1 | Бот, доступ и один поллер проверены | [Runbook, этап 1](watermark-remover.md#этап-1--команда-в-боте) |
| 2 | Доставка и расход памяти проверены | [Runbook, этап 2](watermark-remover.md#этап-2--кандидаты-сразу-без-знаков) |
| 3 | Сайт, обновление превью и кэш проверены | [Runbook, этап 3](watermark-remover.md#этап-3--сайт-без-знаков) |

- [ ] Зафиксирован результат каждого этапа. Откаты описаны там же: полный откат сайта возвращает оригиналы на `/photos/`, уже созданные превью остаются чистыми; для конкретного кандидата предусмотрен `wm-revert`. Поведение сверено с [store.py:219](../../src/watermark/store.py#L219) и [cli.py:213](../../src/watermark/cli.py#L213).

## 5. Один поллер на токен

`otz-bot` заменяет `otz-subscription-bot`; вместе их не запускать. `SERVICES` содержит оба имени ([scripts/release.py:29](../../scripts/release.py#L29)), снимок релиза учитывает активные сервисы ([scripts/release.py:514](../../scripts/release.py#L514)). Старый юнит отключается отдельным шагом, а не самим фактом появления нового файла: [план:3848](../superpowers/plans/2026-10-14-watermark-remover.md#L3848), [runbook, этап 1](watermark-remover.md#этап-1--команда-в-боте).

- [ ] Перед включением нового демона старый отключён по runbook; проверено отсутствие второго поллера.
- [ ] Учтено сохранение очереди: после рестарта старые текстовые `/clean` могут выполниться, фильтр старости относится к фото. Источник: [R2, пункт 3.1](../reviews/2026-10-14-watermark-review-final.md).

## 6. Память

Предварительный ориентир R02 — около **1,1 ГБ на процесс с LaMa**; у `otz-bot` **MemoryMax=2G** ([deploy/otz-bot.service:28](../../deploy/otz-bot.service#L28)). Доставка работает в `otz monitor`, `/clean` и sweep — в `otz bot`; общего замка между процессами нет. Могут одновременно жить две модели, ориентир около 2,2 ГБ суммарно: [R2, пункт 2.7](../reviews/2026-10-14-watermark-review-final.md), [bot_delivery.py:134](../../src/telegram/bot_delivery.py#L134), [wm_bot.py:121](../../src/telegram/wm_bot.py#L121).

Это **не замер R03** и не утверждение, что суммарная память двух процессов ограничена одним `MemoryMax`: лимит в файле юнита относится к `otz-bot`. Фактический замер лаборатории и условия измерения будут добавлены в D2 (источник — [задание wm-docs, шаги 10–12](/Volumes/StudioData/Home/Projects/codex-plans/2026-10-15/rentfinder-wm/tasks/wm-docs.md)).

- [ ] Дмитрий выбрал вариант: замок между процессами, увеличение лимита после оценки памяти сервера либо доставка без LaMa. Это варианты для решения, не уже реализованные режимы. Замок может ограничить одновременную работу, но сам по себе не выгружает две уже загруженные модели; отдельной настройки «LaMa только для доставки» D1 не вводит. Источники: [lama.py:108](../../src/watermark/lama.py#L108), [задание wm-docs, раздел 6 чек-листа](/Volumes/StudioData/Home/Projects/codex-plans/2026-10-15/rentfinder-wm/tasks/wm-docs.md).

## 7. После выката

- [ ] Подготовлен и принят отдельный PR пина sanitized-копии: `SOURCE_REVISION` и `HELPER_SET_SHA256`, с соответствующим набором helper-файлов. Простая замена одного SHA недостаточна; старое незавершённое состояние разбирается кодом своей эпохи. Порядок — [offhost-backup.md:139](offhost-backup.md#L139); прецедент — PR #12, указанный в задании R03.
- [ ] Если для `/photos/*` включён Cloudflare «Cache Everything» с Edge TTL, после этапа 3 выполнен Purge по префиксу. Условие и действия — [runbook, этап 3, пункт 3](watermark-remover.md#этап-3--сайт-без-знаков). В R03 Cloudflare не проверялся и не менялся.

## 8. Модель на сервере

Команда для оператора — `otz wm-fetch-model`; серверный вызов приведён только в [runbook, этапе 0](watermark-remover.md#этап-0--после-релиза-флаги-выключены). По умолчанию модель лежит в `data/models/big-lama.pt` относительно корня проекта (на сервере `/opt/otz/data/models/big-lama.pt`), настройка — `WATERMARK_LAMA_MODEL_PATH`. Источники: [src/config.py:278](../../src/config.py#L278), [cli.py:240](../../src/watermark/cli.py#L240).

- [ ] Размер **205669692 байт**; SHA-256 **`344c77bbcb158f17dd143070d1e789f38a66c04202311ae3a258ef66667a9ea9`** — [lama.py:29–31](../../src/watermark/lama.py#L29). Загрузка проверяет сумму до замены файла; повторная загрузка не нужна при верной сумме ([lama.py:131](../../src/watermark/lama.py#L131)). Наличие модели на сервере в R03 не проверялось.

## 9. Совместимость с main

Ветка была пересобрана на `adcafcbd0054f8ea8c25d9afec38a8dc56253c27` 14 октября. В плане R03 последующее движение `main` связано с выкатом №2 RentBelgrad (PR #15 и пин); их фактический статус здесь не подтверждается. В R03 `main` в ветку проекта не вливается. Источник: [план R03, «Состояние на старте» и «Что уже решено»](/Volumes/StudioData/Home/Projects/codex-plans/2026-10-15/rentfinder-wm/PLAN.md).

- [ ] Перед выпуском сверена совместимость свежих голов командой `git merge-tree --write-tree --name-only origin/main origin/feat/watermark-remover`. Итог R03 предоставляет `wm-review`; отсутствие текстового конфликта не заменяет проверки релиза. Источник: [задание wm-docs, раздел 9 чек-листа](/Volumes/StudioData/Home/Projects/codex-plans/2026-10-15/rentfinder-wm/tasks/wm-docs.md).

## 10. Юридическая сторона

По рамке задания фото и знаки принадлежат площадкам Halo Oglasi, 4zida и CityExpert; техническое удаление знака не устанавливает право на публикацию фотографии. Публиковать очищенные фото на сайте — отдельное решение Дмитрия. Права на конкретные снимки в R03 не проверялись. Источник: [задание wm-docs, раздел 10 чек-листа](/Volumes/StudioData/Home/Projects/codex-plans/2026-10-15/rentfinder-wm/tasks/wm-docs.md).

- [ ] Дмитрий отдельно принял решение о публикации очищенных фото.

## 11. Уборка после закрытия проекта — только Дмитрий

Сессиям R03 удалять эти каталоги запрещено. После сохранения нужных результатов Дмитрий проверяет чистоту каждого worktree и выбирает, что убрать. Перечень — [план R03, «Открытые вопросы Дмитрию»](/Volumes/StudioData/Home/Projects/codex-plans/2026-10-15/rentfinder-wm/PLAN.md), `PROJECT.md` в vault, «Заготовка R03». Следующие команды — локальные примеры для Дмитрия после закрытия, в R03 они не выполнялись:

```bash
git -C /Volumes/StudioData/Home/Projects/BelgradAppartment worktree list
git -C /Volumes/StudioData/Home/Projects/BelgradAppartment worktree remove /Volumes/StudioData/Home/Projects/BelgradAppartment-wm-bot
git -C /Volumes/StudioData/Home/Projects/BelgradAppartment worktree remove /Volumes/StudioData/Home/Projects/BelgradAppartment-wm-site
# Каждый подтверждённый каталог R03 — отдельно, без --force:
git -C /Volumes/StudioData/Home/Projects/BelgradAppartment worktree remove /Volumes/StudioData/Home/Projects/BelgradAppartment-wm-r03/docs-1
git -C /Volumes/StudioData/Home/Projects/BelgradAppartment branch -d codex/wm-r03-lab-lama
git -C /Volumes/StudioData/Home/Projects/BelgradAppartment branch -d codex/wm-r03-lab-blend
```

- [ ] Проверены `-wm-bot`, `-wm-site`, все `/Volumes/StudioData/Home/Projects/BelgradAppartment-wm-r03/*` и локальные ветки `codex/wm-r03-lab-*`. Ветки удаляются только после освобождения соответствующего worktree; отказ безопасного `-d` не обходится принудительным удалением.
- [ ] Отдельно решена судьба `/Volumes/StudioData/Home/Projects/BelgradAppartment/data/wm-assets-c2pa-original/` и `/tmp/wm-*` после сохранения нужных приватных материалов. Финальный список и команды предоставляет `wm-review`; массовое удаление по шаблону этот документ не предлагает. Источник: [задание wm-docs, раздел 11 чек-листа](/Volumes/StudioData/Home/Projects/codex-plans/2026-10-15/rentfinder-wm/tasks/wm-docs.md).
