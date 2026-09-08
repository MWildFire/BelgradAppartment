"""Add durable delivery claims without replaying historical sends."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0013_delivery_outcomes"
down_revision: str | Sequence[str] | None = "0012_refresh_evidence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _outcome_columns() -> list[sa.Column]:
    return [
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("claim_token", sa.String(64), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    op.add_column("subscriptions", sa.Column("notification_cursor_at", sa.DateTime(timezone=True)))
    op.create_table(
        "match_deliveries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("match_id", sa.Integer(), sa.ForeignKey("matches.id", ondelete="CASCADE"), nullable=False),
        sa.Column("recipient_id", sa.BigInteger(), nullable=False),
        sa.Column("message_ids", sa.JSON(), nullable=False, server_default="[]"),
        *_outcome_columns(),
        sa.UniqueConstraint("match_id", "recipient_id", name="uq_match_recipient"),
    )
    op.create_table(
        "subscription_deliveries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("subscription_id", sa.Integer(), sa.ForeignKey("subscriptions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("site_listing_id", sa.Integer(), sa.ForeignKey("site_listings.id", ondelete="CASCADE"), nullable=False),
        *_outcome_columns(),
        sa.UniqueConstraint("subscription_id", "site_listing_id", name="uq_subscription_delivery"),
    )


def downgrade() -> None:
    raise RuntimeError("Use application rollback; do not discard delivery evidence automatically")
