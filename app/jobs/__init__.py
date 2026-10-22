"""Registry of runnable jobs. The scheduler (step 9) and `scripts/run_job.py` run them by name.

A job is `async def job(deps, run) -> None`; it reports counts with `run.add(...)` and fails
by raising. `run_job` wraps it in run tracking and never raises.
"""

import logging
from collections.abc import Awaitable, Callable

from app.deps import Deps
from app.runs import RunHandle, track_run

log = logging.getLogger(__name__)

Job = Callable[[Deps, RunHandle], Awaitable[None]]


async def selftest_ok(deps: Deps, run: RunHandle) -> None:
    run.add("checks")


async def selftest_fail(deps: Deps, run: RunHandle) -> None:
    raise RuntimeError("forced failure (selftest_fail) to test alerts and issue reporting")


async def permits_ingest(deps: Deps, run: RunHandle) -> None:
    from app.permits.ingest import permits_ingest_job

    await permits_ingest_job(deps, run)


JOBS: dict[str, Job] = {
    "selftest_ok": selftest_ok,
    "selftest_fail": selftest_fail,
    "permits_ingest": permits_ingest,
}


async def run_job(deps: Deps, name: str) -> tuple[int | None, str]:
    """Run a registered job with tracking. Returns (run_id, status)."""
    job = JOBS.get(name)
    if job is None:
        raise KeyError(f"unknown job {name!r}; known: {', '.join(sorted(JOBS))}")
    run_id: int | None = None
    try:
        async with track_run(deps, name) as run:
            run_id = run.id
            await job(deps, run)
    except Exception:
        return run_id, "failed"
    return run_id, "succeeded"
