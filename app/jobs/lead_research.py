"""Daily lead research job (CLAUDE.md §7, 07:00; issue #8).

    new lead, score ≥ threshold → research (model + web) → consent check (code)
        → qualified        contact found, published, checks passed
        → no_valid_contact nothing usable found, or a check failed
        → do_not_contact   the address or its domain is on the suppression list

Highest scores first, at most RESEARCH_MAX_PER_DAY leads and RESEARCH_MAX_USD_PER_DAY of model and
search spend per Montréal calendar day (manual re-runs share the same budgets). The spend cap is
checked before each lead, so one lead can take the day slightly over it. A lead whose research
errors stays `new` and is retried on a later day; after MAX_ATTEMPTS errors it goes to
`no_valid_contact`.

The email is stored only when it passes every check (Law 25: collect the minimum). The full record
(model answer, tool calls, cost, consent evidence) goes to `leads.research_details`.
"""

import logging
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Float, func, select, update

from app.db.models import Lead
from app.deps import Deps
from app.guards.consent import ConsentCheck, verify_consent
from app.leads import TransitionError, email_domain, normalize_phone, transition
from app.llm import FINAL_TOOL, LLMError
from app.permits.model import Permit
from app.research import ResearchOutcome, research_permit
from app.runs import RunHandle
from app.suppression import is_suppressed
from app.tools.web import PageFetcher

log = logging.getLogger(__name__)

ACTOR = "lead_research"
MAX_ATTEMPTS = 3
MAX_STEPS_KEPT = 30


async def lead_research_job(deps: Deps, run: RunHandle) -> None:
    settings = deps.settings
    researched, spent = await _today(deps)
    budget = settings.research_max_per_day - researched
    if budget <= 0:
        run.add("daily_cap_reached")
        return
    if spent >= settings.research_max_usd_per_day:
        run.add("daily_spend_cap_reached")
        return
    async with deps.sessionmaker() as session:
        leads = (
            (
                await session.execute(
                    select(Lead)
                    .where(
                        Lead.status == "new",
                        Lead.score >= settings.lead_score_threshold,
                        Lead.source == "permit",
                    )
                    .order_by(Lead.score.desc(), Lead.id)
                    .limit(budget)
                )
            )
            .scalars()
            .all()
        )
    if not leads:
        return
    if deps.llm is None:
        raise LLMError(f"no LLM configured (OPENROUTER_API_KEY empty); {len(leads)} leads waiting")

    fetcher = deps.web or PageFetcher()
    try:
        for lead in leads:
            spent_now = spent + run.counts.get("llm_cost_microusd", 0) / 1_000_000
            if spent_now >= settings.research_max_usd_per_day:
                run.add("daily_spend_cap_reached")
                break
            await research_lead(deps, run, fetcher, lead)
    finally:
        if deps.web is None:
            await fetcher.aclose()

    if run.counts.get("research_errors", 0) == len(leads):
        raise LLMError(f"research failed for all {len(leads)} leads (see research_errors and the logs)")


async def _today(deps: Deps) -> tuple[int, float]:
    """Leads with a research attempt since midnight Montréal time, and what they cost (USD)."""
    tz = ZoneInfo(deps.settings.timezone)
    midnight = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
    cost = Lead.research_details["cost_usd"].astext.cast(Float)
    async with deps.sessionmaker() as session:
        row = (
            await session.execute(
                select(func.count(), func.coalesce(func.sum(cost), 0.0))
                .select_from(Lead)
                .where(Lead.research_details["researched_at"].astext >= midnight.isoformat())
            )
        ).one()
    return int(row[0] or 0), float(row[1] or 0.0)


