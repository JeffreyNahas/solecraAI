"""Permits → leads pipeline (CLAUDE.md §7, 06:00 job; issue #7).

    permits (any source) → filters → insert_lead (dedupe) → cheap-model score → keep or disqualify

The source is passed in, so this works the same for the Montréal open-data client and for tests.
Scoring also picks up leads left unscored by an earlier run (e.g. the model was down).
Each lead is committed on its own: one bad record never loses the rest of the run.
"""

import logging
from collections.abc import Iterable

from sqlalchemy import select, update

from app.db.models import Lead
from app.deps import Deps
from app.leads import LeadIn, TransitionError, insert_lead, transition
from app.llm import LLMError
from app.permits.filters import reject_reason
from app.permits.model import Permit
from app.runs import RunHandle
from app.scoring import score_permit

log = logging.getLogger(__name__)

SOURCE = "permit"


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
            _, created = await insert_lead(session, to_lead(permit))
            await session.commit()
        run.add("inserted" if created else "duplicates")
    await score_pending(deps, run)


async def score_pending(deps: Deps, run: RunHandle) -> None:
    """Score permit leads that are still `new` and unscored, up to the per-run cap."""
    if deps.llm is None:
        log.warning("no LLM configured; scoring skipped")
        run.add("scoring_skipped")
        return
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

    for lead in leads:
        permit = Permit.model_validate({**(lead.source_details or {}), "permit_id": lead.source_ref})
        try:
            result = await score_permit(deps.llm, permit)
        except LLMError:
            log.exception("scoring failed for lead %s", lead.id)
            run.add("scoring_errors")
            continue
        run.add("scored")
        run.add("llm_cost_microusd", round(result.cost_usd * 1_000_000))
        run.add("llm_tokens", result.prompt_tokens + result.completion_tokens)
        await _apply_score(deps, run, lead, result.data.score, result.data.reason)


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

    First run (no permit leads yet) looks back PERMITS_LOOKBACK_DAYS; later runs look back 14 days,
    which covers the dataset's weekly publication with overlap (dedupe absorbs repeats).
    """
    from datetime import date, timedelta

    from app.permits.montreal import MontrealPermitSource, to_permit

    async with deps.sessionmaker() as session:
        has_leads = (await session.execute(select(Lead.id).where(Lead.source == SOURCE).limit(1))).first()
    days = 14 if has_leads else deps.settings.permits_lookback_days
    since = date.today() - timedelta(days=days)
    source = MontrealPermitSource()
    try:
        records = await source.fetch_since(since)
    finally:
        await source.aclose()
    run.add("fetched", len(records))
    await ingest_permits(deps, run, (to_permit(r) for r in records))
