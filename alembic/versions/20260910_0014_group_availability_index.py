"""Cover public apartment availability lookups without scanning scraper payloads."""

from collections.abc import Sequence

from alembic import op

revision: str = "0014_group_availability_index"
down_revision: str | Sequence[str] | None = "0013_delivery_outcomes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_listings_group_availability", "listings",
        ["dup_group_id", "is_active", "link_status", "deactivated_at", "absence_streak"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_listings_group_availability", table_name="listings")
