"""Persistent identities and reversible, evidenced source links. No legacy group import."""

import sqlalchemy as sa

from alembic import op

revision = "0016_identity_edges"
down_revision = "0015_validation_work"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "apartment_identities",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "anchor_listing_id",
            sa.Integer(),
            sa.ForeignKey("listings.id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column("merged_into", sa.Integer(), sa.ForeignKey("apartment_identities.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "listing_identity_edges",
        sa.Column(
            "left_id",
            sa.Integer(),
            sa.ForeignKey("listings.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column(
            "right_id",
            sa.Integer(),
            sa.ForeignKey("listings.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("review_reason", sa.Text()),
        sa.CheckConstraint("left_id < right_id", name="ordered_identity_edge"),
    )
    op.create_table(
        "identity_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    raise RuntimeError("Use application rollback; preserve queued work and identity evidence")
