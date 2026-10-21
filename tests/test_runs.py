"""Step 8 "done when": a forced failure shows in runs, alerts owners, and opens a single issue."""

import json

import httpx
import pytest
from sqlalchemy import select

from app.db.models import Run, Setting
from app.db.session import make_engine, make_sessionmaker
from app.deps import Deps
from app.jobs import run_job
from app.owners import sync_owners
from app.redact import redact
from app.tools.github_issues import GitHubIssueReporter
from tests.helpers import OWNER_1, OWNER_2, FakeChannel


class FakeGitHub:
    """Minimal GitHub issues API: create, get, comment."""

    def __init__(self) -> None:
        self.issues: dict[int, dict] = {}
        self.comments: dict[int, list[str]] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.split("/repos/owner/repo", 1)[1]
        body = json.loads(request.content) if request.content else {}
        if request.method == "POST" and path == "/issues":
            number = len(self.issues) + 1
            self.issues[number] = {"number": number, "state": "open", **body}
            return httpx.Response(201, json=self.issues[number])
        number = int(path.split("/")[2])
        if request.method == "GET":
            return httpx.Response(200, json=self.issues[number])
        self.comments.setdefault(number, []).append(body["body"])
        return httpx.Response(201, json={"id": 1})


@pytest.fixture
async def env(settings):
    settings = settings.model_copy(update={"run_failure_issue_threshold": 3})
    engine = make_engine(settings)
    sm = make_sessionmaker(engine)
    await sync_owners(sm, settings.owners())
    gh = FakeGitHub()

    async def no_sleep(_):
        return None

    reporter = GitHubIssueReporter(
        "token",
        "owner/repo",
        sm,
        http=httpx.AsyncClient(transport=httpx.MockTransport(gh.handler)),
        sleep=no_sleep,
    )
    channel = FakeChannel()
    yield Deps(settings, sm, channel, reporter), channel, gh
    await engine.dispose()


async def test_success_is_recorded(env):
    deps, channel, gh = env
    run_id, status = await run_job(deps, "selftest_ok")
    async with deps.sessionmaker() as s:
        run = await s.get(Run, run_id)
    assert status == "succeeded" and run.status == "succeeded" and run.counts == {"checks": 1}
    assert run.finished_at is not None and channel.sent == [] and gh.issues == {}


async def test_forced_failure_alerts_once_and_opens_one_issue(env):
    deps, channel, gh = env
    for _ in range(4):
        _, status = await run_job(deps, "selftest_fail")
        assert status == "failed"

    async with deps.sessionmaker() as s:
        runs = (await s.execute(select(Run).order_by(Run.id))).scalars().all()
        stored = await s.get(Setting, "agent_issue:selftest_fail")
    assert [r.status for r in runs] == ["failed"] * 4
    assert "RuntimeError: forced failure" in runs[0].error

    # Owners: one alert each for the streak, not one per failure.
    for phone in (OWNER_1, OWNER_2):
        alerts = [m["body"] for m in channel.to(phone)]
        assert len(alerts) == 1 and alerts[0].startswith("⚠️ Self-test (forced failure) didn't work")

    # GitHub: one issue at failure 3, a comment at failure 4.
    assert list(gh.issues) == [1] and stored.value == {"number": 1}
    issue = gh.issues[1]
    assert issue["title"] == "[agent] selftest_fail failing" and issue["labels"] == ["bug"]
    assert "**Failures in a row:** 3" in issue["body"]
    assert len(gh.comments[1]) == 1 and "**Failures in a row:** 4" in gh.comments[1][0]


async def test_recovery_notifies_owners_and_issue(env):
    deps, channel, gh = env
    for _ in range(3):
        await run_job(deps, "selftest_fail")
    # Same job name succeeding again: reuse the failing job's name via JOBS swap.
    from app import jobs

    original = jobs.JOBS["selftest_fail"]
    jobs.JOBS["selftest_fail"] = jobs.selftest_ok
    try:
        _, status = await run_job(deps, "selftest_fail")
    finally:
        jobs.JOBS["selftest_fail"] = original
    assert status == "succeeded"
    assert any(
        m["body"].startswith("✅ Self-test (forced failure) is working again") for m in channel.to(OWNER_1)
    )
    assert any("succeeded again" in c for c in gh.comments[1])


async def test_closed_issue_leads_to_a_new_one(env):
    deps, _, gh = env
    for _ in range(3):
        await run_job(deps, "selftest_fail")
    gh.issues[1]["state"] = "closed"
    await run_job(deps, "selftest_fail")
    assert list(gh.issues) == [1, 2]


async def test_reporter_off_without_token(env, caplog):
    deps, _, _ = env
    deps.issues = GitHubIssueReporter("", "owner/repo", deps.sessionmaker)
    for _ in range(3):
        await run_job(deps, "selftest_fail")
    assert "GitHub reporter off" in caplog.text


def test_redact_removes_emails_and_phones():
    text = "send to marie@studio.ca failed; called +1 (514) 555-0101 and 5145550199; run 12 id 42"
    assert redact(text) == "send to [email] failed; called [phone] and [phone]; run 12 id 42"
