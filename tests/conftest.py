"""Tests run against a real Postgres (TEST_DATABASE_URL). The schema is
rebuilt with Alembic before each test, so migrations are tested too."""

import os
from contextlib import asynccontextmanager

import httpx
import psycopg
import pytest
from alembic import command
from alembic.config import Config

from app.config import Settings
from app.main import create_app
from tests.helpers import APP_SECRET, OWNER_1, FakeChannel

TEST_DB = os.environ.get("TEST_DATABASE_URL", "postgresql://solecra:solecra@localhost:5432/solecra_test")


def _reset_schema() -> None:
    try:
        conn = psycopg.connect(TEST_DB, autocommit=True, connect_timeout=3)
    except psycopg.OperationalError as exc:
        pytest.skip(f"test database not reachable ({exc}); set TEST_DATABASE_URL")
    with conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
    cfg = Config("alembic.ini")
    cfg.attributes["url"] = TEST_DB.replace("postgresql://", "postgresql+psycopg://", 1)
    command.upgrade(cfg, "head")


@pytest.fixture
def settings() -> Settings:
    _reset_schema()
    return Settings(
        _env_file=None,
        database_url=TEST_DB,
        whatsapp_verify_token="verify-me",
        whatsapp_app_secret=APP_SECRET,
        owner_phones=f"{OWNER_1}:Alice,+1 (514) 555-0002:Bob",
        worker_poll_seconds=0.1,
    )


@pytest.fixture
def channel() -> FakeChannel:
    return FakeChannel()


@pytest.fixture
def running_app(settings, channel):
    """Factory: `async with running_app() as (app, client)` starts a fresh app.
    Calling it twice in one test simulates a restart (same DB, new process state)."""

    @asynccontextmanager
    async def _run():
        app = create_app(settings, channel=channel)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                yield app, client

    return _run
