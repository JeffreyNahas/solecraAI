"""Database tables. Gate 1 tables first, then Phase 1 outreach tables."""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
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


# --- Phase 1: outreach (migration 0002) ---------------------------------------

LEAD_STATUSES = (
    "new",
    "researched",
    "qualified",
    "drafted",
    "approved",
    "sent",
    "follow_up_1",
    "follow_up_2",
    "replied",
    "site_visit_booked",
    "won",
    "lost",
    "disqualified",
    "no_valid_contact",
    "do_not_contact",
)


class Lead(Base):
    """A prospect. Unconverted leads live here (Postgres is their source of truth)."""

    __tablename__ = "leads"
    __table_args__ = (UniqueConstraint("source", "source_ref", name="uq_leads_source_ref"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    business_name: Mapped[str | None] = mapped_column(String)
    contact_name: Mapped[str | None] = mapped_column(String)
    role: Mapped[str | None] = mapped_column(String)
    email: Mapped[str | None] = mapped_column(String)
    phone: Mapped[str | None] = mapped_column(String)
    address: Mapped[str | None] = mapped_column(String)
    website: Mapped[str | None] = mapped_column(String)
    # permit | owner_tip | google_places | news | manual
    source: Mapped[str] = mapped_column(String)
    source_ref: Mapped[str | None] = mapped_column(String)
    score: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String, default="new", index=True)
    language: Mapped[str] = mapped_column(String, default="fr")  # fr | en (Bill 96: French first)
    # CASL consent evidence: where the address was published, when, for which role, why relevant.
    consent_source_url: Mapped[str | None] = mapped_column(Text)
    consent_captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consent_role: Mapped[str | None] = mapped_column(String)
    relevance_note: Mapped[str | None] = mapped_column(Text)
    score_reason: Mapped[str | None] = mapped_column(Text)  # one line from the scoring step
    # Public facts from the source record (e.g. permit type, units, issue date). No personal data.
    source_details: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # Research step record: when, model/cost, tool calls, the model's answer and the code's consent
    # check with its evidence (page excerpt, hash, fetch time). See app/jobs/lead_research.py.
    research_details: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    owner_assigned: Mapped[str | None] = mapped_column(String)
    # Leads of the same company point to the lead that gets the email (one email per company).
    primary_lead_id: Mapped[int | None] = mapped_column(
        ForeignKey("leads.id", ondelete="SET NULL"), index=True
    )
    # Normalized values used for dedupe (see app/leads.py).
    email_domain: Mapped[str | None] = mapped_column(String, index=True)
    phone_normalized: Mapped[str | None] = mapped_column(String, index=True)
    address_normalized: Mapped[str | None] = mapped_column(String, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    version: Mapped[int] = mapped_column(Integer, default=1)


class Touch(Base):
    """One email in or out with a lead (or an owner note about it)."""

    __tablename__ = "touches"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    direction: Mapped[str] = mapped_column(String)  # in | out
    type: Mapped[str] = mapped_column(String)  # initial | follow_up | reply | owner_response
    gmail_message_id: Mapped[str | None] = mapped_column(String, unique=True)
    gmail_thread_id: Mapped[str | None] = mapped_column(String, index=True)
    subject: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String, default="pending")  # pending | sent | received | failed
    email_send_id: Mapped[str | None] = mapped_column(
        ForeignKey("email_sends.idempotency_key", ondelete="SET NULL")
    )
    rfc_message_id: Mapped[str | None] = mapped_column(String)  # for In-Reply-To on follow-ups
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


DRAFT_STATUSES = ("drafting", "pending", "approved", "rejected", "blocked", "expired", "cancelled")
SEND_STATUSES = ("queued", "held", "sending", "sent", "unknown", "failed", "cancelled")


class EmailDraft(Base):
    """The outreach email to one company: current text plus earlier revisions in `history`.

    At most one open initial draft per lead (partial unique index)."""

    __tablename__ = "email_drafts"
    __table_args__ = (
        Index(
            "uq_email_drafts_open_initial",
            "lead_id",
            unique=True,
            postgresql_where=text("kind = 'initial' AND status IN ('drafting', 'pending', 'approved')"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id", ondelete="SET NULL"))  # primary lead
    lead_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger), default=list)  # the company's leads
    run_id: Mapped[str | None] = mapped_column(String)  # LangGraph thread_id
    kind: Mapped[str] = mapped_column(String, default="initial")  # initial | follow_up (#11)
    language: Mapped[str] = mapped_column(String, default="fr")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    author: Mapped[str] = mapped_column(String, default="ai")  # ai | owner
    subject: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String, default="drafting", index=True)  # DRAFT_STATUSES
    guard: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # last output-guard result
    llm: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # model, cost
    history: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)  # earlier revisions
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class EmailSend(Base):
    """Gmail send queue. The primary key is an idempotency key (e.g. `initial:<lead_id>:<mode>`),
    so an email is queued, and sent, at most once. Kept after a lead is purged: the unsubscribe
    link must keep working for at least 60 days (CASL)."""

    __tablename__ = "email_sends"
    __table_args__ = (
        UniqueConstraint("unsubscribe_token", name="uq_email_sends_unsubscribe_token"),
        UniqueConstraint("rfc_message_id", name="uq_email_sends_rfc_message_id"),
    )

    idempotency_key: Mapped[str] = mapped_column(String, primary_key=True)
    draft_id: Mapped[int | None] = mapped_column(ForeignKey("email_drafts.id", ondelete="SET NULL"))
    revision: Mapped[int | None] = mapped_column(Integer)
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("leads.id", ondelete="SET NULL"))
    mode: Mapped[str] = mapped_column(String)  # test | live
    intended_to: Mapped[str] = mapped_column(String, index=True)
    actual_to: Mapped[str | None] = mapped_column(String)
    subject: Mapped[str] = mapped_column(Text)
    rendered_text: Mapped[str] = mapped_column(Text)
    content_sha256: Mapped[str] = mapped_column(String)  # of the approved text; re-checked at send
    unsubscribe_token: Mapped[str | None] = mapped_column(String)
    rfc_message_id: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="queued", index=True)  # SEND_STATUSES
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    approved_by: Mapped[str | None] = mapped_column(String)
    gmail_message_id: Mapped[str | None] = mapped_column(String)
    gmail_thread_id: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Suppression(Base):
    """Addresses or whole domains we must never email again. Checked before every send, forever."""

    __tablename__ = "suppression"

    email_or_domain: Mapped[str] = mapped_column(String, primary_key=True)  # lowercase
    reason: Mapped[str] = mapped_column(String)  # unsubscribe | bounce | do_not_contact
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Run(Base):
    """One execution of a scheduled job or workflow."""

    __tablename__ = "runs"
    __table_args__ = (Index("ix_runs_job_started", "job", "started_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    job: Mapped[str] = mapped_column(String)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String, default="running")  # running | succeeded | failed
    counts: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
