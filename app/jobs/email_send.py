"""Send approved outreach emails, one at a time, from Gmail (CLAUDE.md §7; issue #49).

Runs every 5 minutes on weekdays 09:00–16:00 Montréal. Each run sends at most ONE email:
random 15–35 min gap after the previous one, daily cap (`settings.outreach_daily_cap`), skips
Québec holidays and paused outreach.

Safety, in order:
1. Test mode (default): only `EMAIL_TEST_RECIPIENTS` can ever receive email (also enforced inside
   `app/tools/gmail.py`). Live mode needs `app/outreach/golive.py` to report no problems.
2. A queued email is claimed `queued → sending` in one transaction after re-checking, with the
   lead locked (same order as the unsubscribe): lead still `approved`, address and domain not
   suppressed, consent evidence present, text unchanged since approval, footer and unsubscribe link intact.
3. The Gmail call is made exactly once per claim. Only a *certain* failure puts the email back in
   the queue (max 3 tries); a doubtful one (timeout, 5xx) becomes `unknown` and is never retried:
   the owners check the Sent folder. A `sending` row left by a crash becomes `unknown` after 10 min.
4. After a successful send, one transaction records the Gmail ids, a `touch`, moves the leads
   `approved → sent` and sets the next send time.
Problems (unknown, refused, expired Gmail sign-in) make the job fail, which alerts the owners
through run tracking. Messages contain no addresses.
"""

import logging
import random
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import make_msgid
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from app.channel import ChannelError
from app.config import Settings
from app.db.audit import audit
from app.db.models import EmailDraft, EmailSend, Lead, Touch
from app.deps import Deps
from app.graphs.outreach_email import move_leads
from app.guards.output import check_rendered
from app.outreach.calendar import is_business_day
from app.outreach.golive import live_problems
from app.outreach.present import window_open
from app.outreach.render import Identity, content_hash
from app.owners import active_owners
from app.runs import RunHandle
from app.settings_store import (
    EMAIL_NEXT_SEND_AT,
    email_next_send_at,
    outreach_daily_cap,
    outreach_paused,
    set_setting,
)
from app.suppression import is_suppressed
from app.tools.gmail import (
    Ambiguous,
    AuthProblem,
    GmailError,
    NotSent,
    RecipientNotAllowed,
    Rejected,
)

log = logging.getLogger(__name__)

ACTOR = "email_send"
STUCK_AFTER = timedelta(minutes=10)
MAX_ATTEMPTS = 3
MAX_SKIPS = 25  # emails dropped in one run before giving up until the next tick


@dataclass
class Claimed:
    key: str
    lead_id: int
    to: str
    subject: str
    text: str
    headers: dict[str, str]
    message_id: str
    attempts: int


def _unsubscribe_url(text: str, token: str | None) -> str | None:
    match = re.search(rf"https?://\S+/u/{re.escape(token)}\b", text) if token else None
    return match.group(0) if match else None


async def email_send_job(deps: Deps, run: RunHandle, now: datetime | None = None) -> None:
    settings = deps.settings
    now_utc = now or datetime.now(UTC)
    local = now_utc.astimezone(ZoneInfo(settings.timezone))
    problems: list[str] = []

    problems += await mark_stuck(deps, run)

    ignore_schedule = settings.email_mode == "test" and settings.email_test_ignores_schedule
    if not ignore_schedule and not (
        is_business_day(local.date())
        and settings.email_send_start_hour <= local.hour < settings.email_send_end_hour
    ):
        run.add("outside_hours")
    else:
        await send_one(deps, run, now_utc, local, ignore_schedule, problems)

    if problems:
        raise RuntimeError("; ".join(problems))


