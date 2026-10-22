"""Scheduler process (its own container): runs registered jobs on the daily timetable.

Times are America/Montreal (CLAUDE.md §7). Jobs not registered yet in `app/jobs.JOBS` are skipped
with a warning, so the timetable can list the whole plan while steps are still being built.

    python -m app.scheduler
"""

import asyncio
import logging
import signal

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import get_settings
from app.deps import Deps
from app.jobs import JOBS, run_job
from app.jobs.runtime import job_deps

log = logging.getLogger(__name__)

# (job name, cron expression in local time)
SCHEDULE: list[tuple[str, str]] = [
    ("permits_ingest", "0 6 * * *"),
    ("lead_research", "0 7 * * *"),
    ("outreach_draft", "0 8 * * 1-5"),  # weekdays; the job also skips Québec holidays
    # One email per tick; the job enforces hours (09:00–16:00), holidays, the random gap and the cap.
    ("email_send", "*/5 9-15 * * 1-5"),
]


def build_scheduler(deps: Deps) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=deps.settings.timezone)
    for name, cron in SCHEDULE:
        if name not in JOBS:
            log.warning("scheduled job %s is not registered yet; skipping", name)
            continue
        scheduler.add_job(
            run_job,
            CronTrigger.from_crontab(cron, timezone=deps.settings.timezone),
            args=[deps, name],
            id=name,
            max_instances=1,  # never two runs of the same job at once
            coalesce=True,  # after downtime, run once, not once per missed slot
            misfire_grace_time=3600,
        )
        log.info("scheduled %s at '%s' (%s)", name, cron, deps.settings.timezone)
    return scheduler


async def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    async with job_deps(settings) as deps:
        scheduler = build_scheduler(deps)
        scheduler.start()
        await stop.wait()
        scheduler.shutdown(wait=True)


if __name__ == "__main__":
    asyncio.run(main())
