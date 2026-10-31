# Watermark — LaMa в боте: процесс-воркер, лимиты памяти, как проверить

Документ раунда WM05 (31 октября 2026), поток W3. Дополняет `watermark-remover.md`: там — что и как
убирается, здесь — как LaMa живёт рядом с ботом на сервере с 3.8 ГБ ОЗУ и почему так.

## Что устроено

- **LaMa работает в отдельном процессе** (`python -m src.watermark.lama_proc`, режим по умолчанию
  `WATERMARK_LAMA_ISOLATION=process`). В процессе бота `torch` не импортируется вообще; воркер
  стартует при первом знаке, которому нужна LaMa (4zida, центр Halo, углы Halo в режиме `lama`),
  обслуживает **один запрос за раз** (очередь — по порядку вызовов) и завершается после
  `WATERMARK_LAMA_IDLE_UNLOAD_SECONDS` (600 с) без запросов, возвращая системе всю память.
- Центральные и угловые знаки идут через один и тот же вход `LamaInpainter.predict`, значит под
  одним воркером, одной блокировкой, одним лимитом потоков и одним таймаутом.
- Фото без знаков LaMa (CityExpert, углы Halo в режиме `skip`) воркер не запускают.
- **Если воркер упал, не ответил за `WATERMARK_LAMA_TIMEOUT_SECONDS` (180 с) или его убил OOM-killer**,
  бот жив: знак остаётся (`method=skip`, `kept_reason=lama_unavailable`), пиксели фото не тронуты,
  пользователь получает оригинал и честный текст «Знак найден и оставлен без изменений». Следующий
  запрос поднимает новый воркер; сохранённые результаты с `lama_unavailable` пересчитываются, когда
  модель снова доступна (`store._retry_available_lama`).
- `WATERMARK_LAMA_ISOLATION=inline` — модель в том же процессе (тесты, разовые запуски CLI).
  Выгрузка по простою там тоже есть (`ModelRunner.unload_if_idle`), но внутри процесса память
  возвращается лишь частично (см. цифры), поэтому для бота режим не годится.

## Настройки (`src/watermark/settings.py`, переменные окружения `WATERMARK_*`)

| Настройка | По умолчанию | Смысл |
|---|---|---|
| `watermark_lama_isolation` | `process` | `process` — воркер, `inline` — в том же процессе |
| `watermark_lama_max_pixels` | 75000 | бюджет площади кропа, px; 0 — выключен (при 105000 сумма RSS бота и воркера на альбоме из 10 фото была 1.02–1.07 ГиБ) |
| `watermark_lama_min_margin` | 16 | нижняя граница `margin` при сжатии кропа до бюджета |
| `watermark_lama_max_side` | 1024 | предел длинной стороны кропа (практически не срабатывает) |
| `watermark_torch_threads` | 2 | потоки torch (на память не влияют, на время да) |
| `watermark_lama_idle_unload_seconds` | 600 | простой до выхода воркера; 0 — не выходить |
| `watermark_lama_timeout_seconds` | 180 | предел одного запроса к воркеру |

**Бюджет кропа.** Память модели растёт с площадью кропа (~2.6 КБ на пиксель сверх ~0.5 ГиБ за `torch`
и big-lama). Кроп, влезающий в бюджет, обрабатывается **так же, как в R04** (margin 96 px вокруг маски);
больший сначала сужает margin (до 16 px, родное разрешение), и только если и этого мало — уменьшается
масштаб. На 24 центральных знаках отличие от R04 небольшое (знаки 4zida в среднем 2.7 уровня серого из 255,
Halo-центр 7.5), на кропах 2× без видимых отличий, а память альбома из 10 фото укладывается в 1 ГиБ. Грубый
предел «длинная сторона ≤ 320–384» качество большого центрального знака Halo заметно ухудшает, поэтому не
используется. Углы Halo (160×720 px, +320 МиБ) в бюджет не входят: их кроп всегда такой, поэтому с углами в
режиме `lama` пик на 50–60 МиБ выше (см. таблицу).

## Замеры (Linux, Colima, контейнер `python:3.12-slim` 2 CPU, потоки 2, бюджет 75000; `scripts/wm_memory_probe.py`)

Каждая строка — свежий контейнер и свежий процесс. «Σ RSS» — сумма RSS бота и воркера в пике (выборка каждые
50 мс), «cgroup» — `memory.peak` контейнера (включает кэш файлов и зависит от того, тёплый ли кэш хоста).
Альбом 10 = 4 Halo + 3 4zida + 3 CityExpert.

