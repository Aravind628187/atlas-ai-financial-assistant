"""
Data model for Atlas AI.

Design goals:
  - one row per Telegram user, everything else hangs off `User.id`
  - conversation history is stored so the assistant has real memory
  - `Preference` is a flexible key/value bag so personalization can grow
    without a migration every time we learn a new kind of fact
"""
from __future__ import annotations

import datetime as dt
import enum

from sqlalchemy import (
    String, Integer, BigInteger, Float, Boolean, Text, DateTime, ForeignKey, Enum, JSON, UniqueConstraint
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> dt.datetime:
    return dt.datetime.utcnow()


class OnboardingStage(str, enum.Enum):
    NEW = "new"
    ASKED_ROLE = "asked_role"
    ASKED_INTERESTS = "asked_interests"
    ASKED_MONITORING = "asked_monitoring"
    ASKED_INTELLIGENCE = "asked_intelligence"
    ASKED_BRIEFING_TIME = "asked_briefing_time"
    ASKED_INTEGRATIONS = "asked_integrations"
    DONE = "done"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(128), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(128), nullable=True)

    role: Mapped[str | None] = mapped_column(String(64), nullable=True)  # investor/analyst/founder/...
    onboarding_stage: Mapped[str] = mapped_column(String(32), default=OnboardingStage.NEW.value)
    briefing_hour_local: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 0-23
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Kolkata")
    last_document_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    last_active_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    messages: Mapped[list["Message"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    watchlist_items: Mapped[list["WatchlistItem"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    preferences: Mapped[list["Preference"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    documents: Mapped[list["Document"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    alerts: Mapped[list["Alert"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    integrations: Mapped[list["Integration"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class Message(Base):
    """One turn of conversation. Used both as memory context and as an analytics log."""
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    role: Mapped[str] = mapped_column(String(16))  # "user" | "assistant"
    content: Mapped[str] = mapped_column(Text)
    intent: Mapped[str | None] = mapped_column(String(64), nullable=True)  # research/document/alert/chitchat...
    input_kind: Mapped[str] = mapped_column(String(16), default="text")  # text/voice/image/document
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)

    user: Mapped["User"] = relationship(back_populates="messages")


class WatchlistItem(Base):
    __tablename__ = "watchlist_items"
    __table_args__ = (UniqueConstraint("user_id", "symbol", name="uq_user_symbol"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    symbol: Mapped[str] = mapped_column(String(24))
    label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    added_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    user: Mapped["User"] = relationship(back_populates="watchlist_items")


class Preference(Base):
    """Flexible fact store the assistant learns over time, e.g. key='sector', value='semiconductors'."""
    __tablename__ = "preferences"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    key: Mapped[str] = mapped_column(String(64))
    value: Mapped[str] = mapped_column(String(512))
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    user: Mapped["User"] = relationship(back_populates="preferences")


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    filename: Mapped[str] = mapped_column(String(256))
    doc_type: Mapped[str] = mapped_column(String(32))  # pdf/image
    extracted_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    user: Mapped["User"] = relationship(back_populates="documents")


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    symbol: Mapped[str] = mapped_column(String(24))
    kind: Mapped[str] = mapped_column(String(32), default="pct_move")  # pct_move/news/filing
    threshold_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_triggered_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_triggered_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    user: Mapped["User"] = relationship(back_populates="alerts")


class Integration(Base):
    """Connected third-party account (Gmail / Calendar / Drive / Sheets)."""
    __tablename__ = "integrations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    provider: Mapped[str] = mapped_column(String(32))  # gmail/calendar/drive/sheets
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending/connected/skipped
    access_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    connected_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    user: Mapped["User"] = relationship(back_populates="integrations")


class BriefingLog(Base):
    """Record of every proactive push, so the dashboard can show what was sent and why."""
    __tablename__ = "briefing_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))  # morning_brief/evening_summary/alert
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)


class DataFetchLog(Base):
    """Provider observability without storing credentials or raw private payloads."""
    __tablename__ = "data_fetch_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    provider: Mapped[str] = mapped_column(String(64), index=True)
    operation: Mapped[str] = mapped_column(String(32))
    symbol: Mapped[str | None] = mapped_column(String(24), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(24), index=True)
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    error_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)


class ResponseValidationLog(Base):
    """Counts unsafe model responses without retaining secrets or provider payloads."""
    __tablename__ = "response_validation_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    intent: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result: Mapped[str] = mapped_column(String(24), index=True)
    unsupported_claims: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)
