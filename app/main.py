"""FastAPI app: /health and the WhatsApp webhook.

Run with: uvicorn --factory app.main:create_app

The webhook only checks, stores and acknowledges. All work happens in the
background worker, so Meta always gets a fast 200.
"""

import json
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, PlainTextResponse
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from sqlalchemy import text, update
from sqlalchemy.dialects.postgresql import insert

from app import unsubscribe
from app.channel import Channel
from app.config import Settings, get_settings
from app.db.audit import audit
from app.db.models import InboundMessage, Owner
from app.db.session import make_engine, make_sessionmaker
from app.deps import Deps
from app.graphs.approval_demo import ITEM_TYPE as DEMO_ITEM_TYPE
from app.graphs.approval_demo import build_approval_demo
from app.graphs.outreach_email import ITEM_TYPE as OUTREACH_ITEM_TYPE
from app.graphs.outreach_email import build_outreach_graph
from app.handlers import GraphEntry, Handler
from app.llm import LLM
from app.outreach import present
from app.owners import sync_owners
from app.whatsapp.client import WhatsAppClient
from app.whatsapp.parse import parse_webhook
from app.whatsapp.signature import verify_signature
from app.worker import Worker

log = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, channel: Channel | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = make_engine(settings)
        sessionmaker = make_sessionmaker(engine)
        await sync_owners(sessionmaker, settings.owners())

        pool = AsyncConnectionPool(
            settings.database_url,
            kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
            open=False,
        )
        await pool.open()
        checkpointer = AsyncPostgresSaver(pool)
        await checkpointer.setup()

        wa = None
        if channel is None:
            wa = WhatsAppClient(settings, sessionmaker)
        llm = LLM(settings) if settings.openrouter_api_key else None  # AI edits of drafts
        deps = Deps(
            settings=settings,
            sessionmaker=sessionmaker,
            channel=channel or wa,
            llm=llm,
            checkpointer=checkpointer,
        )

        async def show_drafts(owner: Owner) -> None:
            if await present.present_next(deps, only_phone=owner.phone) is None:
                await deps.channel.send_text(
                    owner.phone, "No email drafts waiting.", key=f"drafts:none:{uuid4()}"
                )

        async def next_draft() -> None:
            await present.present_next(deps)

        handler = Handler(
            deps,
            {
                DEMO_ITEM_TYPE: GraphEntry(build_approval_demo(deps, checkpointer)),
                OUTREACH_ITEM_TYPE: GraphEntry(
                    build_outreach_graph(deps, checkpointer), ai_edits=True, after_decision=next_draft
                ),
            },
            commands={"drafts": show_drafts, present.SHOW_DRAFTS: show_drafts},
        )
        worker = Worker(sessionmaker, handler, settings.worker_poll_seconds, settings.worker_max_attempts)

        app.state.settings = settings
        app.state.deps = deps
        app.state.handler = handler
        app.state.sessionmaker = sessionmaker
        app.state.worker = worker
        app.state.owners = set(settings.owners())
        await worker.start()
        log.info("started; %d owner(s) allowlisted", len(app.state.owners))
        try:
            yield
        finally:
            await worker.stop()
            if wa:
                await wa.aclose()
            if llm:
                await llm.aclose()
            await pool.close()
            await engine.dispose()

    app = FastAPI(title="Solecra agents", lifespan=lifespan)

    @app.get("/health")
    async def health(request: Request) -> dict[str, str]:
        async with request.app.state.sessionmaker() as session:
            await session.execute(text("SELECT 1"))
        return {"status": "ok"}

    @app.get("/webhook", response_class=PlainTextResponse)
    async def verify(
        mode: str = Query("", alias="hub.mode"),
        token: str = Query("", alias="hub.verify_token"),
        challenge: str = Query("", alias="hub.challenge"),
    ) -> str:
        if mode == "subscribe" and settings.whatsapp_verify_token and token == settings.whatsapp_verify_token:
            return challenge
        raise HTTPException(status_code=403, detail="verification failed")

    @app.post("/webhook")
    async def receive(request: Request) -> Response:
        body = await request.body()
        if not verify_signature(
            settings.whatsapp_app_secret, body, request.headers.get("X-Hub-Signature-256")
        ):
            log.warning("webhook rejected: bad signature")
            raise HTTPException(status_code=401, detail="bad signature")

        if settings.webhook_capture_dir:
            _capture(settings.webhook_capture_dir, body)

        try:
            data: dict[str, Any] = json.loads(body)
        except json.JSONDecodeError:
            log.warning("webhook body is not JSON")
            return Response(status_code=200)

        _log_failed_deliveries(data)
        accepted = await _store(request.app, parse_webhook(data))
        if accepted:
            request.app.state.worker.notify()
        # Any DB error above raises -> 500 -> Meta retries later (dedupe makes that safe).
        return Response(status_code=200)

    @app.get("/u/{token}", response_class=HTMLResponse)
    async def unsubscribe_page(token: str) -> HTMLResponse:
        """Confirm page. Never unsubscribes: mail scanners and previews open every link."""
        return HTMLResponse(unsubscribe.confirm_page(settings, token), headers=unsubscribe.page_headers())

    @app.post("/u/{token}", response_class=HTMLResponse)
    async def unsubscribe_confirm(token: str, request: Request) -> HTMLResponse:
        """The Confirm button, and the one-click POST from mail providers (RFC 8058)."""
        state = request.app.state
        async with state.sessionmaker() as session:
            send = await unsubscribe.find_send(session, token)
            if send is None:
                return HTMLResponse(
                    unsubscribe.done_page(settings, known=False), headers=unsubscribe.page_headers()
                )
            result = await unsubscribe.apply_unsubscribe(session, send)
            send_key = send.idempotency_key
            await session.commit()
        for approval in result.closed_approvals:  # resume the closed drafts' graphs (they end)
            try:
                await state.handler.deliver_decision(approval)
            except Exception:
                log.exception("could not close draft run for approval %s", approval.id)
        await unsubscribe.tell_owners(state.deps, result, send_key)
        log.info("unsubscribed: domain=%s leads=%d", result.domain or "(free-mail)", result.leads)
        return HTMLResponse(unsubscribe.done_page(settings, known=True), headers=unsubscribe.page_headers())

    return app


