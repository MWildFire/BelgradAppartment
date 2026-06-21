# DATA CONTRACT — дельта Stage W2

> Только изменения к `docs/web/DATA_CONTRACT.md`. Применять поверх Stage W. После мержа этапа — влить эти правки в основной `DATA_CONTRACT.md`.

## Изменённые/новые роуты

| Метод/роут | Изменение |
|---|---|
| `POST /lead` | принимает **опц. `site_listing_id: int`** (валидируется: существует и `is_public`); поле `kind` (`general`/`listing`); при наличии квартиры — письмо/нотис включают её сводку. Канал доставки: **e-mail на `SITE_LEAD_EMAIL_TO` (обяз.)** + бот-нотис Дарье (как было). Honeypot/CSRF/rate-limit — без изменений. |
| `GET /` (index) | контекст пополняется: `district_options` (для hero-дропдауна), `featured: SiteListingView | None` (реальная «квартира недели»). |
| `GET /vnj` | **новый** → `templates/vnj.html` («ВНЖ и договор», контент). |
| `GET /how` | **новый** → `templates/how.html` («Как работаем» + расшифровка комиссии 50%). |
| `GET /sitemap.xml` | **новый** → XML: статические страницы + все `is_public` `/apt/{id}`. |
| `GET /robots.txt` | **новый** → allow + ссылка на sitemap. |
| 404 | кастомный handler → `templates/404.html` (с Telegram-CTA и ссылкой в каталог). |
| `GET/POST /admin/leads` | **новый** (админка): список заявок + смена `status` (CSRF). |

## Новые Jinja-globals (доступны всем шаблонам через `app.py`)

| Global | Значение | Назначение |
|---|---|---|
| `telegram_url` | `SITE_TELEGRAM_URL` (=`https://t.me/Darya_kralj`) | кнопка «Написать в Telegram» в `base.html` (шапка/футер/липкая) — **F5** |
| `district_options` | курированный список районов (`data/districts.yaml` ∪ фасеты, со счётчиками) | дропдаун района в hero и фильтрах — **F2** |

## Изменения read-model / схемы

- `site_listings.is_featured: bool` — «квартира недели» (admin-тоггл).
- `leads.site_listing_id: int | None` (FK), `leads.kind: str` (`general`/`listing`/`subscription`).
- `readmodel.featured_one() -> SiteListingView | None` — featured-приоритет, иначе newest public, иначе None.
- `readmodel.apartment_summary(site_listing_id) -> dict | None` — сводка квартиры для письма/нотиса.

## OpenGraph на `/apt/{id}` (в `<head>` `apt.html`)

`og:title` = «{price_eur}€ · {district}» · `og:description` (кратко) · `og:image` = первое фото (абсолютный URL через `SITE_PUBLIC_BASE_URL`) · `og:url` = canonical. Цель — богатые превью при шеринге ссылки в Telegram.

## Новые env-ключи

См. `env.additions-w2.txt`: `SITE_TELEGRAM_URL`, `SITE_LEAD_EMAIL_TO`, `SITE_SMTP_HOST/PORT/USER/PASSWORD/FROM/STARTTLS`.
