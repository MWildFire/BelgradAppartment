"""Durable matching/digest work; no mutation of legacy states or historical matches."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0015_validation_work"
down_revision: str | Sequence[str] | None = "0014_group_availability_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table in ("listings", "inbound_posts"):
        op.add_column(
            table,
            sa.Column("photo_evidence", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        )
    op.create_table(
        "validation_jobs",
        sa.Column(
            "inbound_id",
            sa.Integer(),
            sa.ForeignKey("inbound_posts.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("reason_code", sa.String(64), nullable=False),
        sa.Column("lease_token", sa.String(64)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("next_retry_at", sa.DateTime(timezone=True)),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("outcomes", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "digest_work_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "source_id",
            sa.Integer(),
            sa.ForeignKey("source_channels.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("share_id", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("post_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_retry_at", sa.DateTime(timezone=True)),
        sa.Column("lease_token", sa.String(64)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("reason_code", sa.String(64), nullable=False),
        sa.UniqueConstraint("source_id", "message_id", "share_id"),
    )


def downgrade() -> None:
    raise RuntimeError("Use application rollback; preserve queued work and identity evidence")
