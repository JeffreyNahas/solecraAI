"""Gate 1 tables. Outreach tables (leads, touches, suppression) come in Phase 1."""

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class InboundMessage(Base):
    """Every WhatsApp message we accepted. The primary key is Meta's message id,
    so a redelivered webhook is ignored (dedupe). Also works as the work queue."""

    __tablename__ = "inbound_messages"

    message_id: Mapped[str] = mapped_column(String, primary_key=True)
    sender: Mapped[str] = mapped_column(String, index=True)
    msg_type: Mapped[str] = mapped_column(String)  # text | button | other
    text: Mapped[str | None] = mapped_column(Text)
    button_id: Mapped[str | None] = mapped_column(String)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # pending -> processing -> done | failed
    status: Mapped[str] = mapped_column(String, default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OutboundMessage(Base):
    """Every WhatsApp message we send, keyed so a retry never sends twice."""

    __tablename__ = "outbound_messages"

    idempotency_key: Mapped[str] = mapped_column(String, primary_key=True)
    recipient: Mapped[str] = mapped_column(String)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String, default="pending")  # pending | sent | failed
    wa_message_id: Mapped[str | None] = mapped_column(String)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Owner(Base):
    __tablename__ = "owners"

    phone: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    role: Mapped[str] = mapped_column(String, default="owner")
    active: Mapped[bool] = mapped_column(default=True)
    # Free-form WhatsApp messages are only allowed within 24h of this.
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Approval(Base):
    """One item waiting for an owner decision. First action wins:
    UPDATE ... WHERE status='pending' AND version=:v."""

    __tablename__ = "approvals"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[str] = mapped_column(String, index=True)  # LangGraph thread_id
    item_type: Mapped[str] = mapped_column(String)
    item_id: Mapped[str | None] = mapped_column(String)
    # pending | editing | approved | edited | rejected
    status: Mapped[str] = mapped_column(String, default="pending", index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    decided_by: Mapped[str | None] = mapped_column(String)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    # Set once the graph has been resumed with the decision.
    resumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    actor: Mapped[str] = mapped_column(String)
    action: Mapped[str] = mapped_column(String)
    entity: Mapped[str | None] = mapped_column(String)
    entity_id: Mapped[str | None] = mapped_column(String)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB)
    updated_by: Mapped[str | None] = mapped_column(String)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
