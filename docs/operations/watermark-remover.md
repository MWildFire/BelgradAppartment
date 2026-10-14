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
  && find data/inbound -name "*.jpg" -mtime -7 | head -300 | xargs -r cp --parents -t /tmp/wm-qa-in \
  && .venv/bin/python -m src.cli wm-qa /tmp/wm-qa-in --out-dir /tmp/wm-qa-out'
```

Скачать к себе все `/tmp/wm-qa-out/*.marks.jpg` (`scp -r <сервер>:/tmp/wm-qa-out .`) и посмотреть каждый:
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
4. Откат: `WATERMARK_BOT_ENABLED=false` и `sudo systemctl disable --now otz-bot`. Если нужны
   подписки, демон не выключать: оставить его с выключенным флагом знаков.

`SUBSCRIPTION_BOT_ENABLED=true` (если когда-то включат подписки в Telegram) обслуживает тот же
`otz-bot`.

### Этап 2 — кандидаты сразу без знаков

1. `WATERMARK_CLEAN_ON_DELIVERY=true`, затем в тихое окно `sudo systemctl restart otz-monitor`.
2. Цена: чистка идёт внутри монитора — до ~1 ГБ памяти на LaMa и +5–30 с на альбом.
   После первых кандидатов: `systemctl status otz-monitor --no-pager | grep -i memory`.
3. Любой сбой чистки — уходят оригиналы (доставка важнее). Откат: флаг `false` + рестарт монитора.

### Этап 3 — сайт без знаков

1. `WATERMARK_SITE_USE_CLEAN=true`, затем
   `sudo systemctl restart otz-site otz-admin otz-monitor`. По желанию `wm-clean-published --dry-run`
   покажет объём работы.
2. `sudo systemctl restart otz-bot`: его проход сам почистит фото и пересоберёт превью (прогресс —
   в `/var/log/otz/bot.log`). Ручной `wm-clean-published` одновременно с ботом не запускать: две
   модели LaMa занимают ≈ 2,2 ГБ. Если он нужен, сначала остановите `otz-bot`. Дальше новые
   публикации бот дочищает сам раз в 10 минут (нужен этап 1).
3. Cloudflare: Caddy отдаёт `/photos/*` с `no-store`, но если в Cache Rules для `/photos/*`
   включён «Cache Everything» с Edge TTL, сделать Purge by prefix `rentbelgrad.com/photos/`,
   иначе до недели будут видны старые картинки.
4. Плохая чистка у конкретной квартиры: `wm-revert <match_id>`.
5. Полный откат: флаг `false` + `sudo systemctl restart otz-site otz-admin otz-monitor otz-bot`. Уже пересобранные превью остаются
   чистыми (это безвредно); для конкретных квартир — `wm-revert`.

## Диагностика

- Бот не отвечает: `systemctl status otz-bot`, `tail -n 100 /var/log/otz/bot.log`.
  `TelegramConflictError` — запущен второй поллер (старый `otz-subscription-bot`, локальный dev).
- `LaMa unavailable — falling back to Telea` в логе: нет модели или torch → `wm-fetch-model`.
- Нехватка памяти: у юнита `MemoryMax=2G`; systemd перезапустит демон, придёт алерт OnFailure.
- Чистые копии можно пересоздать в любой момент: `wm-clean-match <id> --force`,
  `wm-clean-published --force`.
