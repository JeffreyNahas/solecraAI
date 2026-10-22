"""Show outreach drafts to the owners on WhatsApp, one at a time (decided 2026-10-22).

- `morning(deps)` (08:00 job): "N drafts ready" — the `drafts_ready` template for an owner whose
  24 h window is closed (its "Show drafts" button reopens it), else a text — then the first draft.
- `present_next(deps)`: the oldest pending draft approval goes to every owner whose window is open:
  one text with the research evidence and the full email (split, never cut), then a buttons message.
  Called after each decision, on "Show drafts" and on the `drafts` command. Keys include the
  approval id and version, so calling it again never repeats a message.
While an owner is editing a draft, nothing new is shown (one at a time).
"""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.channel import ChannelError
from app.db.models import Approval, EmailDraft, Lead, Owner
from app.deps import Deps
from app.owners import active_owners
from app.whatsapp.buttons import ButtonId

log = logging.getLogger(__name__)

ITEM_TYPE = "outreach_email"
TEMPLATE = "drafts_ready"
TEMPLATE_LANGUAGE = "en_US"
SHOW_DRAFTS = "show_drafts"  # quick-reply payload of the template button
WINDOW = timedelta(hours=23)  # Meta's window is 24 h; keep a margin


def window_open(owner: Owner, now: datetime | None = None) -> bool:
    now = now or datetime.now(UTC)
    return owner.last_inbound_at is not None and now - owner.last_inbound_at < WINDOW


async def pending_count(deps: Deps) -> int:
    async with deps.sessionmaker() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(Approval)
            .where(Approval.item_type == ITEM_TYPE, Approval.status.in_(("pending", "editing")))
        )
    return int(count or 0)


async def morning(deps: Deps, today: str) -> int:
    """Tell owners how many drafts wait, then show the first. Returns the count (0 = nothing sent)."""
    count = await pending_count(deps)
    if count == 0:
        return 0
    async with deps.sessionmaker() as session:
        owners = await active_owners(session)
    for owner in owners:
        key = f"outreach:morning:{today}:{owner.phone}"
        try:
            if window_open(owner):
                text = f"Good morning! {count} email drafts are ready."
                await deps.channel.send_text(owner.phone, text, key)
            else:
                await deps.channel.send_template(
                    owner.phone, TEMPLATE, TEMPLATE_LANGUAGE, [str(count)], key, button_payloads=[SHOW_DRAFTS]
                )
        except ChannelError:
            log.exception("could not send the morning message to %s", owner.phone)
    await present_next(deps)
    return count


async def present_next(deps: Deps, only_phone: str | None = None) -> int | None:
    """Show the current draft to owners whose window is open. Returns its approval id, if any."""
    async with deps.sessionmaker() as session:
        editing = await session.scalar(
            select(Approval.id).where(Approval.item_type == ITEM_TYPE, Approval.status == "editing").limit(1)
        )
        if editing is not None:
            return None
        approval = await session.scalar(
            select(Approval)
            .where(Approval.item_type == ITEM_TYPE, Approval.status == "pending")
            .order_by(Approval.id)
            .limit(1)
        )
        if approval is None:
            return None
        draft = await session.get(EmailDraft, int(approval.item_id or 0))
        if draft is None or draft.status != "pending":
            log.error("approval %s points to draft %s which is not pending", approval.id, approval.item_id)
            return None
        leads = list((await session.execute(select(Lead).where(Lead.id.in_(draft.lead_ids)))).scalars())
        owners = await active_owners(session)
        waiting = await pending_count(deps)

    leads.sort(key=lambda lead: (lead.id != draft.lead_id, lead.id))
    detail = draft_message(draft, leads, waiting)
    primary = leads[0]
    flagged = needs_owner_check(primary)
    company = (primary.business_name or "this company")[:200]  # the email itself is never cut
    question = f"Send this email to {company} ({primary.email})?"
    buttons = [
        (
            ButtonId(action="approve", approval_id=approval.id, version=approval.version).encode(),
            "Approve (checked)" if flagged else "Approve",
        ),
        (ButtonId(action="edit", approval_id=approval.id, version=approval.version).encode(), "Edit"),
        (ButtonId(action="reject", approval_id=approval.id, version=approval.version).encode(), "Reject"),
    ]
    for owner in owners:
        if only_phone and owner.phone != only_phone:
            continue
        if not window_open(owner):
            continue  # the morning template (or their next message) reopens the window
        prefix = f"approval:{approval.id}:v{approval.version}"
        try:
            await deps.channel.send_text(owner.phone, detail, key=f"{prefix}:detail:{owner.phone}")
            await deps.channel.send_buttons(owner.phone, question, buttons, key=f"{prefix}:ask:{owner.phone}")
        except ChannelError:
            log.exception("could not show draft %s to %s", draft.id, owner.phone)
    return approval.id


def needs_owner_check(lead: Lead) -> bool:
    evidence = ((lead.research_details or {}).get("consent_check") or {}).get("evidence") or {}
    return bool(evidence.get("needs_owner_check"))


def draft_message(draft: EmailDraft, leads: list[Lead], waiting: int) -> str:
    """What owners read before deciding: who, why, the evidence, then the full email."""
    primary = leads[0]
    research = primary.research_details or {}
    evidence = (research.get("consent_check") or {}).get("evidence") or {}
    link = (research.get("answer") or {}).get("link_source_url")
    score = primary.score if primary.score is not None else "?"
    lines = [
        f"📝 Email draft ({waiting} waiting) · lead #{primary.id} · score {score}",
        "",
        f"Company: {primary.business_name or '?'} ({primary.consent_role or primary.role or 'role unknown'})",
        f"To: {primary.email}",
    ]
    for lead in leads:
        lines.append(f"Project: {lead.address or '?'}")
    lines += [
        f"Why: {primary.relevance_note or '?'}",
        f"Email found on: {primary.consent_source_url or '?'}",
        f"Company ↔ project: {link or 'named in the permit'}",
    ]
    if needs_owner_check(primary):
        lines += [
            "",
            "⚠️ Directory address that doesn't carry the company name.",
            "Check it's theirs, not a neighbouring listing:",
            f"“{evidence.get('excerpt', '')}”",
        ]
    greeting = f"Bonjour {primary.contact_name}," if primary.contact_name else "Bonjour,"
    if draft.language == "en":
        greeting = f"Hello {primary.contact_name}," if primary.contact_name else "Hello,"
    lines += [
        "",
        f"Subject: {draft.subject}",
        "",
        greeting,
        "",
        draft.body or "",
        "",
        "[+ signature, Solecra contact details and unsubscribe link added automatically]",
    ]
    return "\n".join(lines)
