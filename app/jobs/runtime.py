"""Builds the shared services (DB, WhatsApp, GitHub reporter, LLM) for processes that run jobs:
the scheduler container and `scripts/run_job.py`."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from app.config import Settings
from app.db.session import make_engine, make_sessionmaker
from app.deps import Deps
from app.llm import LLM
from app.owners import sync_owners
from app.tools.github_issues import GitHubIssueReporter
from app.whatsapp.client import WhatsAppClient


@asynccontextmanager
async def job_deps(settings: Settings) -> AsyncIterator[Deps]:
    engine = make_engine(settings)
    sessionmaker = make_sessionmaker(engine)
    await sync_owners(sessionmaker, settings.owners())
    wa = WhatsAppClient(settings, sessionmaker)
    issues = GitHubIssueReporter(settings.github_issues_token, settings.github_repo, sessionmaker)
    llm = LLM(settings) if settings.openrouter_api_key else None
    try:
        yield Deps(settings, sessionmaker, wa, issues, llm)
    finally:
        await wa.aclose()
        await issues.aclose()
        if llm:
            await llm.aclose()
        await engine.dispose()
