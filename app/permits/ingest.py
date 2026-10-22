"""Permits → leads pipeline (CLAUDE.md §7, 06:00 job; issue #7).

    permits (any source) → filters → insert_lead (dedupe) → cheap-model score → keep or disqualify

The source is passed in, so this works the same for the Montréal open-data client and for tests.
Scoring also picks up leads left unscored by an earlier run (e.g. the model was down).
Each lead is committed on its own: one bad record never loses the rest of the run.
"""

import logging
from collections.abc import Iterable

from sqlalchemy import select, update

from app.db.models import AuditLog, Lead, Run
from app.deps import Deps
from app.leads import LeadIn, TransitionError, insert_lead, reopen, transition
from app.llm import LLMError
from app.permits.filters import reject_reason
from app.permits.model import Permit
from app.runs import RunHandle
from app.scoring import score_permit

log = logging.getLogger(__name__)

SOURCE = "permit"
MAX_OTHER_PERMITS = 10  # later permits kept on a lead (newest last)
OVERLAP_DAYS = 14


def to_lead(permit: Permit) -> LeadIn:
    details = permit.model_dump(mode="json", exclude={"address", "latitude", "longitude"})
    return LeadIn(
        source=SOURCE,
        source_ref=permit.permit_id,
        address=permit.address,
        language="fr",
        source_details=details,
    )


async def ingest_permits(deps: Deps, run: RunHandle, permits: Iterable[Permit]) -> None:
    for permit in permits:
        run.add("seen")
        reason = reject_reason(permit, deps.settings)
        if reason:
            run.add("filtered_out")
            run.add(f"filtered_{reason.split(':')[0]}")
            continue
        async with deps.sessionmaker() as session:
            lead, created = await insert_lead(session, to_lead(permit))
            outcome = "inserted" if created else await _add_permit_to_lead(session, deps, lead, permit)
            await session.commit()
        run.add(outcome)
    await score_pending(deps, run)


def _compact(permit: Permit) -> dict[str, object]:
    return {
        "permit_id": permit.permit_id,
        "issued_on": permit.issued_on.isoformat() if permit.issued_on else None,
        "work": permit.work.value,
        "work_label": permit.work_label,
        "building": permit.building.value,
        "units": permit.units,
        "description": (permit.description or "")[:300],
    }


async def _add_permit_to_lead(session, deps: Deps, lead: Lead, permit: Permit) -> str:
    """Another permit at an address that already has a lead (decided 2026-10-22, #43).

    The permit is recorded on the lead (`source_details.other_permits`). If the lead is still `new`,
    or was disqualified only by its score, it is scored again with every permit at the address, so
    e.g. a fit-out after a roof repair isn't lost. Leads further along keep the permit on record.
    Returns the run counter to bump.
    """
    details = dict(lead.source_details or {})
    known = {lead.source_ref, *details.get("permit_ids", [])}
    if lead.source != SOURCE or permit.permit_id in known:
        return "duplicates"
    details["permit_ids"] = sorted((known - {lead.source_ref}) | {permit.permit_id})
    details["other_permits"] = [*details.get("other_permits", []), _compact(permit)][-MAX_OTHER_PERMITS:]
    disqualified_by_score = lead.status == "disqualified" and await _disqualified_by_score(session, lead.id)
    rescore = lead.status == "new" or disqualified_by_score
    values: dict[str, object] = {"source_details": details, "version": Lead.version + 1}
    if rescore:
        values.update(score=None, score_reason=None)
    updated = await session.execute(
        update(Lead)
        .where(Lead.id == lead.id, Lead.version == lead.version)
        .values(**values)
        .returning(Lead.version)
        .execution_options(synchronize_session=False)
    )
    if updated.scalar_one_or_none() is None:
        return "permit_conflicts"
    if disqualified_by_score:
        await reopen(
            session,
            lead.id,
            expected_version=lead.version + 1,
            actor="system",
            reason=f"new permit {permit.permit_id} at the same address",
        )
    return "rescored" if rescore else "permits_added"


