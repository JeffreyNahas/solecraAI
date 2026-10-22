"""Run one job by name with run tracking, alerts and issue reporting.

    uv run python -m scripts.run_job selftest_fail
    docker compose exec api python -m scripts.run_job selftest_fail     # on the server

Exit code 0 if the job succeeded, 1 if it failed.
"""

import argparse
import asyncio
import logging
import sys

from app.config import get_settings
from app.jobs import JOBS, run_job
from app.jobs.runtime import job_deps


async def main(name: str) -> int:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    async with job_deps(settings) as deps:
        run_id, status = await run_job(deps, name)
    print(f"run {run_id}: {status}")
    return 0 if status == "succeeded" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("job", choices=sorted(JOBS))
    sys.exit(asyncio.run(main(parser.parse_args().job)))
