"""Daily outreach drafting job (CLAUDE.md §7, 08:00 on Québec business days; issue #47).

1. Skip weekends, Québec holidays and paused outreach (`settings.outreach_paused`).
2. Expire drafts nobody decided within OUTREACH_DRAFT_EXPIRY_BUSINESS_DAYS (lead back to qualified).
3. Finish draft runs a crash stopped before they paused for the owners.
4. Pick qualified leads with a verified contact, one email per company:
   - same company = same company email domain, or the same address for free-mail;
   - a company already emailed or in progress isn't emailed again: the lead is linked to it
     (`primary_lead_id`) and left out (decided 2026-10-22);
   - suppressed addresses → do_not_contact; consent evidence older than
     OUTREACH_CONSENT_MAX_AGE_DAYS is re-checked first.
5. Up to the daily cap (`settings.outreach_daily_cap`, minus drafts already made today): create the
   draft, move its leads qualified → drafted, run the graph until it pauses for approval.
6. Tell the owners (`app/outreach/present.morning`).
"""

import logging
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import false, func, or_, select, update
from sqlalchemy.orm import aliased

from app import approvals
from app.db.audit import audit
from app.db.models import Approval, EmailDraft, EmailSend, Lead
from app.deps import Deps
from app.graphs.outreach_email import ITEM_TYPE, build_outreach_graph, move_leads, run_id_for
from app.graphs.resume import deliver
from app.guards.consent import verify_consent
from app.leads import TransitionError, transition
from app.llm import LLMError
from app.outreach import present
from app.outreach.calendar import business_days_between, is_business_day
from app.outreach.render import Identity
from app.runs import RunHandle
from app.settings_store import outreach_daily_cap, outreach_paused
from app.suppression import is_suppressed
from app.tools.web import PageFetcher

log = logging.getLogger(__name__)

ACTOR = "outreach_draft"
# Statuses meaning "this company has been emailed or is being emailed".
CONTACTED = (
    "drafted",
    "approved",
    "sent",
    "follow_up_1",
    "follow_up_2",
    "replied",
    "site_visit_booked",
    "won",
    "lost",
)
STUCK_AFTER = timedelta(minutes=10)


def company_key(lead: Lead) -> str:
    return lead.email_domain or (lead.email or "").strip().lower()


async def outreach_draft_job(deps: Deps, run: RunHandle, now: datetime | None = None) -> None:
    tz = ZoneInfo(deps.settings.timezone)
    now_local = (now or datetime.now(UTC)).astimezone(tz)
    if not is_business_day(now_local.date()):
        run.add("not_a_business_day")
        return
    async with deps.sessionmaker() as session:
        if await outreach_paused(session):
            run.add("paused")
            return
    Identity.from_settings(deps.settings)  # CASL footer: refuse to draft without the company identity
    if deps.checkpointer is None:
        raise RuntimeError("outreach drafting needs a LangGraph checkpointer (job_deps)")
    graph = build_outreach_graph(deps, deps.checkpointer)

    await expire_stale(deps, graph, run, now_local)
    await finish_stuck(deps, graph, run)
    await unlink_stale(deps, run)

    midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
    async with deps.sessionmaker() as session:
        cap = await outreach_daily_cap(session, deps.settings.outreach_daily_cap)
        made_today = await session.scalar(
            select(func.count()).select_from(EmailDraft).where(EmailDraft.created_at >= midnight)
        )
    budget = cap - int(made_today or 0)
    if budget <= 0:
        run.add("daily_cap_reached")
    else:
        groups = await pick_groups(deps, run, budget)
        if groups and deps.llm is None:
            raise LLMError(f"no LLM configured (OPENROUTER_API_KEY empty); {len(groups)} companies waiting")
        for leads in groups:
            await draft_company(deps, graph, run, leads)
        if groups and run.counts.get("draft_errors", 0) == len(groups):
            raise LLMError(f"drafting failed for all {len(groups)} companies (see the logs)")

    shown = await present.morning(deps, now_local.date().isoformat())
    run.add("pending_for_owners", shown)


async def expire_stale(deps: Deps, graph: Any, run: RunHandle, now_local: datetime) -> None:
    """Undecided drafts expire: pending ones counted from when they were asked, ones an owner
    started editing but never finished from the Edit tap (else they'd block every other draft)."""
    limit = deps.settings.outreach_draft_expiry_business_days
    async with deps.sessionmaker() as session:
        open_approvals = list(
            (
                await session.execute(
                    select(Approval).where(
                        Approval.item_type == ITEM_TYPE, Approval.status.in_(("pending", "editing"))
                    )
                )
            ).scalars()
        )
    for approval in open_approvals:
        editing = approval.status == "editing" and approval.decided_at is not None
        since = approval.decided_at if editing else approval.created_at
        if business_days_between(since.astimezone(now_local.tzinfo).date(), now_local.date()) < limit:
            continue
        decision = {"action": "expire", "by": "system", "by_name": "system", "approval_id": approval.id}
        async with deps.sessionmaker() as session:
            won = await approvals.transition(
                session,
                approval.id,
                expected_status=approval.status,
                version=approval.version,
                new_status="rejected",
                owner_phone="system",
                decision=decision,
            )
            if won:
                audit(session, "system", "approval_expired", "approval", approval.id)
            await session.commit()
        if won:  # an owner may have tapped in the meantime: first action wins
            await deliver(graph, deps.sessionmaker, won)
            run.add("drafts_expired")


