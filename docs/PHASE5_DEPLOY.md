# Phase 5 — Deployment Guide (VPS)

Полный план переезда системы с локальной macOS на VPS для production-режима 24/7.

> **Целевая среда:** Ubuntu 24.04 LTS на Hetzner CX22 (€4.5/мес, 2 vCPU / 4 GB RAM / 40 GB SSD), Timeweb, Beget, или любой аналог. CX22 рекомендован — нам нужна оперативка под Chromium и Playwright + ~600 MB под CLIP-модель.
>
> **Стратегия:** все compute локально на VPS (никакого Bedrock/AWS). Не-rocket science: один Python-процесс под `otz monitor`, один systemd-timer для weekly-refresh, SQLite в файле, бэкап в Backblaze B2 раз в день. Минимум зависимостей, минимум подвижных частей.

---

## 0. Обзор архитектуры на VPS

```
                      Telegram API
                            ↑↓
                   ┌────────────────────┐
   (live SSE)──→   │  otz-monitor       │ ← systemd unit, always-on
                   │  (Telethon userbot)│
                   └─────────┬──────────┘
                             │ new inbound → pipeline
                             ▼
                   ┌────────────────────┐
                   │  scrape (throttled)│ → nekretnine + 4zida (httpx)
                   │  match (CLIP+LLM)  │ → halooglasi (xvfb-run Chromium)
                   │  deliver (userbot) │ ← Anthropic API
                   └─────────┬──────────┘
                             ▼
                       @Darya_kralj DM
                       (готовый шаблон + фото + ссылки)

   weekly  ──→  ┌────────────────────┐
   (Sun 03:00)  │  otz-weekly        │ ← systemd timer
                │  full scrape +     │
                │  mark stale        │
                └────────────────────┘

   nightly ──→  ┌────────────────────┐
   (03:00)      │  backup-db         │ ← cron, sqlite + state → B2
                └────────────────────┘
```

Все процессы под user `otz`, без sudo, в `/opt/otz/`.

---

## 1. Подготовка VPS

### 1.1 Заказать VPS

| Провайдер | План | Цена | Зачем |
|---|---|---|---|
| **Hetzner CX22** (рекомендую) | 2 vCPU / 4GB / 40GB SSD | €4.51/мес | Германия/Финляндия, ping до Сербии ~30мс. Best price/performance в Европе. |
| Timeweb Cloud (RU/EU) | 2 vCPU / 4GB / 40GB | ~$5 | Альтернатива если Hetzner недоступен. |
| Beget VPS | 2 vCPU / 4GB | ~$5 | Простой админ-интерфейс. |

OS: **Ubuntu 24.04 LTS** (любой свежий Debian/Ubuntu подойдёт).

### 1.2 Первичная настройка (как root через SSH)

```bash
# Базовые обновления
apt update && apt upgrade -y

# Создать пользователя otz
adduser --disabled-password --gecos "" otz
usermod -aG sudo otz   # на случай если нужно админ-действие; основной работы не требует sudo

# SSH-ключи: положить публичный ключ в /home/otz/.ssh/authorized_keys
mkdir -p /home/otz/.ssh
chmod 700 /home/otz/.ssh
# Скопировать твой ~/.ssh/id_*.pub в authorized_keys
chown -R otz:otz /home/otz/.ssh

# Зафаерволить (рекомендую — открыть только SSH)
ufw allow OpenSSH
ufw --force enable
```

### 1.3 Установить системные пакеты

```bash
apt install -y \
  python3.13 python3.13-venv python3.13-dev \
  build-essential libssl-dev libffi-dev \
  git curl rsync \
  xvfb \
  chromium-browser libnss3 libatk1.0-0 libatk-bridge2.0-0 libxss1 \
  libasound2t64 libcups2t64 libxcomposite1 libxdamage1 libxrandr2 \
  libgbm1 libgtk-3-0t64 libpango-1.0-0 \
  sqlite3
```

> **xvfb** даёт виртуальный X-display для headed Chromium (нужен halooglasi). **chromium-browser** + связанные libs покрывают системные зависимости Playwright. Playwright сам потом скачает свой binary, но без системных libs он не запустится.

---

## 2. Деплой приложения

### 2.1 Перенести код

