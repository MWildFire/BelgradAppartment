"""Stage T «Insight & Integrity» — first-party analytics (DATA_CONTRACT-T §A.1).

site_events — append-only телеметрия публичного сайта: pageview/lead_submit/subscribe_submit.
Приватность заложена в схему: колонка visitor = sha256(daily_salt‖ip‖ua)[:16] — сырые IP/UA
НЕ хранятся НИКОГДА, а daily_salt делает уников несвязываемыми между днями.
site_stats_daily — nightly-агрегаты с PK (day, metric, key): идемпотентный upsert-таргет,
живёт вечно, пока raw-события ротируются retention-ом.
leads получает 4 nullable-колонки атрибуции (utm_* + referrer) — снимок первого касания.

Revision ID: 0010_analytics
Revises: 0009_subscriptions
Create Date: 2026-07-02
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010_analytics"
down_revision: str | Sequence[str] | None = "0009_subscriptions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "site_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),  # 'pageview' | 'lead_submit' | 'subscribe_submit'
        sa.Column("path", sa.Text(), nullable=False),  # нормализованный: '/apt/{id}' → '/apt' + apt_id
        # Намеренно БЕЗ FK на site_listings: события — исторический факт; снятие квартиры
        # с сайта не должно ни трогать телеметрию, ни блокироваться ею.
        sa.Column("apt_id", sa.Integer(), nullable=True),
        sa.Column("visitor", sa.String(length=16), nullable=False),
        sa.Column("referrer", sa.Text(), nullable=True),  # только внешний, обрезан до ≤200 при записи
        sa.Column("utm_source", sa.Text(), nullable=True),
        sa.Column("utm_medium", sa.Text(), nullable=True),
        sa.Column("utm_campaign", sa.Text(), nullable=True),
        sa.Column("is_bot", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        # Строгий AUTOINCREMENT (не rowid-alias): retention удаляет старые строки, и id
        # удалённого события никогда не должен переиспользоваться живым.
        sqlite_autoincrement=True,
    )
    # §A.1: (ts) — retention/окна, (type, ts) — счётчики по типу, (apt_id, ts) — per-квартира.
    op.create_index("ix_site_events_ts", "site_events", ["ts"])
    op.create_index("ix_site_events_type_ts", "site_events", ["type", "ts"])
    op.create_index("ix_site_events_apt_id_ts", "site_events", ["apt_id", "ts"])

    op.create_table(
        "site_stats_daily",
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("metric", sa.Text(), primary_key=True),
        sa.Column("key", sa.Text(), primary_key=True),  # '' если измерение не применимо
        sa.Column("value", sa.Integer(), nullable=False),
    )

    # Batch mode: SQLite rebuild (как 0007/0008) — plain ALTER не гарантирован для всех случаев.
    with op.batch_alter_table("leads") as batch:
        batch.add_column(sa.Column("utm_source", sa.Text(), nullable=True))
        batch.add_column(sa.Column("utm_medium", sa.Text(), nullable=True))
        batch.add_column(sa.Column("utm_campaign", sa.Text(), nullable=True))
        batch.add_column(sa.Column("referrer", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("leads") as batch:
        batch.drop_column("referrer")
        batch.drop_column("utm_campaign")
        batch.drop_column("utm_medium")
        batch.drop_column("utm_source")

    op.drop_table("site_stats_daily")

    op.drop_index("ix_site_events_apt_id_ts", table_name="site_events")
    op.drop_index("ix_site_events_type_ts", table_name="site_events")
    op.drop_index("ix_site_events_ts", table_name="site_events")
    op.drop_table("site_events")
