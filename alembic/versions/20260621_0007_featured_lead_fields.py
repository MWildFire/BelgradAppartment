"""Stage W2: featured «квартира недели» flag + lead↔apartment linkage.

Revision ID: 0007_featured_lead_fields
Revises: 0006_site_listings_leads
Create Date: 2026-06-21
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007_featured_lead_fields"
down_revision: str | Sequence[str] | None = "0006_site_listings_leads"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Batch mode: SQLite has no real ALTER ADD with constraint/FK — alembic rebuilds the table.
    with op.batch_alter_table("site_listings") as batch:
        batch.add_column(
            sa.Column("is_featured", sa.Boolean(), nullable=False, server_default=sa.text("0"))
        )
    # Partial index — only the (tiny) featured set is indexed, so «квартира недели» lookup is O(1).
    op.create_index(
        "ix_site_listings_featured",
        "site_listings",
        ["is_featured"],
        sqlite_where=sa.text("is_featured = 1"),
    )

    with op.batch_alter_table("leads") as batch:
        batch.add_column(sa.Column("site_listing_id", sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column("kind", sa.Text(), nullable=False, server_default="general")
        )
        # A lead may reference a specific apartment; SET NULL keeps the lead if the listing goes.
        batch.create_foreign_key(
            "fk_leads_site_listing_id",
            "site_listings",
            ["site_listing_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    op.drop_index("ix_site_listings_featured", table_name="site_listings")
    with op.batch_alter_table("site_listings") as batch:
        batch.drop_column("is_featured")
    with op.batch_alter_table("leads") as batch:
        # Drop FK implicitly via table rebuild when the column goes.
        batch.drop_column("site_listing_id")
        batch.drop_column("kind")
