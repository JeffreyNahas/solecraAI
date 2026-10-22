from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.channel import Channel
from app.config import Settings

if TYPE_CHECKING:
    from app.llm import LLM
    from app.tools.github_issues import GitHubIssueReporter


@dataclass
class Deps:
    """Shared services passed to graphs, handlers and jobs."""

    settings: Settings
    sessionmaker: async_sessionmaker
    channel: Channel
    issues: "GitHubIssueReporter | None" = None
    llm: "LLM | None" = None
