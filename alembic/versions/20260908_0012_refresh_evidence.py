"""Add verified refresh and absence evidence without inventing historical success."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0012_refresh_evidence"
down_revision: str | Sequence[str] | None = "0011_keep_on_site"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TIMESTAMPS = (
    "details_succeeded_at", "details_next_retry_at", "link_checked_at", "link_succeeded_at",
    "link_next_retry_at", "deactivated_at", "absence_first_seen_at", "absence_last_seen_at",
)


def upgrade() -> None:
    for name in TIMESTAMPS:
        op.add_column("listings", sa.Column(name, sa.DateTime(timezone=True), nullable=True))
    for name in ("detail_failure_count", "absence_streak"):
        op.add_column("listings", sa.Column(name, sa.Integer(), nullable=False, server_default="0"))
    op.add_column("listings", sa.Column("link_status", sa.String(32), nullable=True))
    op.add_column("listings", sa.Column("absence_last_run_id", sa.String(64), nullable=True))


def downgrade() -> None:
    # Application rollback keeps these additive columns and their evidence intact.
    raise RuntimeError("Use application rollback; do not discard refresh evidence automatically")
