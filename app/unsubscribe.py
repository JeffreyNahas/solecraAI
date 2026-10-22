"""Unsubscribe from outreach emails (CLAUDE.md §2 rule 7: opt-outs applied instantly).

Every outreach email carries `<PUBLIC_BASE_URL>/u/<token>` (and the List-Unsubscribe headers).
- GET shows a page with one Confirm button. A GET never unsubscribes: corporate mail scanners
  and previews open every link in an email.
- POST unsubscribes. Mail providers also POST to the same URL for the one-click header (RFC 8058).
- An unknown token gets the same page (with Solecra's contact email) and changes nothing, so
  the URL reveals nothing about who was emailed.

`apply_unsubscribe` does everything in one transaction, so a crash can't leave it half applied:
suppress the address (and the whole company domain for company addresses, not free-mail), move
the matching leads to `do_not_contact`, cancel emails not yet sent, and close pending drafts.
Repeating it is harmless. The audit log gets the domain only, never the address.
"""

import html
import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import approvals
from app.channel import ChannelError
from app.config import Settings
from app.db.audit import audit
from app.db.models import Approval, EmailDraft, EmailSend, Lead
from app.deps import Deps
from app.leads import TransitionError, email_domain, transition
from app.outreach.present import window_open
from app.owners import active_owners
from app.suppression import is_suppressed, suppress

log = logging.getLogger(__name__)

ACTOR = "unsubscribe"
CANCELLABLE = ("queued", "held")  # not yet handed to Gmail; `sending` can't be undone here (#49 re-checks)


@dataclass
class Unsubscribed:
    address: str = ""
    domain: str | None = None
    leads: int = 0
    emails_cancelled: int = 0
    drafts_closed: int = 0  # drafts owners were about to act on (pending, or decided but not yet queued)
    already: bool = False
    business_names: list[str] = field(default_factory=list)
    closed_approvals: list[Approval] = field(default_factory=list)

    @property
    def cancelled_something(self) -> bool:
        return bool(self.emails_cancelled or self.drafts_closed)


async def find_send(session: AsyncSession, token: str) -> EmailSend | None:
    if not token or len(token) > 100:
        return None
    return await session.scalar(select(EmailSend).where(EmailSend.unsubscribe_token == token))


