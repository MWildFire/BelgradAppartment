"""Stage W public catalog: site_listings overlay + leads form table.

Revision ID: 0006_site_listings_leads
Revises: 0005_indexes_dedup_metrics
Create Date: 2026-06-20
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006_site_listings_leads"
down_revision: str | Sequence[str] | None = "0005_indexes_dedup_metrics"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "site_listings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("match_id", sa.Integer(), nullable=False),
        sa.Column("is_public", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("is_for_vnj", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("is_for_white_card", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("channel_post_url", sa.Text(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["match_id"], ["matches.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("match_id", name="uq_site_listings_match_id"),
    )
    op.create_index("ix_site_listings_is_public", "site_listings", ["is_public"])

    op.create_table(
        "leads",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("contact", sa.Text(), nullable=False),
        sa.Column("budget_eur", sa.Integer(), nullable=True),
        sa.Column("districts", sa.Text(), nullable=True),
        sa.Column("rooms", sa.Text(), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False, server_default="site"),
        sa.Column("status", sa.Text(), nullable=False, server_default="new"),
    )
    op.create_index("ix_leads_created_at", "leads", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_leads_created_at", table_name="leads")
    op.drop_table("leads")
    op.drop_index("ix_site_listings_is_public", table_name="site_listings")
    op.drop_table("site_listings")
