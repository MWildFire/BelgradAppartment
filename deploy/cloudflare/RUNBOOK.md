# Cloudflare live rollout — RUNBOOK (Stage S Wave 3b)

> **Совместная выкатка.** Каждый шаг подтверждается (как ssh-шаги в Stage R/W). Делать по порядку.
> Контекст/политика — [`README.md`](README.md). Откат — внизу. Origin: `90.156.254.159`. Домен:
> `rentbelgrad.com` (+ `www`, `admin`). Регистратор: **Spaceship** (NS `launch1/launch2.spaceship.net`).
>
> **Точка невозврата** — только смена NS (шаг 3), и она обратима (вернуть старые NS). Всё до неё — без риска.

## 0. Перед стартом (де-риск, не обязательно)
Прогнать S0-2 с не-RU мобильной сети/прокси, записать в `DECISIONS-S.md §S0-2` (опционально — архитектурный
кейс уже доказан). Снять бэкап как обычно (он и так в Wave 6).

## 1. Origin CA cert (на VPS) — ДО смены NS, без влияния на текущий трафик
1. CF dashboard → **SSL/TLS → Origin Server → Create Certificate**.
   - Hostnames: `rentbelgrad.com` **и** `*.rentbelgrad.com` (wildcard покрывает `www` + `admin`).
   - Key type: RSA или ECDSA; срок 15 лет ок. Скачать **cert** (PEM) + **private key**.
2. На VPS положить их (root):
   ```sh
   sudo mkdir -p /etc/caddy/origin
   sudo tee /etc/caddy/origin/rentbelgrad.pem  >/dev/null   # вставить cert PEM
   sudo tee /etc/caddy/origin/rentbelgrad.key  >/dev/null   # вставить private key
   sudo chmod 600 /etc/caddy/origin/rentbelgrad.key
   sudo chmod 644 /etc/caddy/origin/rentbelgrad.pem
   ```
3. **НЕ переключать Caddy на них прямо сейчас** (текущий публичный доступ ещё прямой, не через CF — ACME
   ещё работает). Раскомментируем `tls …` в Caddyfile на шаге 4, синхронно с включением прокси. (Можно
   подготовить файл `/etc/caddy/Caddyfile` с раскомментированными строками, но `reload` сделать после DNS.)

## 2. Добавить домен в Cloudflare
1. CF → Add a Site → `rentbelgrad.com` → **Free** plan.
2. CF просканит DNS и предложит записи. CF выдаст **2 nameserver'а** (вида `x.ns.cloudflare.com`). Запиши их.
3. Проверь, что в CF DNS есть записи (создадим/поправим на шаге 4): пока можно оставить как импортировалось.

## 3. Сменить NS у Spaceship  ⚠️ точка невозврата (обратимая)
1. Spaceship → домен `rentbelgrad.com` → Nameservers → заменить на 2 CF-NS из шага 2.
2. Дождаться `Active` в CF (минуты–часы; обычно быстро). `dig NS rentbelgrad.com` должен показать CF-NS.
   - Откат на любом этапе: вернуть `launch1/launch2.spaceship.net` у Spaceship.

## 4. DNS-записи + SSL mode в CF, и переключить Caddy на Origin-cert
1. CF → DNS:
   - `A  rentbelgrad.com → 90.156.254.159` — **Proxied (оранжевое облако)**.
   - `CNAME www → rentbelgrad.com` (или `A www → 90.156.254.159`) — **Proxied**.
   - `A  admin → 90.156.254.159` — **Proxied** (D4).
