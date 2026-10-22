"""outreach email tables

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-22 18:00:00.000000

- email_drafts: the email to one company, its current text and earlier revisions.
- email_sends: the Gmail send queue; the primary key is an idempotency key, so an email is
  queued (and sent) at most once. Rows outlive a purged lead (unsubscribe links work >= 60 days).
- leads.primary_lead_id: leads of the same company point to the one that gets the email.
- touches.email_send_id / rfc_message_id: link a sent email to its send row and Message-ID.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

OPEN_DRAFT = "kind = 'initial' AND status IN ('drafting', 'pending', 'approved')"


def upgrade() -> None:
    op.create_table(
        "email_drafts",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("lead_id", sa.BigInteger(), nullable=True),
        sa.Column("lead_ids", postgresql.ARRAY(sa.BigInteger()), server_default="{}", nullable=False),
        sa.Column("run_id", sa.String(), nullable=True),
        sa.Column("kind", sa.String(), server_default="initial", nullable=False),
        sa.Column("language", sa.String(), server_default="fr", nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("author", sa.String(), server_default="ai", nullable=False),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("status", sa.String(), server_default="drafting", nullable=False),
        sa.Column("guard", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("llm", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("history", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_email_drafts_status", "email_drafts", ["status"])
    op.create_index(
        "uq_email_drafts_open_initial",
        "email_drafts",
        ["lead_id"],
        unique=True,
        postgresql_where=sa.text(OPEN_DRAFT),
    )

    op.create_table(
        "email_sends",
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column("draft_id", sa.BigInteger(), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=True),
        sa.Column("lead_id", sa.BigInteger(), nullable=True),
        sa.Column("mode", sa.String(), nullable=False),
        sa.Column("intended_to", sa.String(), nullable=False),
        sa.Column("actual_to", sa.String(), nullable=True),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("rendered_text", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.String(), nullable=False),
        sa.Column("unsubscribe_token", sa.String(), nullable=True),
        sa.Column("rfc_message_id", sa.String(), nullable=True),
        sa.Column("status", sa.String(), server_default="queued", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("approved_by", sa.String(), nullable=True),
        sa.Column("gmail_message_id", sa.String(), nullable=True),
        sa.Column("gmail_thread_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["draft_id"], ["email_drafts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("idempotency_key"),
        sa.UniqueConstraint("unsubscribe_token", name="uq_email_sends_unsubscribe_token"),
        sa.UniqueConstraint("rfc_message_id", name="uq_email_sends_rfc_message_id"),
    )
    op.create_index("ix_email_sends_status", "email_sends", ["status"])
    op.create_index("ix_email_sends_intended_to", "email_sends", ["intended_to"])

    op.add_column("leads", sa.Column("primary_lead_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "fk_leads_primary_lead_id", "leads", "leads", ["primary_lead_id"], ["id"], ondelete="SET NULL"
    )
    op.create_index("ix_leads_primary_lead_id", "leads", ["primary_lead_id"])

    op.add_column("touches", sa.Column("email_send_id", sa.String(), nullable=True))
    op.add_column("touches", sa.Column("rfc_message_id", sa.String(), nullable=True))
    op.create_foreign_key(
        "fk_touches_email_send_id",
        "touches",
        "email_sends",
        ["email_send_id"],
        ["idempotency_key"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_touches_email_send_id", "touches", type_="foreignkey")
    op.drop_column("touches", "rfc_message_id")
    op.drop_column("touches", "email_send_id")
    op.drop_index("ix_leads_primary_lead_id", table_name="leads")
    op.drop_constraint("fk_leads_primary_lead_id", "leads", type_="foreignkey")
    op.drop_column("leads", "primary_lead_id")
    op.drop_index("ix_email_sends_intended_to", table_name="email_sends")
    op.drop_index("ix_email_sends_status", table_name="email_sends")
    op.drop_table("email_sends")
    op.drop_index("uq_email_drafts_open_initial", table_name="email_drafts")
    op.drop_index("ix_email_drafts_status", table_name="email_drafts")
    op.drop_table("email_drafts")