| платформа, лимит | набор | углы | Σ RSS, МиБ | cgroup, МиБ | бот в простое после выхода воркера, МиБ | с/фото |
|---|---|---|---|---|---|---|
| aarch64, 4g | альбом 10 | skip | 968 | 860 | 93 | 1.05 |
| aarch64, 4g | альбом 10 | lama | 1016 | 936 | 116 | 1.48 |
| aarch64, 4g, холодный кэш | альбом 10 | skip / lama | 947 / 1010 | 1090 / 939 | 116 / 119 | 1.01 / 1.42 |
| aarch64, 1.5g (как `MemoryMax`) | альбом 10 | skip / lama | 944 / 1031 | 883 / 921 | 131 / 93 | 1.05 / 1.46 |
| aarch64, 4g | все 36 | skip / lama | 994 / 1091 | 952 / 981 | 133 / 124 | 0.79 / 1.36 |
| x86_64 (Rosetta), 4g | альбом 10 | skip / lama | 981 / 1052 | 1070 / 923 | 255 / 286 | 1.96 / 2.70 |

Углы `skip` (умолчание): пик 0.92–0.96 ГиБ. Углы `lama`: 0.98–1.01 ГиБ (кроп угла сам даёт +320 МиБ). Под
лимитом 1.5 ГиБ ни один прогон не убит OOM-killer-ом. На x86_64 под эмуляцией сам процесс весит больше (бот в
простое 255–286 МиБ вместо ~100 — память транслятора), поэтому цифры завышены.

Без изменений (код R04, LaMa в процессе бота): пик альбома 10 фото 1.51–1.54 ГиБ, простой после
пачки 1.39–1.44 ГиБ; выгрузка модели внутри процесса (`del`, `gc.collect()`, `malloc_trim(0)`)
возвращает ~60 МиБ из ~1.3 ГиБ. Кэш файла модели (~200 МиБ) после загрузки сбрасывается
(`posix_fadvise(DONTNEED)`): без этого `memory.peak` альбома был 1.09–1.11 ГиБ. При запуске с холодным
кэшем файловый кэш библиотек (`torch` и др.) тоже попадает в cgroup — отсюда 1090 МиБ в строке «холодный кэш»;
ядро вытесняет его раньше, чем нужно убивать процессы.

Повторить (Linux в Colima; на macOS RSS нестабилен из-за сжатия памяти). `$WT` — каталог репозитория, `$WM` —
`/Volumes/StudioData/Home/Projects/Watermark`:

```
COLIMA_HOME=/Volumes/StudioData/AppData/colima-wm05 colima start --cpu 4 --memory 6 --disk 30 --vm-type vz \
  --mount-type virtiofs --vz-rosetta --mount $WT:r --mount $WM/private/datasets/wm-samples:r \
  --mount $WM/private/models:r --mount $WM/tmp/wm05/w3:w
docker --context colima run --rm --platform linux/arm64 --memory 4g --memory-swap 4g --cpus 2 \
  -v wm05-venv:/venv:ro -v $WT:/repo:ro -v $WM/private/datasets/wm-samples:/samples:ro \
  -v $WM/private/models:/models:ro -v $WM/tmp/wm05/w3/probe:/out \
  -e WATERMARK_LAMA_MODEL_PATH=/models/big-lama.pt -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONPATH=/repo \
  python:3.12-slim /venv/bin/python /repo/scripts/wm_memory_probe.py /samples \
  --set album10 --corner skip --idle-seconds 5 --out /out/album10-skip.json
```

Том `wm05-venv` — окружение по замку: `uv export --frozen --no-dev --extra clip --no-hashes --no-emit-project`,
`pip install -r` внутри такого же контейнера, затем `torch==2.12.0`/`torchvision==0.27.0` с индекса
`https://download.pytorch.org/whl/cpu` (на aarch64 PyPI отдаёт CUDA-сборку — её надо заменить на CPU).
Для x86_64 — `--platform linux/amd64` и отдельный том. Лимит 1.5 ГиБ — `--memory 1536m --memory-swap 1536m`,
холодный кэш — `colima ssh -- sudo sh -c 'sync; echo 3 > /proc/sys/vm/drop_caches'` перед прогоном.

## Как проверить на проде (только чтение)

- `systemctl status otz-subscription-bot` — память юнита (`Memory:`), число задач; `MemoryHigh/MemoryMax`
  задаёт drop-in вне репозитория (1280M/1536M).
- `ps -eo pid,ppid,rss,etime,args | grep -E "otz|lama_proc"` — родитель (бот) и, пока идёт обработка
  или не прошло 10 минут простоя, процесс `python -m src.watermark.lama_proc`; через ~10 минут после
  последней обработки воркера в списке нет, RSS бота ≈ 100–200 МБ.
- В журнале юнита: `[lama-worker] started pid=…` при запуске воркера, `idle for … s: stopping` при
  выходе, `LaMa failed, keeping …` если знак оставлен из-за сбоя.