2. CF → **SSL/TLS → Overview → Full (Strict)**. (Origin-cert доверен CF в Strict.)
3. На VPS — переключить Caddy на Origin-cert (S3-1):
   - В `/etc/caddy/Caddyfile` раскомментировать `tls /etc/caddy/origin/rentbelgrad.pem /etc/caddy/origin/rentbelgrad.key`
     под **обоими** хостами `rentbelgrad.com` и `admin.rentbelgrad.com`.
   - `sudo systemctl reload caddy` → `journalctl -u caddy -n 30` (без ошибок TLS).
   - Локальная проверка origin: `curl -kI --resolve rentbelgrad.com:443:127.0.0.1 https://rentbelgrad.com` (Caddy отвечает Origin-cert'ом).

## 5. Edge-настройки CF
- SSL/TLS → Edge Certificates: **Always Use HTTPS = on**, **Min TLS 1.2**, **HTTP/3 (QUIC) = on**.
- Speed/Content: **Brotli = on**.
- **HSTS — включить ПОЗЖЕ** (шаг 9, после полной проверки; max-age необратим на срок действия).

## 6. Cache Rules (CF → Rules → Cache Rules) — политика из README
- `/static/*`, `/photos/*` → **Cache Everything**, Edge TTL «Respect origin» (ассеты иммутабельны).
- HTML (`/`, `/catalog`, `/apt/*`, `/vnj`, `/how`, `/calculator`, `/map`) → **Bypass cache**.
- `/api/map.json` → Cache, Edge TTL ~60s (опц.).
- `POST /lead`, `POST /subscribe`, `/healthz` → **Bypass** (не кэшировать).
- `admin.rentbelgrad.com/*` → **Bypass cache**.

## 7. Admin защита (D4)
Один из:
- **CF Access** (Zero Trust → Application для `admin.rentbelgrad.com`, политика = твой e-mail/IP), ИЛИ
- **WAF custom rule**: `(http.host eq "admin.rentbelgrad.com" and not ip.src in {<твои IP>})` → **Block**.
App-side basic-auth + G2-lockout остаются (defense-in-depth).

## 8. Включить CF-Connecting-IP в приложении (S3-3) — ТОЛЬКО после origin-lock (шаг 9)
> Важно: `TRUST_CLOUDFLARE=1` включать **после** того, как origin залочен на CF (шаг 9). Иначе заголовок
> `CF-Connecting-IP` можно подделать прямым заходом на ещё-открытый origin.
1. В серверном `/opt/otz/.env` добавить `TRUST_CLOUDFLARE=1`.
2. `sudo systemctl restart otz-site otz-admin` → проверить логи чистые.
3. Проверка: rate-limit `/lead` и G2-lockout теперь кейятся на реальном клиентском IP (см. шаг 10).

## 9. Origin lock (скрыть origin) — `scripts/cf_ufw_allowlist.sh`
1. Сначала **dry-run**: `scripts/cf_ufw_allowlist.sh` (покажет команды, ничего не меняет; убедись, что
   видит ~20–30 CF-диапазонов и что SSH-22 ALLOW существует).
2. Применить: `sudo scripts/cf_ufw_allowlist.sh --apply` (скрипт откажется, если нет SSH-ALLOW — анти-локаут).
3. `sudo ufw status numbered` — 80/443 allow от CF + deny остального; 22/SSH и Outline нетронуты.
4. Теперь вернуться к шагу 8 (включить `TRUST_CLOUDFLARE=1`), т.к. origin уже залочен.

## 10. Проверка глобальной доступности (DoD)
- С **не-RU** мобильной сети и/или прокси-локаций: `https://rentbelgrad.com` = **200**, TLS-цепочка ок,
  OG-превью, формы работают.
- `dig rentbelgrad.com` показывает **CF-IP** (не `90.156.254.159`).
- Прямой `curl -I --resolve rentbelgrad.com:443:90.156.254.159 https://rentbelgrad.com` **снаружи** (не с VPS)
  → **отказ/таймаут** (origin-lock работает).
- `/healthz` = 200; admin закрыт (CF Access/WAF) и не кэшируется (`cf-cache-status: BYPASS/DYNAMIC`).
- Rate-limit: серия `POST /lead` с одного клиента ограничивается, с другого — нет (кейится на реальном IP).
- CF → **Web Analytics = on**.

## 11. HSTS (последним, осознанно)
После полной проверки — CF → SSL/TLS → Edge Certificates → **HSTS Enable** (max-age, includeSubDomains).
Помни: необратимо на срок max-age (браузеры запомнят). У Caddy HSTS-заголовок уже стоит.

---
## Откат (по слоям)
1. **grey-cloud**: CF → DNS → снять прокси (серое облако) с записи → трафик мимо CF (origin-lock тогда
   временно ослабить: `sudo ufw allow 80,443/tcp`).
2. **вернуть NS** у Spaceship → выход из CF целиком.
3. **TRUST_CLOUDFLARE=0** + restart, если нужно вернуть старое извлечение IP.
4. Caddy: закомментировать `tls …` обратно (вернётся ACME) — только если ушли из-за CF полностью.
