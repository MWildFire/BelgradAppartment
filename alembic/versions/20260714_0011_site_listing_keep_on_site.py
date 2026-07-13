"""Liveness pin: site_listings.keep_on_site — a card the sweep never auto-hides (D4).

Revision ID: 0011_keep_on_site
Revises: 0010_analytics
Create Date: 2026-07-14
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0011_keep_on_site"
down_revision: str | Sequence[str] | None = "0010_analytics"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Batch mode: SQLite has no real ALTER ADD of a NOT NULL column, so alembic rebuilds the table.
    # server_default 0 → every existing card is unpinned after the upgrade.
    with op.batch_alter_table("site_listings") as batch:
        batch.add_column(
            sa.Column("keep_on_site", sa.Boolean(), nullable=False, server_default=sa.text("0"))
        )


def downgrade() -> None:
    with op.batch_alter_table("site_listings") as batch:
        batch.drop_column("keep_on_site")
