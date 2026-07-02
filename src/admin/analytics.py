"""Admin analytics query layer — §A.5 context for GET /analytics (Stage T, T3-1).

Read-only агрегация: прошлые дни берутся из site_stats_daily (их закрыл nightly-rollup),
«сегодня» считается live из raw site_events — сегодняшние партиал-строки rollup-а игнорируются,
иначе live-подсчёт задвоил бы день. Bot-семантика зеркалит rollup §A.2: pageview/uniques —
is_bot=0, leads — как есть; live обязан давать те же цифры, что rollup запишет за этот день.
Уники за период = СУММА дневных уников: суточная соль делает visitor-хэши несвязываемыми
между днями по построению, поэтому «честных» периодных уников не существует — UI подписывает
методику явно.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from sqlalchemy import ColumnElement, Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from src.db.models import Listing, Match, SiteEvent, SiteListing, SiteStatsDaily

# Общий парсер домена (admin→site импорт допустим, обратный — нет): live-топ источников
# обязан сводить referrer к тому же ключу src_ref и с той же терпимостью к мусору
# (обрезка ≤200 на write-side может сломать синтаксис URL), что и nightly-rollup.
from src.site.analytics import ref_domain

# §A.5: ?days=7|30, всё прочее падает в дефолт (не в 500 — конвенция /published, /leads).
ANALYTICS_WINDOWS = (7, 30)
DEFAULT_DAYS = 7
# §A.5: top_apts ≤15, sources ≤15.
TOP_LIMIT = 15
# T3-2: окно бейджа «👁 7д» в /published.
LISTING_VIEWS_DAYS = 7


async def _stats_by_key(
    s: AsyncSession, metric: str, day_from: date, day_to: date
) -> dict[str, int]:
    """SUM(value) по key за [day_from, day_to) из site_stats_daily."""
    rows = await s.execute(
        select(SiteStatsDaily.key, func.sum(SiteStatsDaily.value))
        .where(
            SiteStatsDaily.metric == metric,
            SiteStatsDaily.day >= day_from,
            SiteStatsDaily.day < day_to,
        )
        .group_by(SiteStatsDaily.key)
    )
    return {k: int(v) for k, v in rows.all()}


async def _live_by_column(
    s: AsyncSession,
    column: InstrumentedAttribute[Any],
    today_start: datetime,
    *extra_where: ColumnElement[bool],
) -> dict[Any, int]:
    """COUNT(*) сегодняшних событий по column (NULL-ы отброшены); bot-фильтр — в extra_where."""
    rows = await s.execute(
        select(column, func.count())
        .where(
            SiteEvent.ts >= today_start,
            column.is_not(None),
            *extra_where,
        )
        .group_by(column)
    )
    return {k: int(v) for k, v in rows.all()}


async def views_7d_by_apt(s: AsyncSession, apt_ids: Sequence[int]) -> dict[int, int]:
    """Бейдж «👁 7д» для /published (T3-2): SUM(views_apt) per site_listings.id за 7 дней.

    Только агрегаты (day >= today-7, партиал-«сегодня» rollup-а входит, если есть) — live
    из raw НЕ добирается: Дарье нужен порядок величины, точная сводка живёт в /analytics.
    Квартиры без строк отсутствуют в результате — caller делает .get(id, 0).
    """
    if not apt_ids:
        return {}
    day_floor = datetime.now(UTC).date() - timedelta(days=LISTING_VIEWS_DAYS)
    rows = await s.execute(
        select(SiteStatsDaily.key, func.sum(SiteStatsDaily.value))
        .where(
            SiteStatsDaily.metric == "views_apt",
            SiteStatsDaily.day >= day_floor,
            # key в агрегатах — TEXT (§A.1); фильтр строками сразу отсекает и мусорные ключи
            SiteStatsDaily.key.in_([str(i) for i in apt_ids]),
        )
        .group_by(SiteStatsDaily.key)
    )
    return {int(k): int(v) for k, v in rows.all()}


async def build_analytics(s: AsyncSession, days: int = DEFAULT_DAYS) -> dict[str, Any]:
    """Контекст дашборда строго по DATA_CONTRACT-T §A.5 (+ `today` для UI-пометки live-дня)."""
    today = datetime.now(UTC).date()
    day_floor = today - timedelta(days=days - 1)
    # SQLite хранит naive-UTC текст (см. _as_aware в routes.py) — граница дня тоже naive-UTC.
    today_start = datetime.combine(today, time.min)

    # --- ряд по дням: прошлые дни одной группировкой (day, metric) из агрегатов ---
    per_day: dict[str, dict[date, int]] = {"views_path": {}, "uniques": {}, "leads": {}}
    rows = await s.execute(
        select(SiteStatsDaily.day, SiteStatsDaily.metric, func.sum(SiteStatsDaily.value))
        .where(
            SiteStatsDaily.metric.in_(tuple(per_day)),
            SiteStatsDaily.day >= day_floor,
            SiteStatsDaily.day < today,  # сегодняшний партиал-роллап игнорируем — сегодня live
        )
        .group_by(SiteStatsDaily.day, SiteStatsDaily.metric)
    )
    for day, metric, value in rows.all():
        per_day[metric][day] = int(value)

    # --- «сегодня» live из raw; live обязан совпасть с тем, что rollup запишет за этот день ---
    human = SiteEvent.is_bot.is_(False)

    def _live_count(*where: ColumnElement[bool]) -> Select[tuple[int]]:
        return (
            select(func.count()).select_from(SiteEvent).where(SiteEvent.ts >= today_start, *where)
        )

    views_today = (await s.execute(_live_count(SiteEvent.type == "pageview", human))).scalar_one()
    # §A.2 leads — БЕЗ bot-фильтра (rollup считает lead_submit как есть, в отличие от pageview)
    leads_today = (await s.execute(_live_count(SiteEvent.type == "lead_submit"))).scalar_one()
    # §A.2 uniques — distinct visitor по ВСЕМ не-бот событиям дня (без фильтра по type)
    uniques_today = (
        await s.execute(
            select(func.count(func.distinct(SiteEvent.visitor))).where(
                SiteEvent.ts >= today_start,
                human,
            )
        )
    ).scalar_one()

    # Ряд плотный (нулевые дни не выпадают) — спарклайн честен к дырам в данных.
    series: list[dict[str, Any]] = [
        {
            "day": d,
            "views": views_today if d == today else per_day["views_path"].get(d, 0),
            "leads": leads_today if d == today else per_day["leads"].get(d, 0),
        }
        for d in (day_floor + timedelta(days=i) for i in range(days))
    ]

    views_total = sum(p["views"] for p in series)
    leads_total = sum(p["leads"] for p in series)
    cards = {
        "views": views_total,
        "uniques": sum(per_day["uniques"].values()) + int(uniques_today),
        "leads": leads_total,
        # conv = leads/views*100, div0 → 0.0 (контракт)
        "conv_pct": (leads_total / views_total * 100) if views_total else 0.0,
    }

    # --- топ-квартиры: агрегаты + live, ключ — site_listings.id ---
    apt_views: dict[int, int] = {}
    apt_leads: dict[int, int] = {}
    for target, metric in ((apt_views, "views_apt"), (apt_leads, "leads_apt")):
        for key, value in (await _stats_by_key(s, metric, day_floor, today)).items():
            if key.isdigit():  # key в агрегатах — TEXT; мусор молча выпадает
                target[int(key)] = target.get(int(key), 0) + value
    # bot-фильтр по §A.2: views_apt (pageview) — is_bot=0, leads_apt — как есть
    for target, where in (
        (apt_views, (SiteEvent.type == "pageview", human)),
        (apt_leads, (SiteEvent.type == "lead_submit",)),
    ):
        live = await _live_by_column(s, SiteEvent.apt_id, today_start, *where)
        for apt_id, value in live.items():
            target[apt_id] = target.get(apt_id, 0) + value

    # views desc → leads desc → id (детерминизм при равенстве); квартира с одними заявками
    # тоже видна (union ключей), а не исчезает из-за нуля просмотров.
    top_ids = sorted(
        set(apt_views) | set(apt_leads),
        key=lambda i: (-apt_views.get(i, 0), -apt_leads.get(i, 0), i),
    )[:TOP_LIMIT]

    # district/price подтягиваются цепочкой SiteListing→Match→Listing; событие — исторический
    # факт, квартира могла уйти с сайта → отсутствующая строка даёт None, не падение.
    info: dict[int, tuple[str | None, int | None]] = {}
    if top_ids:
        apt_rows = await s.execute(
            select(SiteListing.id, Listing.district, Listing.price_eur)
            .join(Match, Match.id == SiteListing.match_id)
            .join(Listing, Listing.id == Match.listing_id)
            .where(SiteListing.id.in_(top_ids))
        )
        info = {i: (district, price) for i, district, price in apt_rows.all()}
    top_apts: list[dict[str, Any]] = []
    for apt_id in top_ids:
        views = apt_views.get(apt_id, 0)
        leads = apt_leads.get(apt_id, 0)
        district, price_eur = info.get(apt_id, (None, None))
        top_apts.append(
            {
                "apt_id": apt_id,
                "district": district,
                "price_eur": price_eur,
                "views": views,
                "leads": leads,
                "ctr_pct": (leads / views * 100) if views else 0.0,
            }
        )

    # --- источники: (kind, key) → views; live-referrer сводится к домену как в rollup ---
    src: dict[tuple[str, str], int] = {}
    for kind, metric in (("utm", "src_utm"), ("ref", "src_ref")):
        for key, value in (await _stats_by_key(s, metric, day_floor, today)).items():
            src[(kind, key)] = src.get((kind, key), 0) + value
    live_utm = await _live_by_column(
        s, SiteEvent.utm_source, today_start, SiteEvent.type == "pageview", human
    )
    for key, value in live_utm.items():
        src[("utm", key)] = src.get(("utm", key), 0) + value
    live_ref = await _live_by_column(
        s, SiteEvent.referrer, today_start, SiteEvent.type == "pageview", human
    )
    for ref, value in live_ref.items():
        domain = ref_domain(ref)
        if domain:
            src[("ref", domain)] = src.get(("ref", domain), 0) + value
    sources = [
        {"kind": kind, "key": key, "views": views}
        for (kind, key), views in sorted(src.items(), key=lambda kv: (-kv[1], kv[0]))
    ][:TOP_LIMIT]

    return {
        "days": days,
        "cards": cards,
        "series": series,
        "top_apts": top_apts,
        "sources": sources,
        # вне контракта §A.5, но нужно шаблону: какой день ряда — live-«сегодня»
        "today": today,
    }
