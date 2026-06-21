# Stage W2 — комплект документов

Положить в репозиторий `otz-rent-finder`. Дерево уже как в репо — распаковать в корень.

| Файл в комплекте | Куда в репо | Что это |
|---|---|---|
| `docs/superpowers/plans/2026-06-21-web-conversion-polish.md` | тот же путь | **Главный план** Stage W2: 5 обязательных фиксов + конверсионные фичи, multi-agent, TDD, ревью, deploy, DoD. |
| `docs/web/DATA_CONTRACT-W2.md` | тот же путь | Дельта к контракту backend↔frontend (новые роуты, поля заявки, globals, env). Влить в `DATA_CONTRACT.md` после мержа. |
| `env.additions-w2.txt` | дописать в `.env.example` (и в `/opt/otz/.env` руками) | Новые ключи: Telegram-URL, e-mail заявок, SMTP. |
| `INDEX-W2.md` | — | этот файл |

## Обязательные фиксы (приоритет этапа)
- **F1** заголовок «ПО-ЧЕЛОВЕЧЕСКИ» → **«КЛЮЧИ ОТ БЕЛГРАДА»** (дефолт; альт: «СВОИ В БЕЛГРАДЕ», «ДОМ ПОД ВНЖ»)
- **F2** район → выпадающий список районов Белграда (combobox + `<select>`-fallback) в hero и фильтрах
- **F3** стат комиссии `€0` → **`50%`** (расшифровка на «Как работаем»; точную формулу выверить у Дарьи)
- **F4** заявки уходят на **`d.mashukova@bk.ru`** (e-mail) + бот-нотис; lead не теряется при сбое
- **F5** «Написать в Telegram» → **`https://t.me/Darya_kralj`** на каждой странице (шапка + футер + липкая)

## Фичи (на мой вкус)
Заявка по конкретной квартире (форма с контекстом квартиры в письме) · реальная «Квартира недели» (admin-тоггл) · контентные страницы «ВНЖ и договор» / «Как работаем» · SEO (OpenGraph для превью в Telegram, sitemap, robots, 404) · admin leads inbox для триажа · (опц.) подписка на новые квартиры.

## Что НЕ вошло (создаётся агентами по плану с TDD)
`alembic/versions/0007_*`, новые шаблоны (`vnj.html`,`how.html`,`404.html`), `src/site/email.py`, `static/js/combobox.js`, правки `index.html`/`base.html`/`apt.html`/`_card.html`/`_filters.html`/`routes.py`/`leads.py`/`app.py`/`config.py`/admin, тесты, `docs/web/DECISIONS-W2.md` (Wave 0).

## Старт
1. Superpowers стоит; создать worktree `feature/web-conversion-polish`.
2. Wave 0: решения (копия заголовка, SMTP-релей + SPF/DKIM, точная формула комиссии) → `docs/web/DECISIONS-W2.md`.
3. Wave 1 (обязательные фиксы) → дальше по волнам.
