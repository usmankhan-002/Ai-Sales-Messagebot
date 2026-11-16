"""The bot's own tables, in its separate `AISalesMessage_bot` Neon database.

Organizations, clients, market commentary and AI settings follow the Trade
Builder's table and column names (UUID ids, `full_name`, `for_date`/`body`,
`ranking_indicators`, ...) so the two stay recognisably the same, but they are
the bot's own records: nothing here reads or writes the Trade Builder database.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import (JSON, BigInteger, Date, DateTime, ForeignKey, Index, Integer, Numeric,
                        String, Text, Uuid)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

JsonType = JSON().with_variant(JSONB(), "postgresql")
# BIGINT identity on Postgres, INTEGER on SQLite (so tests autoincrement).
BigId = BigInteger().with_variant(Integer(), "sqlite")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    sector: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class Client(Base):
    __tablename__ = "clients"
    __table_args__ = (Index("ix_clients_org_active", "org_id", "deactivated_at"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    # Same names as the Trade Builder's clients table.
    full_name: Mapped[str | None] = mapped_column(Text)
    company: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text)
    type: Mapped[str] = mapped_column(Text, nullable=False, default="prospect")
    notes: Mapped[str | None] = mapped_column(Text)
    # The bot's client-profile fields used to tailor messages.
    phone: Mapped[str | None] = mapped_column(Text)
    industry: Mapped[str | None] = mapped_column(Text)
    risk_appetite: Mapped[str | None] = mapped_column(Text)
    hedging_horizon: Mapped[str | None] = mapped_column(Text)
    functional_currency: Mapped[str | None] = mapped_column(String(12))
    created_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OrgMarketCommentary(Base):
    """One commentary entry per organization per date (same shape as the Trade Builder's)."""

    __tablename__ = "org_market_commentary"

    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True)
    for_date: Mapped[date] = mapped_column(Date, primary_key=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    updated_by: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class OrgAiSettings(Base):
    """The organization's ranking rubric (same shape as the Trade Builder's org_ai_settings)."""

    __tablename__ = "org_ai_settings"

    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True)
    ranking_indicators: Mapped[list] = mapped_column(JsonType, nullable=False, default=list)
    updated_by: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class AiDraft(Base):
    """Every message the bot generated. Deleted after DRAFT_RETENTION_DAYS, or with its client."""

    __tablename__ = "ai_drafts"
    __table_args__ = (
        Index("ix_drafts_org_deal_created", "org_id", "deal_id", "created_at"),
        Index("ix_drafts_client", "client_id"),
    )

    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    org_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("organizations.id", ondelete="CASCADE"))
    client_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("clients.id", ondelete="CASCADE"))
    user_id: Mapped[str | None] = mapped_column(String(128))
    deal_id: Mapped[str | None] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # sales_message | sms_summary
    product: Mapped[str | None] = mapped_column(String(200))
    advisor_note: Mapped[str | None] = mapped_column(Text)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str | None] = mapped_column(Text)
    alternative_message: Mapped[str | None] = mapped_column(Text)
    request: Mapped[dict | None] = mapped_column(JsonType)  # what the bot was given
    model: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)


class AiRanking(Base):
    """Every ranking result, so message quality can be tracked over time."""

    __tablename__ = "ai_rankings"
    __table_args__ = (Index("ix_rankings_org_created", "org_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    org_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("organizations.id", ondelete="CASCADE"))
    draft_id: Mapped[int | None] = mapped_column(BigId, ForeignKey("ai_drafts.id", ondelete="CASCADE"), index=True)
    client_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("clients.id", ondelete="CASCADE"))
    message: Mapped[str] = mapped_column(Text, nullable=False)
    indicators: Mapped[list] = mapped_column(JsonType, nullable=False)  # rubric used
    results: Mapped[list] = mapped_column(JsonType, nullable=False)     # [{id, score, evidence}]
    overall_score: Mapped[Decimal] = mapped_column(Numeric(3, 1), nullable=False)
    band: Mapped[str] = mapped_column(String(8), nullable=False)
    model: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class AiUsageLog(Base):
    """One row per AI call, successful or not. Holds no client details."""

    __tablename__ = "ai_usage_log"
    __table_args__ = (Index("ix_usage_org_created", "org_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    org_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("organizations.id", ondelete="SET NULL"))
    user_id: Mapped[str | None] = mapped_column(String(128))
    endpoint: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str | None] = mapped_column(String(64))
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    error: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
