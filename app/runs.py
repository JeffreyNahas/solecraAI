"""Run tracking for scheduled jobs and workflows.

    async with track_run(deps, "permits_ingest") as run:
        ...
        run.add("inserted", 12)

Every run gets a `runs` row. When a job fails:
  - the first failure of a streak sends owners a short WhatsApp alert;
  - from `RUN_FAILURE_ISSUE_THRESHOLD` failures in a row, one GitHub issue is opened/updated.
When it succeeds again after failing, owners get a "working again" message and the issue a comment.
Alerting problems are logged and never hide the job's own error.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select, update

from app.channel import ChannelError
from app.db.models import Run
from app.deps import Deps
from app.owners import active_owners

log = logging.getLogger(__name__)

# Plain-language names for owner messages. Unknown jobs fall back to their id.
JOB_LABELS: dict[str, str] = {
    "selftest_ok": "Self-test",
    "selftest_fail": "Self-test (forced failure)",
    "permits_ingest": "The morning building-permits lead search",
}


class RunHandle:
    def __init__(self, run_id: int, job: str) -> None:
        self.id = run_id
        self.job = job
        self.counts: dict[str, int] = {}

    def add(self, key: str, n: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + n


@asynccontextmanager
async def track_run(deps: Deps, job: str) -> AsyncIterator[RunHandle]:
    async with deps.sessionmaker() as session:
        run = Run(job=job, status="running", counts={})
        session.add(run)
        await session.commit()
        handle = RunHandle(run.id, job)
    log.info("run %s started: %s", handle.id, job)

    try:
        yield handle
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        await _finish(deps, handle, "failed", error)
        log.error("run %s failed: %s", handle.id, error)
        await _after_failure(deps, handle, error)
        raise
    else:
        await _finish(deps, handle, "succeeded", None)
        log.info("run %s succeeded: %s %s", handle.id, job, handle.counts)
        await _after_success(deps, handle)


async def _finish(deps: Deps, handle: RunHandle, status: str, error: str | None) -> None:
    async with deps.sessionmaker() as session:
        await session.execute(
            update(Run)
            .where(Run.id == handle.id)
            .values(status=status, counts=handle.counts, error=error, finished_at=datetime.now(UTC))
        )
        await session.commit()


async def failure_streak(deps: Deps, job: str, limit: int = 50) -> int:
    """How many of the job's most recent finished runs failed in a row."""
    async with deps.sessionmaker() as session:
        statuses = (
            await session.execute(
                select(Run.status)
                .where(Run.job == job, Run.status != "running")
                .order_by(Run.started_at.desc(), Run.id.desc())
                .limit(limit)
            )
        ).scalars()
        streak = 0
        for status in statuses:
            if status != "failed":
                break
            streak += 1
        return streak


async def _previous_status(deps: Deps, handle: RunHandle) -> str | None:
    async with deps.sessionmaker() as session:
        return (
            await session.execute(
                select(Run.status)
                .where(Run.job == handle.job, Run.id != handle.id, Run.status != "running")
                .order_by(Run.started_at.desc(), Run.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()


def _now_local(deps: Deps) -> str:
    return datetime.now(ZoneInfo(deps.settings.timezone)).strftime("%Y-%m-%d %H:%M")


async def _after_failure(deps: Deps, handle: RunHandle, error: str) -> None:
    try:
        streak = await failure_streak(deps, handle.job)
        label = JOB_LABELS.get(handle.job, handle.job)
        if streak == 1:
            await _tell_owners(
                deps,
                handle,
                f"⚠️ {label} didn't work this time ({_now_local(deps)}). "
                "It will try again at the next scheduled run, and Jeff is notified if it keeps failing.",
            )
        if deps.issues and streak >= deps.settings.run_failure_issue_threshold:
            await deps.issues.report_failure(
                job=handle.job, run_id=handle.id, error=error, streak=streak, when=_now_local(deps)
            )
    except Exception:
        log.exception("failure alerting for run %s failed", handle.id)


async def _after_success(deps: Deps, handle: RunHandle) -> None:
    try:
        if await _previous_status(deps, handle) != "failed":
            return
        label = JOB_LABELS.get(handle.job, handle.job)
        await _tell_owners(deps, handle, f"✅ {label} is working again.")
        if deps.issues:
            await deps.issues.report_recovery(job=handle.job, run_id=handle.id, when=_now_local(deps))
    except Exception:
        log.exception("recovery alerting for run %s failed", handle.id)


async def _tell_owners(deps: Deps, handle: RunHandle, text: str) -> None:
    async with deps.sessionmaker() as session:
        owners = await active_owners(session)
    for owner in owners:
        try:
            await deps.channel.send_text(owner.phone, text, key=f"run:{handle.id}:alert:{owner.phone}")
        except ChannelError:
            log.exception("could not alert %s about run %s", owner.phone, handle.id)