async def mark_stuck(deps: Deps, run: RunHandle) -> list[str]:
    """`sending` for over 10 minutes = the process died around the Gmail call: we can't know."""
    async with deps.sessionmaker() as session:
        stuck = list(
            (
                await session.execute(
                    select(EmailSend)
                    .where(EmailSend.status == "sending", EmailSend.updated_at < func.now() - STUCK_AFTER)
                    .with_for_update()
                )
            ).scalars()
        )
        keys = [row.idempotency_key for row in stuck]
        for row in stuck:
            row.status = "unknown"
            row.error = "interrupted while sending; check the Sent folder to see if it left"
            audit(session, ACTOR, "send_unknown", "email_send", row.idempotency_key, reason="interrupted")
        await session.commit()
    if stuck:
        run.add("marked_unknown", len(stuck))
        await alert_unknown(deps, run, keys)
    return (
        [f"{len(stuck)} email(s) may or may not have been sent (interrupted); check Gmail Sent"]
        if stuck
        else []
    )


async def send_one(
    deps: Deps, run: RunHandle, now_utc: datetime, local: datetime, ignore_schedule: bool, problems: list[str]
) -> None:
    settings = deps.settings
    async with deps.sessionmaker() as session:
        if await outreach_paused(session):
            run.add("paused")
            return
        queued = await session.scalar(
            select(func.count())
            .select_from(EmailSend)
            .where(EmailSend.status == "queued", EmailSend.mode == settings.email_mode)
        )
        if not queued:
            run.add("nothing_queued")
            return
        if settings.email_mode == "live":
            blockers = await live_problems(session, settings)
            if blockers:
                raise RuntimeError("live sending is not allowed yet: " + "; ".join(blockers))
        next_at = await email_next_send_at(session)
        if not ignore_schedule and next_at and next_at > now_utc:
            run.add("waiting_for_gap")
            return
        cap = await outreach_daily_cap(session, settings.outreach_daily_cap)
        midnight = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
        sent_today = await session.scalar(
            select(func.count())
            .select_from(EmailSend)
            .where(
                EmailSend.mode == settings.email_mode,
                EmailSend.status.in_(("sent", "sending", "unknown")),
                func.coalesce(EmailSend.sent_at, EmailSend.updated_at) >= midnight,
            )
        )
        if int(sent_today or 0) >= cap:
            run.add("daily_cap_reached")
            return
    gmail = deps.gmail
    if gmail is None or not gmail.configured:
        raise RuntimeError("Gmail is not connected (GMAIL_* settings missing in the scheduler)")
    Identity.from_settings(settings)

    claimed = await claim_next(deps, run)
    if claimed is None:
        return
    try:
        sent = await gmail.send(
            to=claimed.to,
            subject=claimed.subject,
            text=claimed.text,
            message_id=claimed.message_id,
            headers=claimed.headers,
        )
    except GmailError as exc:
        problem = await handle_failure(deps, run, claimed, exc, now_utc)
        if problem:
            problems.append(problem)
        return
    await record_sent(deps, run, claimed, sent.message_id, sent.thread_id, now_utc, ignore_schedule)