```bash
su - otz  # переключиться на пользователя otz, всё дальше — от него
cd /opt
sudo mkdir otz && sudo chown otz:otz otz
cd /opt/otz

# Вариант A — через git (если репо на GitHub)
git clone <repo-url> .

# Вариант B — rsync с локалки (с твоей машины)
# (на твоей машине)
rsync -avz --exclude='.venv' --exclude='data' --exclude='__pycache__' \
  --exclude='*.session' --exclude='.env' \
  /Users/mvstrike/BelgradAppartment/ otz@<vps-ip>:/opt/otz/
```

### 2.2 Venv + зависимости

```bash
cd /opt/otz
python3.13 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e '.[clip]'   # CLIP вариант поставит torch+open_clip CPU
.venv/bin/pip install 'playwright>=1.45' 'playwright-stealth>=2.0' anthropic
.venv/bin/playwright install chromium --no-shell
```

⏱ Установка ~10-15 минут (torch + chromium = ~1.5 GB).

### 2.3 Структура каталогов

```bash
mkdir -p /opt/otz/data/{inbound,listings,state,playwright,backups}
mkdir -p /var/log/otz
sudo chown -R otz:otz /var/log/otz
```

### 2.4 Скопировать конфигурацию

```bash
# С локалки скопировать .env и Telethon session
scp /Users/mvstrike/BelgradAppartment/.env otz@<vps-ip>:/opt/otz/
scp /Users/mvstrike/BelgradAppartment/data/otz_monitor.session otz@<vps-ip>:/opt/otz/data/
scp /Users/mvstrike/BelgradAppartment/data/otz.db otz@<vps-ip>:/opt/otz/data/
chmod 600 /opt/otz/.env /opt/otz/data/*.session   # секреты — только владельцу
```

> **Альтернатива:** не копировать `otz.db`, сделать чистый запуск: `cd /opt/otz && .venv/bin/alembic upgrade head` + `otz add-source https://t.me/OtzRentBelgrad`. Затем дать первый `otz weekly-refresh` ночью — он засеет БД полностью за 6-7 часов.

### 2.5 Проверочный запуск

```bash
cd /opt/otz
.venv/bin/python -m src.cli list-sources
.venv/bin/python -m src.cli stats
.venv/bin/python -m src.cli refresh --force        # быстрый scrape, проверить что httpx+playwright идут
xvfb-run -a .venv/bin/python -m src.cli scrape halooglasi --max-pages=1   # отдельно halooglasi под xvfb
```

Если эти команды отрабатывают без ошибок — деплой готов.

---

## 3. systemd units

Положить два юнита и один timer.

### 3.1 `otz-monitor.service` — live режим, всегда работает

```ini
# /etc/systemd/system/otz-monitor.service
[Unit]
Description=otz-rent-finder — live Telegram monitor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=otz
WorkingDirectory=/opt/otz
Environment="PATH=/opt/otz/.venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
# xvfb-run чтобы halooglasi-scrape (когда триггерится pipeline) мог запустить headed chromium
ExecStart=/usr/bin/xvfb-run -a /opt/otz/.venv/bin/python -m src.cli monitor
Restart=on-failure
RestartSec=30
# Логи в journald (journalctl -u otz-monitor) + дополнительно в файл
StandardOutput=append:/var/log/otz/monitor.log
StandardError=append:/var/log/otz/monitor.log

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now otz-monitor
sudo systemctl status otz-monitor
journalctl -u otz-monitor -f   # смотреть live логи
```

### 3.2 `otz-weekly.service` + `otz-weekly.timer` — раз в неделю

```ini
# /etc/systemd/system/otz-weekly.service
[Unit]
Description=otz-rent-finder — weekly full refresh

[Service]
Type=oneshot
User=otz
WorkingDirectory=/opt/otz
ExecStart=/usr/bin/xvfb-run -a /opt/otz/.venv/bin/python -m src.cli weekly-refresh
StandardOutput=append:/var/log/otz/weekly.log
StandardError=append:/var/log/otz/weekly.log
TimeoutStartSec=8h
```

