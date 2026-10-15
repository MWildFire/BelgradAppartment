# Водяные знаки — чек-лист решения о выпуске, R03

## 1. Статус

Ветка результата — `feat/watermark-remover`; [PR #14](https://github.com/MWildFire/BelgradAppartment/pull/14) в `main` остаётся draft. Снимок D2: `dd17dfc411ea2e72317383e1ad82b3f0d916f745`. В R03 слиты после отдельных REVIEW OK:

| PR | Что вошло | Merge SHA |
|---|---|---|
| [#16](https://github.com/MWildFire/BelgradAppartment/pull/16) | Чек-лист и две правки runbook | `e8690c4890068ead7f4ea6383bc352c2b9678a21` |
| [#17](https://github.com/MWildFire/BelgradAppartment/pull/17) | Исправления аудита сайта и импорта бота | `8ce0370c9a2adf8bd6dac42150bd25fcdaef9ac3` |
| [#18](https://github.com/MWildFire/BelgradAppartment/pull/18) | `skip` по умолчанию, прежний `telea`, учёт режима в кэше | `62fe7d8b0919460b2d13b9bc5e662bdbc5e42b34` |
| [#19](https://github.com/MWildFire/BelgradAppartment/pull/19) | Хвосты CLI/хранилища и запросы аудита | `12221fb7d7028ddf43fa8db4795198fd8abfaa4e` |
| [#20](https://github.com/MWildFire/BelgradAppartment/pull/20) | Необязательный `lama` для углов и сообщение CLI о kept | `dd17dfc411ea2e72317383e1ad82b3f0d916f745` |

Это подготовка решения: выпуск только после явного «да» Дмитрия; сессии R03 не выполняют действий на сервере. Основание: [план, «Выпуск на прод»](../superpowers/plans/2026-10-14-watermark-remover.md#выпуск-на-прод); [локальные правила R03](/Volumes/StudioData/Home/Projects/codex-plans/2026-10-15/rentfinder-wm/PLAN.md). Ссылки вида `/Volumes/...` в этом документе — локальные источники раунда на Mac; в GitHub они недоступны. Ссылки на файлы репозитория и PR открываются в GitHub.

- [ ] Дмитрий принял решение по качеству, способу выпуска и публикации очищенных фото.
- [ ] Для точной выпускаемой ревизии есть реальные квитанции тестов и восстановления; порядок подготовки, активации и отката — в [releases.md](releases.md). Локальные тесты и draft PR сами по себе не означают готовность прода.

## 2. Качество чистки

[Итоговая независимая QA R03](../reviews/2026-10-15-watermark-qa-r03.md) на одобренной голове `205de7268899863e38226b50a0d7d5511832c826`: 36 фото / 46 знаков, все три режима — `all matched`. По умолчанию **1 ok / 10 minor / 9 bad / 16 фото только kept**; ещё один kept-угол — в смешанном `zida_043`. По видам очищенных знаков: 4zida 5/11/2, центр Halo 0/3/3, CityExpert 0/1/4 (ok/minor/bad). Всего 17 углов оставлены намеренно.

| Режим углов | ok | minor | bad | kept |
|---|---:|---:|---:|---:|
| `skip`, по умолчанию | 0 | 0 | 0 | 17 |
| `telea`, прежняя чистка | 0 | 3 | 14 | 0 |
| `lama`, необязательный | 5 | 12 | 0 | 0 |

Для выбора углов QA рекомендует `lama` при доступной модели и подтверждённом бюджете памяти; до ответа Дмитрия остаётся `skip`. Это рекомендация только для углов: **9 bad в других видах остаются**, цель качества раунда не достигнута. Публичную чистку и чистку доставки до решения не включать. Настройки и fallback — [runbook, «Углы Halo»](watermark-remover.md#углы-halo-оставлять-или-чистить).

Историческая R02: **8/12/16** по фото; повторная более строгая оценка того же кода этой сессией — **1/12/23**. Это изменение оценки, не регрессия R03. Подробности и причины — [QA R03](../reviews/2026-10-15-watermark-qa-r03.md#историческая-оценка-и-повторная-база), [QA R02](../reviews/2026-10-14-watermark-qa.md#после-task-13).

- [ ] Просмотрены все 36 фото, 46 знаков и 17 углов каждого режима; принято допустимое качество. `halo_006` остаётся bad из-за дорисованного выступа/деформации дивана; у `halo_012` оранжевый предмет есть в оригинале, но его поверхность сглажена (minor). Разбор оригиналов — [QA R03](../reviews/2026-10-15-watermark-qa-r03.md#оригинал-и-дорисованный-предмет).

## 3. Почему лёгкий выкат не подойдёт

`validate_data_only_release` требует побайтового совпадения зависимостей, конфига, схемы и юнитов с текущим выпуском. Sanitized-копия допускается только для узкого code-only режима. Проверка — [scripts/release.py:272](../../scripts/release.py#L272), фиксированные пути и сравнение — строки 285–293; основание для этой ветки — [R2, раздел 5](../reviews/2026-10-14-watermark-review-final.md).

Проверено на снимке D2 `dd17dfc411ea2e72317383e1ad82b3f0d916f745` командой `git diff --stat origin/main...HEAD -- pyproject.toml uv.lock src/config.py deploy/`:

| Файл | Изменение против общей базы с main |
|---|---:|
| [pyproject.toml](../../pyproject.toml) | +2 строки: OpenCV |
| [uv.lock](../../uv.lock) | +22 строки: зависимость OpenCV |
| [src/config.py](../../src/config.py) | +21 строка: настройки чистки |
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

- [ ] Зафиксирован результат каждого этапа. Откаты описаны там же: полный откат сайта возвращает оригиналы на `/photos/`, уже созданные превью остаются чистыми; для конкретного кандидата предусмотрен `wm-revert`. Поведение сверено с [store.py:245](../../src/watermark/store.py#L245) и [cli.py:233](../../src/watermark/cli.py#L233).

## 5. Один поллер на токен

`otz-bot` заменяет `otz-subscription-bot`; вместе их не запускать. `SERVICES` содержит оба имени ([scripts/release.py:29](../../scripts/release.py#L29)), снимок релиза учитывает активные сервисы ([scripts/release.py:514](../../scripts/release.py#L514)). Старый юнит отключается отдельным шагом, а не самим фактом появления нового файла: [план:3848](../superpowers/plans/2026-10-14-watermark-remover.md#L3848), [runbook, этап 1](watermark-remover.md#этап-1--команда-в-боте).

- [ ] Перед включением нового демона старый отключён по runbook; проверено отсутствие второго поллера.
- [ ] Учтено сохранение очереди: после рестарта старые текстовые `/clean` могут выполниться, фильтр старости относится к фото. Источник: [R2, пункт 3.1](../reviews/2026-10-14-watermark-review-final.md).

## 6. Память

Ориентир R02 около **1,1 ГБ на процесс с LaMa** в Mac-замерах R03 не подтвердился. Лаборатория получила одиночные пики RSS всего процесса **1,73–5,02 GiB** (1,85–5,39 ГБ); удвоение максимума — **10,04 GiB / 10,78 ГБ**, только расчётный сценарий, не измерение двух процессов. Байты, фото, условия и ограничения приведены в [QA R03, «Лабораторный отбор и память»](../reviews/2026-10-15-watermark-qa-r03.md#лабораторный-отбор-и-память), источник — [локальный итог wm-lab-lama](/Volumes/StudioData/Home/Projects/codex-plans/2026-10-15/rentfinder-wm/results/wm-lab-lama.md).

Независимая серия движка дала максимум **5396348928 bytes / 5,026 GiB**; удвоение — **10,05 GiB / 10,79 ГБ**, также только арифметический сценарий. Источник: [локальный итог wm-engine](/Volumes/StudioData/Home/Projects/codex-plans/2026-10-15/rentfinder-wm/results/wm-engine.md), точные пары фото и времени — в QA выше.

У `otz-bot` **MemoryMax=2G** ([deploy/otz-bot.service:30](../../deploy/otz-bot.service#L30)). Доставка работает в `otz monitor`, `/clean` и sweep — в `otz bot`: [bot_delivery.py:134](../../src/telegram/bot_delivery.py#L134), [wm_bot.py:119](../../src/telegram/wm_bot.py#L119). Это два процесса с отдельными моделями; `lama` для углов использует тот же singleton и lock внутри каждого процесса ([lama_corner.py](../../src/watermark/lama_corner.py), [lama.py:108](../../src/watermark/lama.py#L108)). Межпроцессного замка нет. Лимит бота не ограничивает их суммарную память; в [otz-monitor.service](../../deploy/otz-monitor.service) отдельный MemoryMax не задан. Mac-RSS не доказывает конкретный Linux-OOM.

- [ ] До включения обоих путей согласованы замер Linux/cgroup и бюджет памяти. Варианты решения: межпроцессный замок для ограничения одновременного инференса, увеличение отдельных лимитов после замера ресурсов хоста либо доставка без LaMa. Замок сам по себе не выгружает две уже загруженные модели. Автоматически повышать лимит по Mac-замеру нельзя; отдельной настройки «LaMa только для доставки» R03 не вводит. Основание: [итог лаборатории](/Volumes/StudioData/Home/Projects/codex-plans/2026-10-15/rentfinder-wm/results/wm-lab-lama.md), [R2, пункт 2.7](../reviews/2026-10-14-watermark-review-final.md).

## 7. После выката

- [ ] Подготовлен и принят отдельный PR пина sanitized-копии: `SOURCE_REVISION` и `HELPER_SET_SHA256`, с соответствующим набором helper-файлов. Простая замена одного SHA недостаточна; старое незавершённое состояние разбирается кодом своей эпохи. Порядок — [offhost-backup.md:139](offhost-backup.md#L139); прецедент — PR #12, указанный в задании R03.
- [ ] Если для `/photos/*` включён Cloudflare «Cache Everything» с Edge TTL, после этапа 3 выполнен Purge по префиксу. Условие и действия — [runbook, этап 3, пункт 3](watermark-remover.md#этап-3--сайт-без-знаков). В R03 Cloudflare не проверялся и не менялся.

## 8. Модель на сервере

Команда для оператора — `otz wm-fetch-model`; серверный вызов приведён только в [runbook, этапе 0](watermark-remover.md#этап-0--после-релиза-флаги-выключены). По умолчанию модель лежит в `data/models/big-lama.pt` относительно корня проекта (на сервере `/opt/otz/data/models/big-lama.pt`), настройка — `WATERMARK_LAMA_MODEL_PATH`. Источники: [src/config.py:278](../../src/config.py#L278), [cli.py:262](../../src/watermark/cli.py#L262).

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