async def finish_stuck(deps: Deps, graph: Any, run: RunHandle) -> None:
    """Draft runs a crash stopped before their pause for approval: continue (or start) them."""
    cutoff = datetime.now(UTC) - STUCK_AFTER
    async with deps.sessionmaker() as session:
        drafts = list(
            (
                await session.execute(
                    select(EmailDraft).where(
                        EmailDraft.status.in_(("drafting", "pending")), EmailDraft.updated_at < cutoff
                    )
                )
            ).scalars()
        )
    for draft in drafts:
        async with deps.sessionmaker() as session:
            owner_decision_open = await session.scalar(
                select(Approval.id)
                .where(
                    Approval.run_id == draft.run_id,
                    Approval.status.in_(approvals.FINAL_STATUSES),
                    Approval.resumed_at.is_(None),
                )
                .limit(1)
            )
        if owner_decision_open is not None:
            # An owner's decision is being delivered by the API (or will be at its startup):
            # running the graph here too would run the same step twice.
            continue
        config = {"configurable": {"thread_id": draft.run_id}}
        state = await graph.aget_state(config)
        if state.interrupts:
            continue  # paused for the owners: fine
        try:
            if state.next:
                await graph.ainvoke(None, config)
            elif not state.values:
                await graph.ainvoke({"run_id": draft.run_id, "draft_id": draft.id}, config)
            else:
                continue
            run.add("stuck_runs_finished")
        except Exception as exc:
            await give_up(deps, draft, exc)
            run.add("draft_errors")


async def unlink_stale(deps: Deps, run: RunHandle) -> None:
    """Release leads linked to a company draft that ended without an email (expired, rejected,
    blocked, failed): their company was never emailed, so they're grouped and drafted again."""
    primary = aliased(Lead)
    contacted = select(primary.id).where(primary.id == Lead.primary_lead_id, primary.status.in_(CONTACTED))
    emailed = select(EmailSend.idempotency_key).where(
        EmailSend.lead_id == Lead.primary_lead_id, EmailSend.status != "cancelled"
    )
    async with deps.sessionmaker() as session:
        released = await session.execute(
            update(Lead)
            .where(
                Lead.status == "qualified",
                Lead.primary_lead_id.is_not(None),
                ~contacted.exists(),
                ~emailed.exists(),
            )
            .values(primary_lead_id=None)
            .returning(Lead.id)
            .execution_options(synchronize_session=False)
        )
        count = len(released.all())
        await session.commit()
    if count:
        run.add("links_released", count)


async def pick_groups(deps: Deps, run: RunHandle, budget: int) -> list[list[Lead]]:
    """Qualified leads with a verified contact, grouped by company, best score first."""
    async with deps.sessionmaker() as session:
        candidates = list(
            (
                await session.execute(
                    select(Lead)
                    .where(
                        Lead.status == "qualified",
                        Lead.email.is_not(None),
                        Lead.consent_source_url.is_not(None),
                        Lead.primary_lead_id.is_(None),
                    )
                    .order_by(Lead.score.desc().nulls_last(), Lead.id)
                )
            ).scalars()
        )
    by_company: dict[str, list[Lead]] = defaultdict(list)
    for lead in candidates:
        if await link_to_contacted(deps, run, lead):  # cheap DB check before any web fetch
            continue
        if not await usable(deps, run, lead):
            continue
        by_company[company_key(lead)].append(lead)

    groups = sorted(by_company.values(), key=lambda g: -(g[0].score or 0))[:budget]
    async with deps.sessionmaker() as session:  # siblings point to the lead that gets the email
        for group in groups:
            for sibling in group[1:]:
                await session.execute(
                    update(Lead).where(Lead.id == sibling.id).values(primary_lead_id=group[0].id)
                )
        await session.commit()
    return groups


