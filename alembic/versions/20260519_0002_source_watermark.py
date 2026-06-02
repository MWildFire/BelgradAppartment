"""source channel watermark + bigint message IDs

Revision ID: 0002_source_watermark
Revises: 0001_initial
Create Date: 2026-05-19
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_source_watermark"
down_revision: str | Sequence[str] | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("source_channels") as batch_op:
        batch_op.alter_column("tg_id", type_=sa.BigInteger())
        batch_op.add_column(
            sa.Column("start_date", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column("last_processed_message_id", sa.BigInteger(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("last_processed_at", sa.DateTime(timezone=True), nullable=True)
        )

    with op.batch_alter_table("inbound_posts") as batch_op:
        batch_op.alter_column("tg_message_id", type_=sa.BigInteger())
        batch_op.add_column(
            sa.Column("post_date", sa.DateTime(timezone=True), nullable=True)
        )

    with op.batch_alter_table("published_posts") as batch_op:
        batch_op.alter_column("tg_message_id", type_=sa.BigInteger())


def downgrade() -> None:
    with op.batch_alter_table("published_posts") as batch_op:
        batch_op.alter_column("tg_message_id", type_=sa.Integer())

    with op.batch_alter_table("inbound_posts") as batch_op:
        batch_op.drop_column("post_date")
        batch_op.alter_column("tg_message_id", type_=sa.Integer())

    with op.batch_alter_table("source_channels") as batch_op:
        batch_op.drop_column("last_processed_at")
        batch_op.drop_column("last_processed_message_id")
        batch_op.drop_column("start_date")
        batch_op.alter_column("tg_id", type_=sa.Integer())