async def _store(app: FastAPI, messages: list) -> int:
    owners: set[str] = app.state.owners
    accepted = 0
    async with app.state.sessionmaker() as session:
        for msg in messages:
            if msg.sender not in owners:
                log.info("ignored message from non-owner ...%s", msg.sender[-4:])
                audit(session, "system", "ignored_non_owner", "phone", f"...{msg.sender[-4:]}")
                continue
            result = await session.execute(
                insert(InboundMessage)
                .values(
                    message_id=msg.message_id,
                    sender=msg.sender,
                    msg_type=msg.msg_type,
                    text=msg.text,
                    button_id=msg.button_id,
                    payload=msg.raw,
                    status="pending",
                    attempts=0,
                )
                .on_conflict_do_nothing(index_elements=["message_id"])
                .returning(InboundMessage.message_id)
            )
            # RETURNING, not rowcount: rowcount is -1 for these inserts, so a duplicate would pass.
            if result.scalar_one_or_none() is None:
                log.info("duplicate message %s ignored", msg.message_id)
                continue
            accepted += 1
            await session.execute(
                update(Owner).where(Owner.phone == msg.sender).values(last_inbound_at=datetime.now(UTC))
            )
        await session.commit()
    return accepted


def _log_failed_deliveries(data: dict[str, Any]) -> None:
    """Meta reports send failures (e.g. 24h window closed, 131047) as status events."""
    for entry in data.get("entry") or []:
        for change in entry.get("changes") or []:
            for status in (change.get("value") or {}).get("statuses") or []:
                if status.get("status") == "failed":
                    log.warning(
                        "whatsapp delivery failed id=%s errors=%s", status.get("id"), status.get("errors")
                    )


def _capture(directory: str, body: bytes) -> None:
    path = Path(directory)
    path.mkdir(parents=True, exist_ok=True)
    (path / f"webhook_{time.time_ns()}.json").write_bytes(body)