```ini
# /etc/systemd/system/otz-weekly.timer
[Unit]
Description=otz weekly full refresh

[Timer]
OnCalendar=Sun 03:00:00 Europe/Belgrade
Persistent=true
RandomizedDelaySec=15min

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl enable --now otz-weekly.timer
sudo systemctl list-timers | grep otz
```

> **Зачем `xvfb-run` в monitor**: live monitor может триггерить halooglasi-scrape когда Дарья форвардит новый inbound (через `process_new_inbound` → `scrape_all_with_throttle`). Тогда halooglasi-скрапер пытается поднять Chromium. Без `xvfb-run` ему некуда показывать окно и он падает.

### 3.3 Бэкап в Backblaze B2 (через cron)

```bash
# Установить b2-cli
sudo apt install -y python3-pip
pip install --user b2

# Авторизовать (один раз, нужны application key ID + key с https://secure.backblaze.com/app_keys.htm)
~/.local/bin/b2 authorize-account <keyID> <appKey>
~/.local/bin/b2 create-bucket otz-backups allPrivate
```

```bash
# /usr/local/bin/otz-backup.sh
#!/usr/bin/env bash
set -e
TS=$(date +%Y%m%d-%H%M%S)
TMPDIR=$(mktemp -d)
# SQLite hot backup (правильнее чем cp — захватывает WAL)
sqlite3 /opt/otz/data/otz.db ".backup '$TMPDIR/otz-$TS.db'"
tar -czf "$TMPDIR/otz-$TS.tar.gz" -C "$TMPDIR" "otz-$TS.db" -C /opt/otz/data state
/home/otz/.local/bin/b2 upload-file otz-backups "$TMPDIR/otz-$TS.tar.gz" "otz-$TS.tar.gz"
rm -rf "$TMPDIR"
# Подчистить старые (>30 дней)
/home/otz/.local/bin/b2 ls otz-backups | awk -v cutoff="$(date -d '30 days ago' +%Y%m%d)" \
  '$2 < cutoff { print $5 }' | xargs -r -I {} /home/otz/.local/bin/b2 delete-file-version {}
```

```bash
sudo chmod +x /usr/local/bin/otz-backup.sh
sudo crontab -u otz -e
# Добавить:
0 3 * * * /usr/local/bin/otz-backup.sh >> /var/log/otz/backup.log 2>&1
```

B2 цена: 50 МБ snapshot × 30 дней = 1.5 GB × $0.005/GB/мес = **<$0.01/мес**.

---

## 4. Telethon-сессия — особый момент

`otz_monitor.session` — это **зашифрованный SQLite-файл с креденшелами твоего Telegram-аккаунта**. Его нельзя пересоздать на VPS без SMS-кода (и каждый новый auth сжигает session API count).

**Делай так:**
1. Локально (на macOS) уже есть рабочий `data/otz_monitor.session`.
2. Скопировать на VPS через `scp` (см. 2.4).
3. Telegram может прислать **уведомление в твой Telegram** «новый вход с IP X» — это нормально, подтвердить.
4. **Никогда не запускать `otz auth` на VPS параллельно с локальным monitor'ом** — две сессии под одной auth ломаются (FloodWait).

После переезда — **остановить локальный `otz monitor`** на macOS, чтобы VPS-инстанс остался один.

---

## 5. Управление в production

### 5.1 Логи

```bash
journalctl -u otz-monitor -f                # live SSE из journald
tail -f /var/log/otz/monitor.log            # тот же поток в файле
journalctl -u otz-weekly --since "1 week ago"
```

### 5.2 Быстрая операционка

```bash
sudo systemctl restart otz-monitor          # рестарт live-режима
sudo systemctl stop otz-monitor             # выключить
sudo systemctl status otz-weekly.timer      # когда следующий weekly-run

cd /opt/otz
.venv/bin/python -m src.cli stats           # счётчики listings/matches/inbound
.venv/bin/python -m src.cli deliver-all     # вручную перешлёт все PENDING в @Darya_kralj
.venv/bin/python -m src.cli refresh --force # вручную scrape
.venv/bin/python -m src.cli weekly-refresh  # вручную full + stale-sweep
```

### 5.3 Веб-админка (опционально)

Если нужен веб-просмотр очереди — поднять Caddy с HTTPS:

```bash
sudo apt install -y caddy
```

