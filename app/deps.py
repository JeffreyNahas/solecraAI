from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.channel import Channel
from app.config import Settings

if TYPE_CHECKING:
    from app.llm import LLM
    from app.tools.github_issues import GitHubIssueReporter
    from app.tools.gmail import GmailClient
    from app.tools.web import PageFetcher


@dataclass
class Deps:
    """Shared services passed to graphs, handlers and jobs."""

    settings: Settings
    sessionmaker: async_sessionmaker
    channel: Channel
    issues: "GitHubIssueReporter | None" = None
    llm: "LLM | None" = None
    web: "PageFetcher | None" = None
    checkpointer: Any = None  # LangGraph saver (Postgres) for processes that run graphs
    gmail: "GmailClient | None" = None  # only the scheduler container has Gmail credentials (§2 rule 2)
