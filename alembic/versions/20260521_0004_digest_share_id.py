"""Add digest_share_id to inbound_posts; widen unique constraint to allow multiple
inbound rows per Telegram message (one per YoNote share inside a digest post).

Revision ID: 0004_digest_share_id
Revises: 0003_clip_embeddings
Create Date: 2026-05-21
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_digest_share_id"
down_revision: str | Sequence[str] | None = "0003_clip_embeddings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("inbound_posts") as batch_op:
        batch_op.add_column(sa.Column("digest_share_id", sa.String(length=64), nullable=True))
        batch_op.drop_constraint("uq_inbound_channel_msg", type_="unique")
        # SQLite treats NULL values as distinct, so two ordinary rows with the same
        # (source, tg_message_id) and digest_share_id=NULL still pass uniqueness.
        # We rely on the existing watermark logic to avoid that case in practice.
        batch_op.create_unique_constraint(
            "uq_inbound_channel_msg",
            ["source_channel_id", "tg_message_id", "digest_share_id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("inbound_posts") as batch_op:
        batch_op.drop_constraint("uq_inbound_channel_msg", type_="unique")
        batch_op.create_unique_constraint(
            "uq_inbound_channel_msg",
            ["source_channel_id", "tg_message_id"],
        )
        batch_op.drop_column("digest_share_id")