async def _disqualified_by_score(session, lead_id: int) -> bool:
    """True if the lead's latest status change was the scoring step disqualifying it."""
    last = (
        await session.execute(
            select(AuditLog.details)
            .where(
                AuditLog.action == "lead_status",
                AuditLog.entity == "lead",
                AuditLog.entity_id == str(lead_id),
            )
            .order_by(AuditLog.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return (
        bool(last) and last.get("to") == "disqualified" and str(last.get("reason") or "").startswith("score ")
    )


async def score_pending(deps: Deps, run: RunHandle) -> None:
    """Score permit leads that are still `new` and unscored, up to the per-run cap.

    Raises LLMError when leads were waiting and none could be scored (no LLM configured, bad key,
    wrong model id…), so the run is marked failed and owners/Jeff are alerted. Inserted leads are
    already committed and get scored by the next run.
    """
    async with deps.sessionmaker() as session:
        leads = (
            (
                await session.execute(
                    select(Lead)
                    .where(Lead.source == SOURCE, Lead.status == "new", Lead.score.is_(None))
                    .order_by(Lead.id)
                    .limit(deps.settings.scoring_max_per_run)
                )
            )
            .scalars()
            .all()
        )
    if not leads:
        return
    if deps.llm is None:
        run.add("scoring_skipped", len(leads))
        raise LLMError(
            f"no LLM configured (OPENROUTER_API_KEY empty); {len(leads)} leads waiting for a score"
        )

    for lead in leads:
        permit = Permit.model_validate({**(lead.source_details or {}), "permit_id": lead.source_ref})
        try:
            result = await score_permit(
                deps.llm, permit, (lead.source_details or {}).get("other_permits", [])
            )
        except LLMError as exc:
            log.exception("scoring failed for lead %s", lead.id)
            run.add("scoring_errors")
            run.add("llm_cost_microusd", round(exc.cost_usd * 1_000_000))  # failed calls are billed too
            run.add("llm_tokens", exc.tokens)
            continue
        run.add("scored")
        run.add("llm_cost_microusd", round(result.cost_usd * 1_000_000))
        run.add("llm_tokens", result.prompt_tokens + result.completion_tokens)
        await _apply_score(deps, run, lead, result.data.score, result.data.reason)

    if run.counts.get("scored", 0) == 0:
        raise LLMError(f"scoring failed for all {len(leads)} leads (see scoring_errors and the logs)")


async def _apply_score(deps: Deps, run: RunHandle, lead: Lead, score: int, reason: str) -> None:
    async with deps.sessionmaker() as session:
        # Only write if nobody changed the lead since we read it (version check).
        updated = await session.execute(
            update(Lead)
            .where(Lead.id == lead.id, Lead.version == lead.version, Lead.score.is_(None))
            .values(score=score, score_reason=reason)
            .returning(Lead.version)
        )
        if updated.scalar_one_or_none() is None:
            run.add("score_conflicts")
            return
        if score < deps.settings.lead_score_threshold:
            try:
                await transition(
                    session,
                    lead.id,
                    "disqualified",
                    expected_version=lead.version,
                    actor="system",
                    reason=f"score {score} < {deps.settings.lead_score_threshold}",
                )
                run.add("below_threshold")
            except TransitionError:
                run.add("score_conflicts")
        else:
            run.add("above_threshold")
        await session.commit()


async def permits_ingest_job(deps: Deps, run: RunHandle) -> None:
    """Daily job: pull recent Montréal permits and turn the relevant ones into scored leads.

    Fetches permits issued since the last successful run minus a 14-day overlap (the city
    publishes weekly), so an outage of any length is caught up; first run: PERMITS_LOOKBACK_DAYS.
    Dedupe absorbs the overlap.
    """
    from datetime import date, timedelta

    from app.permits.montreal import MontrealPermitSource, to_permit

    async with deps.sessionmaker() as session:
        last_ok = (
            await session.execute(
                select(Run.started_at)
                .where(Run.job == run.job, Run.status == "succeeded")
                .order_by(Run.started_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    if last_ok:
        since = last_ok.date() - timedelta(days=OVERLAP_DAYS)
    else:
        since = date.today() - timedelta(days=deps.settings.permits_lookback_days)
    run.add("since_days", (date.today() - since).days)
    source = MontrealPermitSource()
    try:
        records = await source.fetch_since(since)
    finally:
        await source.aclose()
    run.add("fetched", len(records))
    await ingest_permits(deps, run, (to_permit(r) for r in records))