```caddyfile
# /etc/caddy/Caddyfile
otz.example.com {
    reverse_proxy 127.0.0.1:8000
    basicauth /* {
        darya $2a$14$<bcrypt-hash>
    }
}
```

Затем добавить третий systemd unit:

```ini
# /etc/systemd/system/otz-admin.service
[Unit]
Description=otz-rent-finder — web admin (FastAPI)
After=network.target

[Service]
Type=simple
User=otz
WorkingDirectory=/opt/otz
ExecStart=/opt/otz/.venv/bin/python -m src.cli admin --host 127.0.0.1 --port 8000
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now otz-admin caddy
```

DNS A-запись `otz.example.com → <vps-ip>`. Caddy сам получит Let's Encrypt-сертификат.

### 5.4 Алерты о падении сервисов (OnFailure → Telegram)

Все три юнита (`otz-monitor`, `otz-nightly-refresh`, `otz-daily-summary`) несут
`OnFailure=otz-notify-failure@%n.service`. Когда юнит переходит в состояние `failed`,
systemd запускает инстанс шаблона `otz-notify-failure@.service`, который выполняет
`otz notify-failure --service <имя-юнита>` и шлёт 🚨-сообщение получателям бота.
Юниты ставит `scripts/vps_install.sh` (heredoc, перезапись на месте — идемпотентно).

**Когда срабатывает:**

- `otz-nightly-refresh`, `otz-daily-summary` (oneshot) — алерт на **каждый** упавший запуск.
  `ExecStartPost=-…ship-log` с дефисом — падение шипинга логов алерт НЕ триггерит.
- `otz-monitor` — алерт только когда рестарты **исчерпаны**: `Restart=on-failure` +
  `StartLimitBurst=5` / `StartLimitIntervalSec=600`. То есть 5 стартов за 10 минут →
  systemd сдаётся, юнит уходит в `failed`, летит алерт. На каждый одиночный рестарт
  алерта нет. Важно: дефолтное окно 10 с с нашим `RestartSec=30` не сработало бы
  никогда — потому значения заданы явно. Если monitor умирает реже, чем раз в ~2 мин,
  лимит не выбирается и он рестартует вечно (это видно в утреннем ship-log monitor.log).

**После алерта monitor лежит** — чинить и поднимать руками:

```bash
journalctl -u otz-monitor -n 100            # понять причину
sudo systemctl reset-failed otz-monitor     # сбросить счётчик стартов
sudo systemctl restart otz-monitor
```

**Smoke-тест** (после deploy + `systemctl daemon-reload`):

```bash
sudo systemctl start otz-notify-failure@smoke.service
# в Telegram придёт «🚨 smoke упал…»; лог: tail /var/log/otz/notify-failure.log
```

**Заглушить на время мейнтенанса:**

```bash
sudo systemctl stop otz-monitor             # чистый stop OnFailure НЕ триггерит
sudo systemctl stop otz-nightly-refresh.timer otz-daily-summary.timer
# либо полностью замьютить алерты (все инстансы шаблона):
sudo systemctl mask otz-notify-failure@.service
sudo systemctl unmask otz-notify-failure@.service   # после работ — не забыть!
```

---

## 6. Безопасность

| Что | Действие |
|---|---|
| `.env` с секретами | `chmod 600 /opt/otz/.env`. Не комитить в git (уже в `.gitignore`). |
| `otz_monitor.session` | `chmod 600`. Это пароль от твоего Telegram — если утечёт, кто угодно может войти. |
| `ADMIN_PASSWORD` | Сменить с дефолтного. 12+ символов, случайный. |
| SSH | Только ключи, parolь отключить: `/etc/ssh/sshd_config` → `PasswordAuthentication no`. |
| Firewall | `ufw allow OpenSSH`. Если используешь Caddy — `ufw allow 80,443`. Больше ничего наружу. |
| Fail2ban (опц.) | `apt install fail2ban`, default config закрывает brute-force SSH. |
| Обновления | `apt install unattended-upgrades` — авто-патчи security. |

---

## 7. Стоимость