async def claim_next(deps: Deps, run: RunHandle) -> Claimed | None:
    settings = deps.settings
    for _ in range(MAX_SKIPS):
        async with deps.sessionmaker() as session:
            candidate = (
                await session.execute(
                    select(EmailSend.idempotency_key, EmailSend.lead_id)
                    .where(EmailSend.status == "queued", EmailSend.mode == settings.email_mode)
                    .order_by(EmailSend.created_at, EmailSend.idempotency_key)
                    .limit(1)
                )
            ).first()
            if candidate is None:
                return None
            key, lead_id = candidate
            # Lead first, then the email row: the unsubscribe takes locks in the same order.
            lead = (
                await session.scalar(
                    select(Lead)
                    .where(Lead.id == lead_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                if lead_id is not None
                else None
            )
            row = await session.scalar(
                select(EmailSend)
                .where(EmailSend.idempotency_key == key)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if row is None or row.status != "queued":
                continue  # someone else took it
            verdict, reason = await check_row(session, settings, row, lead)
            if verdict != "ok":
                row.status = verdict
                row.error = reason
                audit(session, ACTOR, f"send_{verdict}", "email_send", key, reason=reason)
                await session.commit()
                run.add(verdict)
                continue
            assert lead is not None
            sender_domain = settings.gmail_sender.rsplit("@", 1)[-1]
            row.status = "sending"
            row.attempts += 1
            row.actual_to = row.intended_to
            row.rfc_message_id = make_msgid(domain=sender_domain)
            row.error = None
            url = _unsubscribe_url(row.rendered_text, row.unsubscribe_token)
            claimed = Claimed(
                key=key,
                lead_id=lead.id,
                to=row.intended_to,
                subject=row.subject,
                text=row.rendered_text,
                headers={
                    "List-Unsubscribe": f"<{url}>",
                    "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
                },
                message_id=row.rfc_message_id,
                attempts=row.attempts,
            )
            audit(session, ACTOR, "send_started", "email_send", key, attempt=row.attempts)
            await session.commit()
        # Last look, as close to the Gmail call as possible: an unsubscribe that landed meanwhile.
        async with deps.sessionmaker() as session:
            if await is_suppressed(session, claimed.to):
                row = await session.get(EmailSend, claimed.key, with_for_update=True)
                row.status, row.error = "cancelled", "suppressed just before sending"
                audit(session, ACTOR, "send_cancelled", "email_send", claimed.key, reason=row.error)
                await session.commit()
                run.add("cancelled")
                continue
        return claimed
    return None


async def check_row(session, settings, row: EmailSend, lead: Lead | None) -> tuple[str, str]:
    """('ok', '') or ('cancelled' | 'failed', reason). Never trusts what was true at approval."""
    if lead is None or lead.status != "approved":
        return "cancelled", "the lead is no longer approved (unsubscribed, closed or changed)"
    if await is_suppressed(session, row.intended_to):
        return "cancelled", "address or domain is on the do-not-email list"
    if not lead.consent_source_url or lead.consent_captured_at is None:
        return "cancelled", "no consent evidence on the lead (CASL)"
    if content_hash(row.subject, row.rendered_text) != row.content_sha256:
        return "failed", "the email text changed after it was approved"
    url = _unsubscribe_url(row.rendered_text, row.unsubscribe_token)
    if url is None:
        return "failed", "the unsubscribe link is missing from the email"
    if settings.email_mode == "live" and not url.startswith(settings.public_base_url.rstrip("/") + "/u/"):
        return "failed", "the unsubscribe link does not use the current PUBLIC_BASE_URL"
    footer = check_rendered(
        row.rendered_text,
        {"List-Unsubscribe": f"<{url}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"},
        Identity.from_settings(settings).required_in_footer(),
        url,
    )
    if not footer.ok:
        return "failed", "the company identity or unsubscribe footer is incomplete: " + "; ".join(
            footer.violations
        )
    return "ok", ""


async def handle_failure(
    deps: Deps, run: RunHandle, claimed: Claimed, exc: GmailError, now_utc: datetime
) -> str | None:
    """Put the email in the right state for how the call failed. Returns a problem for the alert."""
    reason = str(exc)
    if isinstance(exc, NotSent):
        status = "failed" if claimed.attempts >= MAX_ATTEMPTS else "queued"
        problem = f"an email could not be sent after {MAX_ATTEMPTS} tries" if status == "failed" else None
        run.add("retry_later" if status == "queued" else "failed")
    elif isinstance(exc, Ambiguous):
        status, problem = (
            "unknown",
            "an email may or may not have been sent (Gmail gave no clear answer); check Gmail Sent",
        )
        run.add("unknown")
    elif isinstance(exc, AuthProblem):
        status, problem = "queued", "Gmail needs to be reconnected (sign-in expired or refused)"
        run.add("auth_problem")
    elif isinstance(exc, RecipientNotAllowed):
        status, problem = "held", "an email was held: its recipient is not on the test list"
        run.add("held")
    elif isinstance(exc, Rejected):
        status, problem = "failed", "Gmail refused an email; it will not be retried"
        run.add("failed")
    else:  # pragma: no cover - GmailError subclasses are all handled above
        status, problem = "unknown", "unexpected Gmail error"
    async with deps.sessionmaker() as session:
        row = await session.get(EmailSend, claimed.key, with_for_update=True)
        row.status, row.error = status, reason
        if isinstance(exc, AuthProblem):
            row.attempts = max(0, row.attempts - 1)  # nothing was tried: don't count it
        if status == "unknown":
            await set_setting(session, EMAIL_NEXT_SEND_AT, (now_utc + gap(deps.settings)).isoformat(), ACTOR)
        audit(session, ACTOR, f"send_{status}", "email_send", claimed.key, reason=reason)
        await session.commit()
    log.warning("email %s -> %s: %s", claimed.key, status, reason)
    if status == "unknown":
        await alert_unknown(deps, run, [claimed.key])
    return problem


async def alert_unknown(deps: Deps, run: RunHandle, keys: list[str]) -> None:
    """An email that may or may not have left needs a person, whatever the run's failure streak:
    tell the owners now (business name only) and make sure Jeff hears too (one GitHub issue)."""
    async with deps.sessionmaker() as session:
        names = list(
            (
                await session.execute(
                    select(func.coalesce(Lead.business_name, "a prospect"))
                    .join(EmailSend, EmailSend.lead_id == Lead.id)
                    .where(EmailSend.idempotency_key.in_(keys))
                )
            ).scalars()
        )
        owners = await active_owners(session)
    text = (
        f"⚠️ An email to {', '.join(names) or 'a prospect'} may or may not have left (Gmail gave no clear "
        "answer). It will NOT be sent again by itself. Please check the Sent folder of the sending Gmail "
        "and tell Jeff what you see."
    )
    for owner in owners:
        if not window_open(owner):  # free-form text isn't delivered outside the 24 h window
            log.warning("owner %s not told of an unknown send: 24 h window closed", owner.name)
            continue
        try:
            await deps.channel.send_text(owner.phone, text, key=f"email_unknown:{keys[0]}:{owner.phone}")
        except ChannelError:
            log.exception("could not tell %s about an unknown send", owner.phone)
    if deps.issues:
        try:
            await deps.issues.report_failure(
                job="email_send",
                run_id=run.id,
                error=f"{len(keys)} email(s) in state unknown (maybe sent): {', '.join(keys)}",
                streak=1,
                when=datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
            )
        except Exception:
            log.exception("could not open the GitHub issue for an unknown send")


def gap(settings: Settings) -> timedelta:
    """Random wait before the next email (decided 2026-10-22: 15–35 min)."""
    low, high = sorted((settings.email_gap_min_minutes, settings.email_gap_max_minutes))
    return timedelta(minutes=random.uniform(low, high))


async def record_sent(
    deps: Deps,
    run: RunHandle,
    claimed: Claimed,
    gmail_message_id: str,
    gmail_thread_id: str,
    now_utc: datetime,
    ignore_schedule: bool,
) -> None:
    async with deps.sessionmaker() as session:
        row = await session.get(EmailSend, claimed.key)
        draft = await session.get(EmailDraft, row.draft_id) if row.draft_id else None
        lead_ids = sorted(set(draft.lead_ids or []) | {claimed.lead_id}) if draft else [claimed.lead_id]
        # Leads before the email row, in id order: the same order the unsubscribe uses.
        await session.execute(
            select(Lead.id).where(Lead.id.in_(lead_ids)).order_by(Lead.id).with_for_update()
        )
        row = await session.get(EmailSend, claimed.key, with_for_update=True, populate_existing=True)
        row.status = "sent"
        row.sent_at = now_utc
        row.gmail_message_id, row.gmail_thread_id = gmail_message_id, gmail_thread_id
        row.error = None
        session.add(
            Touch(
                lead_id=claimed.lead_id,
                direction="out",
                type="initial",
                gmail_message_id=gmail_message_id,
                gmail_thread_id=gmail_thread_id,
                subject=claimed.subject,
                body=claimed.text,
                sent_at=now_utc,
                status="sent",
                email_send_id=claimed.key,
                rfc_message_id=claimed.message_id,
            )
        )
        await move_leads(session, lead_ids, "approved", "sent", reason="email sent")
        if not ignore_schedule:
            await set_setting(session, EMAIL_NEXT_SEND_AT, (now_utc + gap(deps.settings)).isoformat(), ACTOR)
        audit(session, ACTOR, "email_sent", "email_send", claimed.key, mode=row.mode, thread=gmail_thread_id)
        await session.commit()
    run.add("sent")
    log.info("email %s sent (gmail %s)", claimed.key, gmail_message_id)