async def research_lead(deps: Deps, run: RunHandle, fetcher: PageFetcher, lead: Lead) -> None:
    assert deps.llm is not None
    now = datetime.now(UTC)
    attempts = int((lead.research_details or {}).get("attempts", 0)) + 1
    details: dict[str, Any] = {"researched_at": now.isoformat(), "attempts": attempts}
    permit = Permit.model_validate({**(lead.source_details or {}), "permit_id": lead.source_ref})

    try:
        others = (lead.source_details or {}).get("other_permits", [])
        result = await research_permit(deps.llm, fetcher, deps.settings, permit, lead.address or "", others)
    except LLMError as exc:
        log.exception("research failed for lead %s", lead.id)
        run.add("research_errors")
        run.add("llm_cost_microusd", round(exc.cost_usd * 1_000_000))
        run.add("llm_tokens", exc.tokens)
        details["last_error"] = str(exc)[:300]
        details["cost_usd"] = round(exc.cost_usd, 6)  # counts toward the daily spend cap
        final = "no_valid_contact" if attempts >= MAX_ATTEMPTS else None
        await _save(deps, run, lead, {"research_details": details}, final, reason="research failed")
        return

    run.add("researched")
    run.add("llm_cost_microusd", round(result.cost_usd * 1_000_000))
    run.add("llm_tokens", result.prompt_tokens + result.completion_tokens)
    run.add("web_searches", result.searches)
    out = result.data
    details.update(
        model=result.model,
        turns=result.turns,
        searches=result.searches,
        cost_usd=round(result.cost_usd, 6),
        steps=_public_steps(result.steps),
        answer=out.model_dump(exclude={"email", "phone", "contact_name"}),
    )
    # Public company facts are kept even when no contact is usable: they help the owners.
    values: dict[str, Any] = {
        "business_name": out.business_name,
        "website": out.website,
        "relevance_note": out.relevance_note,
    }

    if not (out.found and out.email and out.source_url and out.business_name):
        details["result"] = out.not_found_reason or "no published business email found"
        run.add("not_found")
        await _save(deps, run, lead, {**values, "research_details": details}, "no_valid_contact")
        return

    check = await verify_consent(
        fetcher,
        email=out.email,
        source_url=out.source_url,
        business_name=out.business_name,
        website=out.website,
    )
    if check.retryable:
        # The page didn't load for a passing reason (network, timeout, 5xx): try again another day.
        details["last_error"] = f"consent check: {check.reason}"
        run.add("consent_fetch_errors")
        final = "no_valid_contact" if attempts >= MAX_ATTEMPTS else None
        await _save(deps, run, lead, {"research_details": details}, final, reason=check.reason)
        return
    async with deps.sessionmaker() as session:
        suppressed = check.ok and await is_suppressed(session, out.email)
        same_company = await _same_company(session, lead.id, out.email) if check.ok else []
    if same_company:
        details["same_company_lead_ids"] = same_company
    evidence = dict(check.evidence)
    if suppressed or not check.ok:
        evidence.pop("excerpt", None)  # it contains the address, kept only for qualified leads
    details["consent_check"] = {"ok": check.ok, "reason": check.reason, "evidence": evidence}

    if suppressed:
        details["result"] = "email or domain is on the do-not-contact list"
        run.add("suppressed")
        await _save(deps, run, lead, {**values, "research_details": details}, "do_not_contact")
    elif not check.ok:
        details["result"] = check.reason
        run.add("consent_failed")
        await _save(deps, run, lead, {**values, "research_details": details}, "no_valid_contact")
    else:
        details["result"] = "qualified"
        run.add("qualified")
        values.update(_contact_values(out, check, now))
        await _save(deps, run, lead, {**values, "research_details": details}, "qualified")


def _public_steps(steps: list[dict[str, str]]) -> list[dict[str, str]]:
    """Tool calls for the record. The final answer's arguments are dropped: they hold the email,
    which is stored only if it passes the consent check."""
    return [s if s["tool"] != FINAL_TOOL else {"tool": FINAL_TOOL} for s in steps[:MAX_STEPS_KEPT]]


def _contact_values(out: ResearchOutcome, check: ConsentCheck, now: datetime) -> dict[str, Any]:
    assert out.email
    email = out.email.strip().lower()
    role = out.contact_role or (out.company_role or "").replace("_", " ") or None
    return {
        "email": email,
        "email_domain": email_domain(email),
        "contact_name": out.contact_name if out.contact_name else None,
        "role": role,
        "phone": out.phone,
        "phone_normalized": normalize_phone(out.phone),
        "consent_source_url": check.evidence.get("final_url") or out.source_url,
        "consent_captured_at": now,
        "consent_role": role,
    }


async def _same_company(session, lead_id: int, email: str) -> list[int]:
    """Other leads already linked to the same company domain (one developer, several permits)."""
    domain = email_domain(email)
    if not domain:
        return []
    rows = await session.execute(
        select(Lead.id).where(Lead.email_domain == domain, Lead.id != lead_id).order_by(Lead.id).limit(20)
    )
    return list(rows.scalars())


async def _save(
    deps: Deps,
    run: RunHandle,
    lead: Lead,
    values: dict[str, Any],
    final_status: str | None,
    reason: str | None = None,
) -> None:
    """Write the research result and move the lead, only if nobody changed it meanwhile."""
    async with deps.sessionmaker() as session:
        updated = await session.execute(
            update(Lead)
            .where(Lead.id == lead.id, Lead.version == lead.version, Lead.status == "new")
            .values(**values)
            .returning(Lead.id)
            .execution_options(synchronize_session=False)
        )
        if updated.scalar_one_or_none() is None:
            run.add("research_conflicts")
            await session.rollback()
            return
        try:
            if final_status in ("qualified",):
                moved = await transition(
                    session, lead.id, "researched", expected_version=lead.version, actor=ACTOR
                )
                await transition(session, lead.id, "qualified", expected_version=moved.version, actor=ACTOR)
            elif final_status:
                await transition(
                    session,
                    lead.id,
                    final_status,
                    expected_version=lead.version,
                    actor=ACTOR,
                    reason=reason or (values.get("research_details") or {}).get("result"),
                )
        except TransitionError:
            run.add("research_conflicts")
            await session.rollback()
            return
        await session.commit()