| Услуга | Цена |
|---|---|
| Hetzner CX22 | €4.51/мес ≈ **$5** |
| Backblaze B2 backup | <$0.01/мес |
| Anthropic API (Claude Haiku 4.5 за валидацию ~30 матчей/мес) | ~$0.30/мес |
| Домен (если нужна админка) | $10/год = $0.80/мес |
| **Итого** | **~$6/мес** без админки, **~$7/мес** с админкой |

---

## 8. Контрольный лист переключения с локалки на VPS

- [ ] VPS заказан, SSH работает.
- [ ] Раздел 1.2-1.3 выполнен.
- [ ] Код перенесён, venv + playwright + xvfb работают.
- [ ] `.env` и `otz_monitor.session` скопированы.
- [ ] `otz stats` отвечает; `xvfb-run otz scrape halooglasi --max-pages=1` отрабатывает.
- [ ] systemd units `otz-monitor`, `otz-weekly.timer` enabled и `status` зелёный.
- [ ] Бэкап cron работает (запустить вручную, проверить файл в B2).
- [ ] **Локальный** `otz monitor` остановлен.
- [ ] Дарья форвардит тестовый пост в @OtzRentBelgrad → через минуту-две приходит в @Darya_kralj.
- [ ] Через 7 дней — журналы `weekly.log` показывают что full-refresh отработал.

---

## 9. Аварийные сценарии

| Проблема | Что делать |
|---|---|
| Monitor падает crash-loop | `journalctl -u otz-monitor -n 100` — посмотреть стек. Перезапустить `systemctl restart otz-monitor`. Если падает на Telethon — сессия сжата, нужен `otz auth` локально и перекопирование `.session`. |
| Halooglasi возвращает 0 previews | CF поднял уровень защиты для IP VPS. Подождать 6-12 часов или подключить residential proxy ($5/мес WebShare). Проверить `xvfb-run otz scrape halooglasi --max-pages=1 --no-photos`. |
| Anthropic API overloaded долго | Уже встроено 4 retry с backoff. Если совсем мёртвый — matcher работает без LLM (CLIP+text only). Опционально — сменить модель на Sonnet 4.6 в `src/matching/llm_validator.py:MODEL`. |
| Дарья не получает сообщения | `otz deliver-all` — вручную перешлёт все PENDING. Если ошибка `Cannot resolve TG_DELIVERY_TARGET` — Дарья не контакт юзербота. Либо она пишет первая, либо TG_DELIVERY_TARGET=me и Дарья сама смотрит Saved. |
| Место кончилось | Проверить `du -sh /opt/otz/data/*`. Скорее всего photos раздулись. Очистить старые: `find /opt/otz/data/listings -mtime +60 -delete`. |
| Полный завал — нужно откатиться | Скачать последний backup из B2 → распаковать → `systemctl stop otz-monitor` → подменить `/opt/otz/data/otz.db` → restart. |

### Обслуживание SQLite: ручной VACUUM

Ночной шаг prune (`NIGHTLY_PRUNE_EMBEDDINGS=1`) обнуляет `photo_embeddings` у объявлений,
неактивных >30 дней, но **файл otz.db от этого не уменьшается** — SQLite лишь помечает
страницы свободными и переиспользует их. Вернуть место на диске может только ручной VACUUM.
VACUUM никогда не автоматизируем: он переписывает весь файл, держит долгую блокировку и
без бэкапа опасен при обрыве. Порядок строго такой:

```bash
sudo systemctl stop otz-monitor          # 1. остановить монитор (и другие писатели)
cp /opt/otz/data/otz.db /opt/otz/data/otz.db.bak-$(date +%F)   # 2. бэкап обязателен
sqlite3 /opt/otz/data/otz.db "VACUUM;"   # 3. собственно сжатие
sudo systemctl start otz-monitor         # 4. вернуть монитор
```

---

## Готовность к Phase 5

Перед стартом убедиться:

| | |
|---|---|
| ☐ | VPS заказан, IP известен |
| ☐ | Domain (если нужна админка) куплен |
| ☐ | Backblaze B2 аккаунт + bucket |
| ☐ | `Darya_kralj` добавлена в контакты твоего Telegram-userbot аккаунта (иначе delivery 400) |
| ☐ | Локальный `otz monitor` работает стабильно ≥1 неделю |

Когда галочки стоят — пиши «деплоим», прохожу с тобой шаги 1-6.
