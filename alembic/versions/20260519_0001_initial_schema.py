"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-05-19
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "source_channels",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tg_id", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(length=128), nullable=True),
        sa.Column("title", sa.String(length=256), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tg_id", name="uq_source_channels_tg_id"),
    )

    op.create_table(
        "inbound_posts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_channel_id", sa.Integer(), nullable=False),
        sa.Column("tg_message_id", sa.Integer(), nullable=False),
        sa.Column("raw_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("photo_paths", sa.JSON(), nullable=False),
        sa.Column("photo_phashes", sa.JSON(), nullable=False),
        sa.Column("parsed_price_eur", sa.Integer(), nullable=True),
        sa.Column("parsed_area_m2", sa.Float(), nullable=True),
        sa.Column("parsed_available_from", sa.String(length=64), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "status",
            sa.Enum("NEW", "MATCHED", "NO_MATCH", "DUPLICATE", "REJECTED", name="inbound_status"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["source_channel_id"], ["source_channels.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("source_channel_id", "tg_message_id", name="uq_inbound_channel_msg"),
    )
    op.create_index("ix_inbound_status", "inbound_posts", ["status"])
    op.create_index("ix_inbound_received_at", "inbound_posts", ["received_at"])

    op.create_table(
        "listings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "platform",
            sa.Enum("NEKRETNINE", "CETIRI_ZIDA", "HALOOGLASI", name="platform"),
            nullable=False,
        ),
        sa.Column("external_id", sa.String(length=128), nullable=False),
        sa.Column("url", sa.String(length=512), nullable=False),
        sa.Column("price_eur", sa.Integer(), nullable=True),
        sa.Column("area_m2", sa.Float(), nullable=True),
        sa.Column("rooms", sa.Float(), nullable=True),
        sa.Column("district", sa.String(length=128), nullable=True),
        sa.Column("address_raw", sa.String(length=256), nullable=True),
        sa.Column("lat", sa.Float(), nullable=True),
        sa.Column("lon", sa.Float(), nullable=True),
        sa.Column("title", sa.String(length=512), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("photo_urls", sa.JSON(), nullable=False),
        sa.Column("photo_phashes", sa.JSON(), nullable=False),
        sa.Column("raw_payload", sa.JSON(), nullable=True),
        sa.Column("scraped_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.UniqueConstraint("platform", "external_id", name="uq_listing_platform_external"),
    )
    op.create_index("ix_listing_platform", "listings", ["platform"])
    op.create_index("ix_listing_is_active", "listings", ["is_active"])
    op.create_index("ix_listing_scraped_at", "listings", ["scraped_at"])

    op.create_table(
        "matches",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("inbound_post_id", sa.Integer(), nullable=False),
        sa.Column("listing_id", sa.Integer(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("matched_photo_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("method", sa.String(length=32), nullable=False, server_default="phash"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "status",
            sa.Enum("PENDING", "PUBLISHED", "REJECTED", name="match_status"),
            nullable=False,
        ),
        sa.Column("rejection_reason", sa.String(length=256), nullable=True),
        sa.ForeignKeyConstraint(["inbound_post_id"], ["inbound_posts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["listing_id"], ["listings.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_match_status", "matches", ["status"])
    op.create_index("ix_match_inbound", "matches", ["inbound_post_id"])

    op.create_table(
        "published_posts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("match_id", sa.Integer(), nullable=False),
        sa.Column("tg_message_id", sa.Integer(), nullable=False),
        sa.Column("post_text", sa.Text(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["match_id"], ["matches.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("match_id", name="uq_published_posts_match_id"),
    )


def downgrade() -> None:
    op.drop_table("published_posts")
    op.drop_index("ix_match_inbound", table_name="matches")
    op.drop_index("ix_match_status", table_name="matches")
    op.drop_table("matches")
    op.drop_index("ix_listing_scraped_at", table_name="listings")
    op.drop_index("ix_listing_is_active", table_name="listings")
    op.drop_index("ix_listing_platform", table_name="listings")
    op.drop_table("listings")
    op.drop_index("ix_inbound_received_at", table_name="inbound_posts")
    op.drop_index("ix_inbound_status", table_name="inbound_posts")
    op.drop_table("inbound_posts")
    op.drop_table("source_channels")
    sa.Enum(name="match_status").drop(op.get_bind(), checkfirst=False)
    sa.Enum(name="platform").drop(op.get_bind(), checkfirst=False)
    sa.Enum(name="inbound_status").drop(op.get_bind(), checkfirst=False)
