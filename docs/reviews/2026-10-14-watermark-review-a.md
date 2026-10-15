# Ревью watermark-A: сессия R1 (ядро, Task 1–5)

Дата: 2026-10-14. Ревьюер: сессия R1 (`wm-r1`), код писал `wm-a`. Ветка `feat/watermark-remover`, база плана `91d6676`.
`origin/main` сдвинулся до `804ac59`. Merge-base ветки с ним по-прежнему `91d6676`, поэтому diff ветки не изменился.

Итог одной строкой: **ядро соответствует плану, можно продолжать (B и C).** Найдены два подтверждённых дефекта, оба исправлены с тестами. Остальное уже закрывает Task 13.

## 1. Коммиты и файлы: ок

- `git log --oneline origin/main..HEAD`: `d9a1839` (план), затем `98ab4e5`, `113f515`, `4c85b3c`, `4fc63e1`, `a355fd6` (Task 1–5). Посторонних коммитов нет.
- `git diff --stat origin/main...HEAD`: 40 файлов, все из карты файлов плана: `pyproject.toml`, `uv.lock`, `src/config.py`, `.env.example`, `src/cli.py`, `src/watermark/**` (модули и 8 файлов шаблонов), `tests/watermark/**`, план и 8 шаблонов в `docs/superpowers/plans/`. Лишнего нет.

## 2. Код совпадает с планом: ок

Проверка инструментом `wm_plan_apply.py`:

- До правок: 17 × `SAME` и 6 × `CONTAINS`.
- `tests/watermark/__init__.py`: 0 байт.
- `shasum -a 256` ассетов совпал со строками 216–223 плана: `sums-ok`.
- `git diff 91d6676 HEAD -- src/config.py .env.example pyproject.toml src/cli.py | grep '^-[^-]'`: пусто (только добавления).
- `uv.lock`: ровно одна строка `+name = "opencv-python-headless"`.
- Эталон `/tmp/wm-dryrun-r01`: `diff -r -x __pycache__` по `src/watermark` пуст; `uv.lock` побайтно совпадает.

Отклонение `wm-a` (только команда, код не менялся): `uv sync --extra clip --python 3.12` вместо `uv sync --extra clip`. Причина: на этом Mac `uv` по умолчанию берёт Python 3.14. Проверено сейчас: `.venv` = Python 3.12.13, cv2 4.14.0, torch 2.12.0.

После моих правок четыре файла из блоков плана показывают `DIFFERENT`. Отличия ровно по правкам из раздела 5: `src/watermark/lama.py`, `src/watermark/published.py` и их тесты. Остальные 13 `SAME` и 6 `CONTAINS` не изменились.

## 3. Инварианты: ок

- Запись и удаление (`grep -rnE "unlink|write_bytes|os.replace" src/watermark`): `store.py` пишет sidecar и удаляет клин-копии только в `data/clean`; `cli.py:228` (`wm-revert`) удаляет только превью `.card.webp`/`.detail.webp`, как и разрешено; `lama.py` пишет временный `.part` и файл модели; `pipeline.py:168–169` пишет в `dst`, который передаёт вызывающий (защита от `dst == src` и записи внутрь `inbound` — пункт 13.7).
- Флаги по умолчанию (`src/config.py`): `watermark_bot_enabled`, `watermark_clean_on_delivery`, `watermark_site_use_clean` = `False`. `watermark_lama_enabled` = `True` по плану: без torch или модели работает Telea или вычитание.
- Сеть в тестах: упоминается только в `tests/watermark/test_lama.py` (`httpx`/`respx`). Все вызовы внутри `respx.mock`, реальных запросов нет.
- «Один поллер на токен» и «доставка при ошибке чистки шлёт оригиналы»: относятся к Task 6–10, в этой ветке кода ещё нет (N/A).

## 4. Команды проверки: ок

| Проверка | До правок | После правок |
|---|---|---|
| `uv run pytest -q tests/watermark tests/test_subscription_cli.py tests/test_subscription_telegram.py tests/test_release.py` | 126 passed, 3 skipped (16.8 с) | 128 passed, 3 skipped (19.0 с) |
| `uv run python scripts/check_changed_lint.py` | New lint diagnostics: 0 | New lint diagnostics: 0 |
| `for s in scripts/*.sh; do bash -n "$s"; done` | shell-ok | shell-ok |

Три пропуска — тесты LaMa без модели, так и задумано планом.

## 5. Найдено и исправлено (2 подтверждённые ошибки)

**5.1. Частичный файл модели LaMa остаётся после обрыва.** `src/watermark/lama.py`, `download_model`.

