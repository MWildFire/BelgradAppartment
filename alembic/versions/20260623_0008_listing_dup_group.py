"""A4: cross-platform listing dedup — listings.dup_group_id + is_primary.

The periodic dedup job groups same-apartment listings (≥2 near-duplicate photos) and tags
each member with a shared dup_group_id + an is_primary flag. NULL dup_group_id = ungrouped;
is_primary defaults TRUE so every listing is its own representative until grouped & demoted.

Revision ID: 0008_listing_dup_group
Revises: 0007_featured_lead_fields
Create Date: 2026-06-23
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008_listing_dup_group"
down_revision: str | Sequence[str] | None = "0007_featured_lead_fields"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("listings") as batch:
        batch.add_column(sa.Column("dup_group_id", sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.text("1"))
        )


def downgrade() -> None:
    with op.batch_alter_table("listings") as batch:
        batch.drop_column("is_primary")
        batch.drop_column("dup_group_id")
