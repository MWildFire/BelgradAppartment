"""SQLAlchemy 2.x ORM models.

Schema mirrors the data model from docs/SPEC.md §5.

Notes on portability:
- Photo lists and pHash lists are stored as JSON columns (SQLite has JSON1; PG has native JSONB).
- All timestamps are timezone-aware UTC.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """Common declarative base for all ORM models."""


# --- Enums ---


class Platform(str, enum.Enum):
    NEKRETNINE = "NEKRETNINE"
    CETIRI_ZIDA = "CETIRI_ZIDA"
    HALOOGLASI = "HALOOGLASI"


class InboundStatus(str, enum.Enum):
    NEW = "NEW"
    MATCHED = "MATCHED"
    NO_MATCH = "NO_MATCH"
    DUPLICATE = "DUPLICATE"
    REJECTED = "REJECTED"


class MatchStatus(str, enum.Enum):
    PENDING = "PENDING"
    PUBLISHED = "PUBLISHED"
    REJECTED = "REJECTED"


def resolve_post_send_status(current: MatchStatus) -> MatchStatus:
    """Match status after a successful send: PENDING → PUBLISHED, anything else unchanged.

    An admin may set REJECTED while the send was in flight — the message already went out,
    but the status must keep reflecting the human decision, not get resurrected.
    """
    return MatchStatus.PUBLISHED if current == MatchStatus.PENDING else current


# --- Tables ---


class SourceChannel(Base):
    """Telegram channels we monitor for inbound apartment posts."""

    __tablename__ = "source_channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    username: Mapped[str | None] = mapped_column(String(128), nullable=True)
    title: Mapped[str | None] = mapped_column(String(256), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    # Backfill watermark: where to resume from on the next monitor run.
    start_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_processed_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    posts: Mapped[list[InboundPost]] = relationship(back_populates="source_channel")


class InboundPost(Base):
    """Raw post received from a source channel."""

    __tablename__ = "inbound_posts"
    __table_args__ = (
        UniqueConstraint(
            "source_channel_id", "tg_message_id", "digest_share_id",
            name="uq_inbound_channel_msg",
        ),
        # SQLite UNIQUE treats NULLs as distinct, so the triple key above misses
        # duplicates with digest_share_id IS NULL — this partial index catches them.
        Index(
            "uq_inbound_chan_msg_nodigest",
            "source_channel_id",
            "tg_message_id",
            unique=True,
            sqlite_where=text("digest_share_id IS NULL"),
        ),
        Index("ix_inbound_status", "status"),
        Index("ix_inbound_received_at", "received_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_channel_id: Mapped[int] = mapped_column(
        ForeignKey("source_channels.id", ondelete="CASCADE"), nullable=False
    )
    tg_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    post_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set only for inbound rows derived from a serbia4rent-style digest post: one Telegram
    # message contains N apartment links to *.yonote.ru, and each YoNote share becomes its
    # own InboundPost. NULL means a normal single-apartment repost.
    digest_share_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    raw_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    photo_paths: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    photo_phashes: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    # CLIP ViT-B/32 embeddings: concatenated float16 blob (N * 512 * 2 bytes).
    photo_embeddings: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)

    parsed_price_eur: Mapped[int | None] = mapped_column(Integer, nullable=True)
    parsed_area_m2: Mapped[float | None] = mapped_column(Float, nullable=True)
    parsed_available_from: Mapped[str | None] = mapped_column(String(64), nullable=True)

    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    status: Mapped[InboundStatus] = mapped_column(
        Enum(InboundStatus, name="inbound_status"), default=InboundStatus.NEW, nullable=False
    )

    source_channel: Mapped[SourceChannel] = relationship(back_populates="posts")
    matches: Mapped[list[Match]] = relationship(back_populates="inbound_post")


class Listing(Base):
    """One listing scraped from one of the three platforms."""

    __tablename__ = "listings"
    __table_args__ = (
        UniqueConstraint("platform", "external_id", name="uq_listing_platform_external"),
        # No single-column platform index: ix_listings_platform_active covers it as a left prefix.
        Index("ix_listing_is_active", "is_active"),
        Index("ix_listing_scraped_at", "scraped_at"),
        Index("ix_listings_last_seen_at", "last_seen_at"),
        Index("ix_listings_platform_active", "platform", "is_active"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    platform: Mapped[Platform] = mapped_column(Enum(Platform, name="platform"), nullable=False)
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    url: Mapped[str] = mapped_column(String(512), nullable=False)

    price_eur: Mapped[int | None] = mapped_column(Integer, nullable=True)
    area_m2: Mapped[float | None] = mapped_column(Float, nullable=True)
    rooms: Mapped[float | None] = mapped_column(Float, nullable=True)
    district: Mapped[str | None] = mapped_column(String(128), nullable=True)
    address_raw: Mapped[str | None] = mapped_column(String(256), nullable=True)
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lon: Mapped[float | None] = mapped_column(Float, nullable=True)

    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    photo_urls: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    photo_phashes: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    # CLIP ViT-B/32 embeddings: concatenated float16 blob (N * 512 * 2 bytes).
    photo_embeddings: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)

    raw_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    scraped_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # NULL = details never re-fetched; rolling refresh sorts ASC (SQLite puts NULLs first).
    details_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    matches: Mapped[list[Match]] = relationship(back_populates="listing")


class Match(Base):
    """Candidate for publication: pairing inbound post with a listing."""

    __tablename__ = "matches"
    __table_args__ = (
        Index("ix_match_status", "status"),
        Index("ix_match_inbound", "inbound_post_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    inbound_post_id: Mapped[int] = mapped_column(
        ForeignKey("inbound_posts.id", ondelete="CASCADE"), nullable=False
    )
    listing_id: Mapped[int] = mapped_column(
        ForeignKey("listings.id", ondelete="CASCADE"), nullable=False
    )

    confidence: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    matched_photo_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    method: Mapped[str] = mapped_column(String(32), default="phash", nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    status: Mapped[MatchStatus] = mapped_column(
        Enum(MatchStatus, name="match_status"), default=MatchStatus.PENDING, nullable=False
    )
    rejection_reason: Mapped[str | None] = mapped_column(String(256), nullable=True)

    inbound_post: Mapped[InboundPost] = relationship(back_populates="matches")
    listing: Mapped[Listing] = relationship(back_populates="matches")
    published_post: Mapped[PublishedPost | None] = relationship(back_populates="match", uselist=False)


class DailyMetrics(Base):
    """Daily per-platform snapshot of listing counts — for active-drop alerts and dashboards."""

    __tablename__ = "daily_metrics"
    __table_args__ = (
        UniqueConstraint("snapshot_date", "platform", name="uq_daily_metrics_date_platform"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot_date: Mapped[str] = mapped_column(String(10), nullable=False)  # ISO YYYY-MM-DD
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    active_count: Mapped[int] = mapped_column(Integer, nullable=False)
    no_photo_count: Mapped[int] = mapped_column(Integer, nullable=False)


class PublishedPost(Base):
    """History of published posts in @OtzRentBelgrad — for dedup and analytics."""

    __tablename__ = "published_posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    match_id: Mapped[int] = mapped_column(
        ForeignKey("matches.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    tg_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    post_text: Mapped[str] = mapped_column(Text, nullable=False)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    match: Mapped[Match] = relationship(back_populates="published_post")


class SiteListing(Base):
    """Public-catalog overlay over a PUBLISHED Match (Stage W).

    CQRS read-model: the pipeline writes its own tables; the site reads this curated
    overlay. One row per apartment Дарья chose to expose. Everything else (price/area/
    rooms/district/coords/photos) is joined from `listings` / `inbound_posts`.
    """

    __tablename__ = "site_listings"
    __table_args__ = (
        Index("ix_site_listings_is_public", "is_public"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    match_id: Mapped[int] = mapped_column(
        ForeignKey("matches.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    is_public: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_for_vnj: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_for_white_card: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    channel_post_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class Lead(Base):
    """Personal-search request submitted from the public site's lead form (Stage W)."""

    __tablename__ = "leads"
    __table_args__ = (
        Index("ix_leads_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    contact: Mapped[str] = mapped_column(Text, nullable=False)
    budget_eur: Mapped[int | None] = mapped_column(Integer, nullable=True)
    districts: Mapped[str | None] = mapped_column(Text, nullable=True)
    rooms: Mapped[str | None] = mapped_column(Text, nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(Text, default="site", server_default="site", nullable=False)
    status: Mapped[str] = mapped_column(Text, default="new", server_default="new", nullable=False)