async def usable(deps: Deps, run: RunHandle, lead: Lead) -> bool:
    """Suppressed → do_not_contact; old consent evidence re-checked (CASL)."""
    async with deps.sessionmaker() as session:
        if await is_suppressed(session, lead.email or ""):
            try:
                await transition(
                    session,
                    lead.id,
                    "do_not_contact",
                    expected_version=lead.version,
                    actor=ACTOR,
                    reason="suppressed",
                )
                await session.commit()
            except TransitionError:
                await session.rollback()
            run.add("suppressed")
            return False
    max_age = timedelta(days=deps.settings.outreach_consent_max_age_days)
    captured = lead.consent_captured_at
    if captured is not None and datetime.now(UTC) - captured < max_age:
        return True
    fetcher = deps.web or PageFetcher()
    try:
        check = await verify_consent(
            fetcher,
            email=lead.email or "",
            source_url=lead.consent_source_url or "",
            business_name=lead.business_name or "",
            website=lead.website,
        )
    finally:
        if deps.web is None:
            await fetcher.aclose()
    run.add("consent_rechecked")
    if check.retryable:
        run.add("consent_recheck_retry_later")
        return False
    async with deps.sessionmaker() as session:
        if check.ok:
            await session.execute(
                update(Lead).where(Lead.id == lead.id).values(consent_captured_at=datetime.now(UTC))
            )
            audit(session, ACTOR, "consent_rechecked", "lead", lead.id, ok=True)
            await session.commit()
            lead.consent_captured_at = datetime.now(UTC)
            return True
        try:
            await transition(
                session,
                lead.id,
                "no_valid_contact",
                expected_version=lead.version,
                actor=ACTOR,
                reason=check.reason,
            )
            await session.commit()
        except TransitionError:
            await session.rollback()
    run.add("consent_no_longer_valid")
    return False


async def link_to_contacted(deps: Deps, run: RunHandle, lead: Lead) -> bool:
    """If this company was already emailed (or is being), link the lead and leave it out."""
    domain, email = lead.email_domain, (lead.email or "").strip().lower()
    same_company = Lead.email_domain == domain if domain else func.lower(Lead.email) == email
    async with deps.sessionmaker() as session:
        other = await session.scalar(
            select(Lead)
            .where(same_company, Lead.status.in_(CONTACTED), Lead.id != lead.id)
            .order_by(Lead.id)
            .limit(1)
        )
        sent_lead = None
        if other is None:
            sent_lead = await session.scalar(
                select(EmailSend.lead_id)
                .where(
                    EmailSend.status != "cancelled",
                    or_(
                        EmailSend.intended_to == email,
                        func.split_part(EmailSend.intended_to, "@", 2) == domain if domain else false(),
                    ),
                )
                .limit(1)
            )
        target = (other.primary_lead_id or other.id) if other is not None else sent_lead
        if target is None:
            return False
        await session.execute(update(Lead).where(Lead.id == lead.id).values(primary_lead_id=target))
        audit(session, ACTOR, "lead_linked_to_contacted_company", "lead", lead.id, primary_lead_id=target)
        await session.commit()
    run.add("already_contacted")
    return True


async def draft_company(deps: Deps, graph: Any, run: RunHandle, leads: list[Lead]) -> None:
    primary = leads[0]
    async with deps.sessionmaker() as session:
        draft = EmailDraft(
            lead_id=primary.id, lead_ids=[lead.id for lead in leads], language=primary.language
        )
        session.add(draft)
        await session.flush()
        draft.run_id = run_id_for(draft.id)
        for lead in leads:
            try:
                await transition(session, lead.id, "drafted", expected_version=lead.version, actor=ACTOR)
            except TransitionError:
                await session.rollback()
                run.add("draft_conflicts")
                return
        await session.commit()
        draft_id, run_id = draft.id, draft.run_id

    try:
        await graph.ainvoke({"run_id": run_id, "draft_id": draft_id}, {"configurable": {"thread_id": run_id}})
    except Exception as exc:
        log.exception("drafting failed for draft %s", draft_id)
        async with deps.sessionmaker() as session:
            draft = await session.get(EmailDraft, draft_id)
        await give_up(deps, draft, exc)
        run.add("draft_errors")
        if isinstance(exc, LLMError):
            run.add("llm_cost_microusd", round(exc.cost_usd * 1_000_000))
        return

    async with deps.sessionmaker() as session:
        draft = await session.get(EmailDraft, draft_id)
    run.add("drafts_ready" if draft.status == "pending" else f"drafts_{draft.status}")
    run.add("llm_cost_microusd", round(float((draft.llm or {}).get("cost_usd", 0)) * 1_000_000))


async def give_up(deps: Deps, draft: EmailDraft, exc: Exception) -> None:
    """A draft that errored: mark it blocked, leads back to qualified (another day)."""
    async with deps.sessionmaker() as session:
        row = await session.get(EmailDraft, draft.id)
        if row.status in ("drafting", "pending"):
            row.status = "blocked"
            row.guard = {"ok": False, "violations": [], "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
            await move_leads(session, row.lead_ids, "drafted", "qualified", reason="drafting failed")
        await session.commit()
