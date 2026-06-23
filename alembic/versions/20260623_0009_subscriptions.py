"""C2 — saved-search subscriptions: subscriptions + subscription_matches.

A visitor saves filter criteria (district/price/rooms/ВНЖ/белый картон) and is notified — by
e-mail or Telegram — when a NEW public listing matches. subscription_matches is the idempotency
ledger (UNIQUE(subscription_id, site_listing_id)) so a listing is notified at most once per
subscription, ever.

Revision ID: 0009_subscriptions
Revises: 0008_listing_dup_group
Create Date: 2026-06-23
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009_subscriptions"
down_revision: str | Sequence[str] | None = "0008_listing_dup_group"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "subscriptions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("criteria", sa.JSON(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),  # "email" | "telegram"
        sa.Column("email", sa.Text(), nullable=True),
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=True),
        sa.Column("token", sa.String(length=64), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint("token", name="uq_subscriptions_token"),
    )
    op.create_index("ix_subscriptions_is_active", "subscriptions", ["is_active"])

    op.create_table(
        "subscription_matches",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("subscription_id", sa.Integer(), nullable=False),
        sa.Column("site_listing_id", sa.Integer(), nullable=False),
        sa.Column(
            "notified_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"], ["subscriptions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["site_listing_id"], ["site_listings.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "subscription_id", "site_listing_id", name="uq_subscription_match"
        ),
    )


def downgrade() -> None:
    op.drop_table("subscription_matches")
    op.drop_index("ix_subscriptions_is_active", table_name="subscriptions")
    op.drop_table("subscriptions")