- Что не так: если соединение оборвалось при скачивании, `.big-lama.pt.<pid>.part` (до 206 МБ) оставался в `data/models`. Каждая повторная попытка создавала новый файл, потому что pid в имени другой.
- Как проверено: тест `test_download_model_removes_partial_file_on_dropped_connection` до правки упал: `assert not [.../.big-lama.pt.16998.part]`.
- Правка: скачивание обёрнуто в `try`. При любом исключении временный файл удаляется, ошибка пробрасывается дальше.
- Коммит: `bc3283f` — `review: remove partial LaMa download after a dropped connection`.

**5.2. Один нечитаемый фото останавливает проход публикаций.** `src/watermark/published.py`, `clean_batch` / `sweep_once`.

- Что не так: `NotAnImageError` из `clean_file` вылетал из `clean_batch` и обрывал весь проход. Фото после него не чистились. Следующий проход (через 10 минут) снова упирался в то же фото, и так до его удаления. Та же ошибка была у `otz wm-clean-published`.
- Как проверено: тест `test_sweep_skips_undecodable_photo` до правки упал: `NotAnImageError: cannot identify image file` поднялся из `sweep_once` (`published.py:61` → `pipeline.py:163`).
- Правка: `clean_batch` пишет предупреждение в лог и пропускает такой файл.
- Коммит: `5d3580a` — `review: keep the watermark sweep going past an undecodable photo`.
- Принятый побочный эффект: нечитаемое фото попадает в предупреждение на каждом проходе. Sidecar для него не пишется.

Тесты после правок: 128 passed (два новых), lint: 0.

## 6. Найдено, но не правлю: закрывает Task 13

- **13.1 (важно). Подтверждено чтением кода, теста пока нет.** `process_photo` не перезаписывает существующий вариант (`src/site/photos.py:145`: `if target.exists()` → `skipped`). `wm-revert` удаляет превью только рядом с оригиналом (`src/watermark/cli.py:228`). На проде `SITE_PHOTOS_ROOT` переопределён на другой каталог (`src/config.py:207–210`). Когда превью станут строиться из чистых копий (этап 3), `wm-revert` на проде не откатит публичное превью. Закрывает 13.1.
- **13.2 (важно). Подтверждено.** Порог CityExpert `min_score` = 0.14 (`src/watermark/assets/cityexpert_center.json:28`), запас мал, ложное вычитание возможно. Закрывает 13.2.
- **13.6. Подтверждено.** `src/watermark/store.py:21` импортирует `clean_file` на уровне модуля, а через него тянется cv2. Сайт пока `store` не импортирует, поэтому сейчас проблемы нет. Закрывает 13.6.
- **13.7. Подтверждено.** `clean_root()` (`store.py:53`) не проверяет, что `WATERMARK_CLEAN_DIR` не лежит внутри `inbound`. `clean_file` (`pipeline.py:161`) не проверяет `dst != src`. `wm-clean` без `--out-dir` пишет рядом с файлом (`src/watermark/cli.py:98`), то есть может попасть в `data/inbound`. Закрывает 13.7.
- **13.9. Подтверждено.** `torch.set_num_threads(...)` в `lama.py:56` меняет настройку всего процесса. Закрывает 13.9.
- **13.3–13.5.** Относятся к коду Task 6–10 (бот, демон, доставка). В этой ветке их нет.
- **13.8.** Runbook, на этом этапе не проверяется.

## 7. Спорное: описано, не правлю

- **Путь LaMa не запускался.** `LamaInpainter.inpaint` ни разу не выполнялся: модели нет, три теста пропущены. Ошибки формы тензоров или масштаба тестами не поймать. Проверка — Task 11 на реальных фото.
- **4zida без LaMa откатывается к вычитанию знака.** Это осознанный компромисс плана, но без скачанной модели (`otz wm-fetch-model`) на 4zida будут пятна. Модель нужно скачать до включения флагов.
- **`wm-clean-match`** при одном нечитаемом фото падает с трассировкой и не обрабатывает остальные фото матча. Ручная команда, мелочь.
- **`wm-qa --out-dir` внутри проверяемой папки:** повторный запуск возьмёт собственные `*.pair.jpg`. Мелочь.

## Итог

Ядро соответствует плану: 13 из 17 файлов совпадают байт в байт, 6 из 6 правок в конфиге и CLI на месте, инварианты держатся. Два подтверждённых дефекта (5.1, 5.2) исправлены с тестами. Остальное — в Task 13. До включения флагов на проде нужно сделать Task 13, особенно 13.1 (`wm-revert`).
