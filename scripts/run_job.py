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
from app.db.session import make_engine, make_sessionmaker
from app.deps import Deps
from app.jobs import JOBS, run_job
from app.owners import sync_owners
from app.tools.github_issues import GitHubIssueReporter
from app.whatsapp.client import WhatsAppClient


async def main(name: str) -> int:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    engine = make_engine(settings)
    sessionmaker = make_sessionmaker(engine)
    await sync_owners(sessionmaker, settings.owners())
    wa = WhatsAppClient(settings, sessionmaker)
    issues = GitHubIssueReporter(settings.github_issues_token, settings.github_repo, sessionmaker)
    try:
        run_id, status = await run_job(Deps(settings, sessionmaker, wa, issues), name)
    finally:
        await wa.aclose()
        await issues.aclose()
        await engine.dispose()
    print(f"run {run_id}: {status}")
    return 0 if status == "succeeded" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("job", choices=sorted(JOBS))
    sys.exit(asyncio.run(main(parser.parse_args().job)))
