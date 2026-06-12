"""Sweep/select indexes on listings, partial unique against NULL-digest dedup race,
listings.details_refreshed_at, daily_metrics table.

Revision ID: 0005_indexes_dedup_metrics
Revises: 0004_digest_share_id
Create Date: 2026-06-12
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_indexes_dedup_metrics"
down_revision: str | Sequence[str] | None = "0004_digest_share_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _dedupe_null_digest_duplicates() -> None:
    """Remove NULL-digest duplicate inbound rows (keep min(id)), cascading to
    matches and published_posts. SQLite UNIQUE treats NULLs as distinct, so the
    triple-key constraint from 0004 never caught these."""
    bind = op.get_bind()
    dup_ids = (
        bind.execute(
            sa.text(
                """
                SELECT id FROM inbound_posts
                WHERE digest_share_id IS NULL
                  AND id NOT IN (
                    SELECT MIN(id) FROM inbound_posts
                    WHERE digest_share_id IS NULL
                    GROUP BY source_channel_id, tg_message_id
                  )
                """
            )
        )
        .scalars()
        .all()
    )
    if not dup_ids:
        print("0005: no NULL-digest duplicate inbound_posts found")
        return

    ids_param = sa.bindparam("ids", expanding=True)
    match_ids = (
        bind.execute(
            sa.text("SELECT id FROM matches WHERE inbound_post_id IN :ids").bindparams(ids_param),
            {"ids": list(dup_ids)},
        )
        .scalars()
        .all()
    )
    pub_deleted = 0
    if match_ids:
        pub_deleted = bind.execute(
            sa.text("DELETE FROM published_posts WHERE match_id IN :ids").bindparams(ids_param),
            {"ids": list(match_ids)},
        ).rowcount
        bind.execute(
            sa.text("DELETE FROM matches WHERE id IN :ids").bindparams(ids_param),
            {"ids": list(match_ids)},
        )
    bind.execute(
        sa.text("DELETE FROM inbound_posts WHERE id IN :ids").bindparams(ids_param),
        {"ids": list(dup_ids)},
    )
    shown_ids = sorted(dup_ids)
    extra = len(shown_ids) - 50
    ids_repr = f"{shown_ids[:50]} (+{extra} more)" if extra > 0 else f"{shown_ids}"
    print(
        f"0005: deleted {len(dup_ids)} duplicate inbound_posts "
        f"(ids={ids_repr}), {len(match_ids)} matches, {pub_deleted} published_posts"
    )


def upgrade() -> None:
    _dedupe_null_digest_duplicates()
    op.create_index(
        "uq_inbound_chan_msg_nodigest",
        "inbound_posts",
        ["source_channel_id", "tg_message_id"],
        unique=True,
        sqlite_where=sa.text("digest_share_id IS NULL"),
    )

    with op.batch_alter_table("listings") as batch_op:
        batch_op.add_column(sa.Column("details_refreshed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_listings_last_seen_at", "listings", ["last_seen_at"])
    op.create_index("ix_listings_platform_active", "listings", ["platform", "is_active"])
    # Covered as a left prefix by ix_listings_platform_active — drop the dead weight.
    op.drop_index("ix_listing_platform", table_name="listings")

    op.create_table(
        "daily_metrics",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("snapshot_date", sa.String(length=10), nullable=False),
        sa.Column("platform", sa.String(length=32), nullable=False),
        sa.Column("active_count", sa.Integer(), nullable=False),
        sa.Column("no_photo_count", sa.Integer(), nullable=False),
        sa.UniqueConstraint("snapshot_date", "platform", name="uq_daily_metrics_date_platform"),
    )


def downgrade() -> None:
    # Deduped rows are not restored.
    op.drop_table("daily_metrics")
    op.create_index("ix_listing_platform", "listings", ["platform"])
    op.drop_index("ix_listings_platform_active", table_name="listings")
    op.drop_index("ix_listings_last_seen_at", table_name="listings")
    with op.batch_alter_table("listings") as batch_op:
        batch_op.drop_column("details_refreshed_at")
    op.drop_index("uq_inbound_chan_msg_nodigest", table_name="inbound_posts")
