from dataclasses import dataclass

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.channel import Channel
from app.config import Settings


@dataclass
class Deps:
    """Shared services passed to graphs and handlers."""

    settings: Settings
    sessionmaker: async_sessionmaker
    channel: Channel