async def apply_unsubscribe(session: AsyncSession, send: EmailSend) -> Unsubscribed:
    """Apply an opt-out for the address this email was sent to. Caller commits."""
    address = send.intended_to.strip().lower()
    # A test email (to a tester's own address, maybe on the company's domain) opts out that
    # address only: the whole domain would stay suppressed forever.
    domain = email_domain(address) if send.mode == "live" else None
    result = Unsubscribed(address=address, domain=domain)

    result.already = await is_suppressed(session, address)
    await suppress(session, address, "unsubscribe", actor=ACTOR)
    if domain:
        await suppress(session, domain, "unsubscribe", actor=ACTOR)

    # Exact matches only (never LIKE: "_" in an address is a wildcard and would hit other people).
    same_company = Lead.email_domain == domain if domain else func.lower(Lead.email) == address
    # Locked (in id order): an approval queueing an email for one of them finishes first, so its
    # email row is committed before we cancel emails below.
    leads = list(
        (
            await session.execute(
                select(Lead)
                .where(same_company)
                .order_by(Lead.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalars()
    )
    lead_ids = [lead.id for lead in leads]
    for lead in leads:
        if lead.status == "do_not_contact":
            continue
        for _attempt in range(3):  # an opt-out always applies: re-read and retry if it changed meanwhile
            try:
                await transition(
                    session,
                    lead.id,
                    "do_not_contact",
                    expected_version=lead.version,
                    actor=ACTOR,
                    reason="unsubscribed",
                )
                break
            except TransitionError:
                await session.refresh(lead)
                if lead.status == "do_not_contact":
                    break
        else:
            raise TransitionError(f"lead {lead.id} kept changing; opt-out not applied")
        result.leads += 1
        if lead.business_name and lead.business_name not in result.business_names:
            result.business_names.append(lead.business_name)

    same_recipient = (
        func.lower(EmailSend.intended_to).endswith(f"@{domain}", autoescape=True)
        if domain
        else func.lower(EmailSend.intended_to) == address
    )
    cancelled = await session.execute(
        update(EmailSend)
        .where(EmailSend.status.in_(CANCELLABLE), same_recipient)
        .values(status="cancelled", error="unsubscribed")
        .returning(EmailSend.idempotency_key)
        .execution_options(synchronize_session=False)
    )
    result.emails_cancelled = len(cancelled.all())

    if lead_ids:
        result.closed_approvals, result.drafts_closed = await close_pending_drafts(session, lead_ids)

    audit(
        session,
        ACTOR,
        "unsubscribed",
        "suppression",
        domain or "free-mail address",
        leads=result.leads,
        emails_cancelled=result.emails_cancelled,
        drafts_closed=result.drafts_closed,
        already=result.already,
    )
    return result


async def close_pending_drafts(session: AsyncSession, lead_ids: list[int]) -> tuple[list[Approval], int]:
    """Close outreach drafts that involve these leads. Returns the approvals closed (rejected by
    `system` with decision `unsubscribed`; the caller resumes their graphs) and how many drafts
    owners were about to act on.

    A draft with no open approval is either still being written (`drafting`: cancelled quietly,
    the graph then stays closed) or between two approvals / decided but not yet queued (`pending`:
    cancelled and counted, so owners are told; the queueing step re-checks and queues nothing)."""
    drafts = list(
        (
            await session.execute(
                select(EmailDraft).where(
                    EmailDraft.status.in_(("drafting", "pending")),
                    or_(EmailDraft.lead_id.in_(lead_ids), EmailDraft.lead_ids.overlap(lead_ids)),
                )
            )
        ).scalars()
    )
    closed: list[Approval] = []
    without_approval = 0
    for draft in drafts:
        open_approval = await session.scalar(
            select(Approval).where(
                Approval.item_type == "outreach_email",
                Approval.item_id == str(draft.id),
                Approval.status.in_(("pending", "editing")),
            )
        )
        if open_approval is None:
            if draft.status == "pending":
                without_approval += 1
            draft.status = "cancelled"
            continue
        decision = {
            "action": "unsubscribed",
            "by": "system",
            "by_name": "system",
            "approval_id": open_approval.id,
        }
        won = await approvals.transition(
            session,
            open_approval.id,
            expected_status=open_approval.status,
            version=open_approval.version,
            new_status="rejected",
            owner_phone="system",
            decision=decision,
        )
        if won is not None:
            audit(session, ACTOR, "approval_closed_by_unsubscribe", "approval", won.id)
            closed.append(won)
    return closed, len(closed) + without_approval


async def tell_owners(deps: Deps, result: Unsubscribed, send_key: str) -> None:
    """Right away, only when the opt-out cancelled something owners were about to act on."""
    if not result.cancelled_something:
        return
    names = ", ".join(result.business_names) or "A prospect"
    parts = []
    if result.drafts_closed:
        parts.append(f"{result.drafts_closed} draft(s) waiting for approval were closed")
    if result.emails_cancelled:
        parts.append(f"{result.emails_cancelled} queued email(s) were cancelled")
    text = f"🚫 {names} unsubscribed. {' and '.join(parts)}. They won't be emailed again."
    async with deps.sessionmaker() as session:
        owners = await active_owners(session)
    for owner in owners:
        if not window_open(owner):
            # Meta doesn't deliver free-form text outside the 24 h window: say so honestly instead
            # of pretending. The owner sees the result in the daily digest (#12).
            log.warning("owner %s not told of an unsubscribe: 24 h window closed", owner.name)
            continue
        try:
            await deps.channel.send_text(owner.phone, text, key=f"unsubscribe:{send_key}:{owner.phone}")
        except ChannelError:
            log.exception("could not tell %s about an unsubscribe", owner.phone)


# --- the page ---------------------------------------------------------------

_STYLE = (
    "body{font-family:system-ui,sans-serif;max-width:34rem;margin:3rem auto;padding:0 1rem;color:#222}"
    "button{font-size:1rem;padding:.7rem 1.4rem;border:0;border-radius:.4rem;background:#1f4e79;color:#fff;"
    "cursor:pointer}hr{border:0;border-top:1px solid #ddd;margin:2rem 0}"
)


def _page(title: str, body: str) -> str:
    return (
        '<!doctype html><html lang="fr"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex,nofollow">'
        f"<title>{html.escape(title)}</title><style>{_STYLE}</style></head><body>{body}</body></html>"
    )


def confirm_page(settings: Settings, token: str) -> str:
    company = html.escape(settings.company_name)
    return _page(
        "Désabonnement / Unsubscribe",
        f"<h1>{company}</h1>"
        "<p>Voulez-vous ne plus recevoir de courriels de notre part?</p>"
        f'<form method="post" action="/u/{html.escape(token, quote=True)}">'
        '<button type="submit">Me désabonner</button></form>'
        "<hr><p>Do you want to stop receiving emails from us?</p>"
        f'<form method="post" action="/u/{html.escape(token, quote=True)}">'
        '<button type="submit">Unsubscribe</button></form>'
        f"{_contact_line(settings)}",
    )


def done_page(settings: Settings, known: bool) -> str:
    contact = _contact_line(settings)
    if known:
        body = (
            "<h1>✓</h1><p>Vous êtes désabonné. Vous ne recevrez plus de courriels de notre part.</p>"
            "<hr><p>You are unsubscribed. You will not receive any more emails from us.</p>"
        )
    else:
        body = (
            "<p>Ce lien n'est plus valide. Si vous recevez encore nos courriels, écrivez-nous et nous "
            "vous retirerons de notre liste.</p><hr><p>This link is no longer valid. If you still receive "
            "our emails, write to us and we will remove you from our list.</p>"
        )
    return _page("Désabonnement / Unsubscribe", body + contact)


def _contact_line(settings: Settings) -> str:
    if not settings.company_email:
        return ""
    email = html.escape(settings.company_email)
    company = html.escape(settings.company_name)
    return f'<hr><p style="color:#666">{company} — <a href="mailto:{email}">{email}</a></p>'


def page_headers() -> dict[str, Any]:
    return {
        "Cache-Control": "no-store",
        "X-Robots-Tag": "noindex, nofollow",
        "Referrer-Policy": "no-referrer",
    }
