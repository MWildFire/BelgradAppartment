"""Add CLIP photo_embeddings BLOB column to listings and inbound_posts.

Revision ID: 0003_clip_embeddings
Revises: 0002_source_watermark
Create Date: 2026-05-19
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_clip_embeddings"
down_revision: str | Sequence[str] | None = "0002_source_watermark"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("listings") as batch_op:
        batch_op.add_column(sa.Column("photo_embeddings", sa.LargeBinary(), nullable=True))

    with op.batch_alter_table("inbound_posts") as batch_op:
        batch_op.add_column(sa.Column("photo_embeddings", sa.LargeBinary(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("inbound_posts") as batch_op:
        batch_op.drop_column("photo_embeddings")
    with op.batch_alter_table("listings") as batch_op:
        batch_op.drop_column("photo_embeddings")
