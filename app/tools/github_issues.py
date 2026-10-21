"""Opens or updates one GitHub issue per failing job (label `bug`, title `[agent] <job> failing`).

Only technical details go to GitHub: job name, run id, time, streak and a redacted error.
The issue number for each job is remembered in the `settings` table (`agent_issue:<job>`),
so repeated failures comment on the same issue instead of opening new ones.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import Setting
from app.redact import redact

log = logging.getLogger(__name__)

API = "https://api.github.com"
RETRY_STATUSES = {429, 500, 502, 503, 504}


class GitHubIssueReporter:
    def __init__(
        self,
        token: str,
        repo: str,
        sessionmaker: async_sessionmaker,
        http: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.token = token
        self.repo = repo
        self.sessionmaker = sessionmaker
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(10.0))
        self.sleep = sleep

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.repo)

    async def aclose(self) -> None:
        await self.http.aclose()

    async def report_failure(
        self, *, job: str, run_id: int, error: str, streak: int, when: str
    ) -> int | None:
        """Comment on the job's open issue, or open a new one. Returns the issue number."""
        body = (
            f"**Job:** `{job}`\n**Run:** {run_id}\n**Time:** {when}\n"
            f"**Failures in a row:** {streak}\n\n**Error:**\n```\n{redact(error)[:3000]}\n```\n\n"
            "Opened automatically by the run tracker (`app/runs.py`). Close it once fixed."
        )
        if not self.enabled:
            log.warning("GitHub reporter off; would report %s failure (run %s)", job, run_id)
            return None
        number = await self._open_issue_number(job)
        if number:
            await self._request("POST", f"/issues/{number}/comments", {"body": body})
            return number
        created = await self._request(
            "POST", "/issues", {"title": f"[agent] {job} failing", "body": body, "labels": ["bug"]}
        )
        number = created["number"]
        await self._remember(job, number)
        log.info("opened GitHub issue #%s for job %s", number, job)
        return number

    async def report_recovery(self, *, job: str, run_id: int, when: str) -> None:
        if not self.enabled:
            return
        number = await self._open_issue_number(job)
        if number:
            await self._request(
                "POST",
                f"/issues/{number}/comments",
                {
                    "body": f"✅ `{job}` succeeded again (run {run_id}, {when}). "
                    "Close this issue once the cause is fixed."
                },
            )

    async def _open_issue_number(self, job: str) -> int | None:
        async with self.sessionmaker() as session:
            row = await session.get(Setting, f"agent_issue:{job}")
        number = (row.value or {}).get("number") if row else None
        if not number:
            return None
        issue = await self._request("GET", f"/issues/{number}")
        return number if issue.get("state") == "open" else None

    async def _remember(self, job: str, number: int) -> None:
        async with self.sessionmaker() as session:
            value = {"number": number}
            await session.execute(
                insert(Setting)
                .values(key=f"agent_issue:{job}", value=value, updated_by="system")
                .on_conflict_do_update(index_elements=["key"], set_={"value": value})
            )
            await session.commit()

    async def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{API}/repos/{self.repo}{path}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        last = ""
        for attempt in range(1, 4):
            try:
                resp = await self.http.request(method, url, json=payload, headers=headers)
            except httpx.TransportError as exc:
                last = f"network error: {exc!r}"
            else:
                if resp.status_code < 300:
                    return resp.json()
                last = f"HTTP {resp.status_code}: {resp.text[:300]}"
                if resp.status_code not in RETRY_STATUSES:
                    break
            if attempt < 3:
                await self.sleep(2 ** (attempt - 1))
        raise RuntimeError(f"GitHub API {method} {path} failed: {last}")
