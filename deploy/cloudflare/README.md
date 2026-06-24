# Cloudflare in front of rentbelgrad.com — overview & policy

> Stage S Wave 3 («Shield»). Зачем: RU-origin (`90.156.254.0/24`) → переменная доступность из части
> мобильных сетей/стран. Cloudflare anycast-edge: клиент коннектится к ближайшему PoP (доступен
> ~отовсюду), origin-IP скрыт, TLS на edge, кэш, DDoS-защита. Это и есть фикс «доступно с любых сетей».
>
> **Живая выкатка — по [`RUNBOOK.md`](RUNBOOK.md)** (совместно, каждый шаг подтверждается). Этот файл —
> карта решений и политика кэша/WAF. Подготовка в репозитории (agent-doable) уже сделана:
> - `deploy/Caddyfile` — закомментированные `tls …` (Origin CA cert) под обоими проксируемыми хостами (S3-1).
> - `scripts/cf_ufw_allowlist.sh` — origin-lock 80/443 только на CF-диапазоны (S3-2).
> - `src/clientip.py` + `TRUST_CLOUDFLARE` — реальный клиентский IP из `CF-Connecting-IP` (S3-3).

## Архитектура (после выкатки)

```
клиент ──TLS──> Cloudflare edge (anycast, ближайший PoP) ──TLS(Full Strict, Origin cert)──> Caddy(:443)
                                                                                              ├─ /static,/photos → file_server
                                                                                              └─ /*             → otz-site(:8001) / otz-admin(:8000)
origin 90.156.254.159: ufw 80/443 ТОЛЬКО от CF-диапазонов (прямой заход снаружи → отказ → origin скрыт)
```

## Решения (из `DECISIONS-S.md`)
- **D3** — CF-аккаунт + Spaceship-доступ есть. Регистратор `launch1/launch2.spaceship.net` → NS меняем на CF.
- **D4** — `admin.rentbelgrad.com` тоже **proxied** (иначе admin-сабдомен светит origin-IP — подтверждено
  диагностикой S0-2: оба резолвятся в один IP). За CF: **bypass-кэш** + IP-allowlist (WAF) или CF Access;
  app-side basic-auth + G2-lockout остаются.
- **TLS** — **Full (Strict)** + **Origin CA cert** (CF доверяет своему Origin-cert в Strict). Публичный
  ACME HTTP-01 под CF-прокси ломается → переходим на Origin-cert (`tls …` в Caddyfile, S3-1).

## Cache policy (CF Cache Rules)
| Путь | Политика | Почему |
|------|----------|--------|
| `/static/*` | **Cache Everything**, длинный Edge TTL, уважать origin `Cache-Control` | ассеты фингерпринтованы `?v={asset_v}` → иммутабельны; Caddy уже шлёт `max-age=31536000, immutable`. |
| `/photos/*` | **Cache Everything**, Edge TTL ~1 нед | фото иммутабельны; Caddy шлёт `max-age=604800`. |
| `/`, `/catalog`, `/apt/*`, `/vnj`, `/how`, `/calculator`, `/map` | **Bypass** | featured/leads-динамика + в app уже TTL-кэш (5 мин). HTML не кэшируем на edge. |
| `/api/map.json` | короткий Edge TTL (~60с) — опц. | дёшево, меняется редко. |
| `POST /lead`, `POST /subscribe`, `/healthz` | **не кэшировать** | запись/health. |
| `admin.rentbelgrad.com/*` | **Bypass cache** + (CF Access ИЛИ WAF IP-allowlist) | админка не кэшируется и закрыта. |

## WAF / security
- **Origin lock** (главное сокрытие origin): `scripts/cf_ufw_allowlist.sh --apply` — 80/443 только от CF.
- **admin**: WAF custom rule «block if not in IP allowlist» ИЛИ CF Access (Zero Trust). App-side basic-auth +
  G2 per-IP lockout остаются как defense-in-depth.
- **Real client IP**: приложение читает `CF-Connecting-IP` при `TRUST_CLOUDFLARE=1` (S3-3) → rate-limit
  `/lead`+`/subscribe` и G2-lockout кейятся на реальном клиенте, а не на CF-edge. **Без этого все клиенты
  схлопываются в один CF-IP-бакет.** Включать флаг ТОЛЬКО когда origin уже залочен на CF (иначе заголовок
  спуфится прямым заходом).
- **CF Web Analytics** = on (бесплатно, cookieless → cookie-баннер не нужен, совпадает с «без аккаунтов»).
- Edge: Always Use HTTPS = on; Min TLS 1.2; Brotli on; HTTP/3 on; HSTS — **после** проверки (необратимо на max-age).

## Откат (по слоям)
- **grey-cloud** (снять прокси с DNS-записи) → трафик идёт мимо CF, прямо на origin.
- **вернуть NS** у Spaceship → выход из CF целиком.
- **origin-lock** временно ослабить: `sudo ufw allow 80,443/tcp` (для прямой отладки), потом re-apply.
