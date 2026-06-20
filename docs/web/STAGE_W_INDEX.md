# Stage W — комплект документов для проекта

Куда что класть в репозитории `otz-rent-finder` и что это.

## Новые документы (создал я)

| Файл в комплекте | Куда в репо | Что это |
|---|---|---|
| `docs/superpowers/plans/2026-06-20-public-web-catalog.md` | тот же путь | **Главный план** для Claude Code: Stage W, multi-agent, TDD, ревью, debug, 6 волн, deploy-runbook, DoD. Путь — по конвенции superpowers (`writing-plans`). |
| `docs/web/ARCHITECTURE.md` | тот же путь | ADR: почему сайт на той же VPS (FastAPI), а не Astro/Vercel; развязка через `site_listings`/`leads`; топология Caddy; безопасность. |
| `docs/web/DATA_CONTRACT.md` | тот же путь | Контракт backend↔frontend (read-model `SiteListingView`, карта роут→шаблон→контекст, фильтры, правила фото/ссылок). Позволяет параллельным агентам не пересекаться. |
| `deploy/Caddyfile` | тот же путь | Стартер reverse-proxy с авто-TLS (заменить домен/пути). |
| `deploy/otz-site.service` | тот же путь | Стартер systemd-юнита публичного app (localhost:8001). |
| `env.additions.txt` | дописать в `.env.example` (и в `/opt/otz/.env` руками) | Новые env-ключи сайта. |

## Дизайн-архив (положил **без изменений**, как просил)

| Файл в комплекте | Куда в репо | Примечание |
|---|---|---|
| `design/tokens.css` | `design/tokens.css` **и** скопировать в `src/site/static/css/tokens.css` (Wave 3) | Дизайн-токены «Belgrade Editorial». Рабочий файл. |
| `design/index.html` | `design/index.html` | Визуальный эталон главной — источник вёрстки. |
| `design/RentBelgrad Design System.html` | `design/RentBelgrad Design System.html` | Вайрфреймы всех экранов (бренд/ТГ/веб/апп/UI-kit). |
| `design/DESIGN_PLAN.md` | `design/DESIGN_PLAN.md` | Оригинальный план Claude Design — **не менял**. |
| `design/reference.png` | `design/reference.png` | Скриншот-референс. |

> ⚠️ **Важно про `design/DESIGN_PLAN.md`:** его раздел 0 (анти-slop закон), палитра, типографика и «вкусовые» ревью-скиллы (`taste`, `impeccable`) — **в силе**. Но его стек (Astro/Vercel/Vercel Blob/Drizzle) и Фаза 3 (повторный ингест из Telegram) **отменены** новым планом: сайт ставится на ту же VPS на FastAPI и читает существующую БД без ингеста. Обоснование — в `docs/web/ARCHITECTURE.md`. Файл оставлен как есть намеренно (ты просил без изменений) — расхождение зафиксировано здесь и в шапке плана.

## Что НЕ вошло (создаётся агентами в ходе этапа)
`src/site/*` (app, routes, readmodel, шаблоны, static, js), миграция `alembic/versions/0006_*`, тесты `tests/site/*`, `tests/e2e/site/*`, `docs/web/DECISIONS.md` (заполняется в Wave 0), правки `src/admin/*`. Всё это пишется по плану с TDD — в комплект не кладётся.

## Порядок старта
1. Установить Superpowers (см. план §0.1) и создать worktree ветки `feature/public-web-catalog`.
2. Прочитать `ARCHITECTURE.md` → `DATA_CONTRACT.md` → план.
3. Wave 0: brainstorm решений (`docs/web/DECISIONS.md`) + DNS A-запись сабдомена → `90.156.254.159`.
4. Дальше по волнам.
