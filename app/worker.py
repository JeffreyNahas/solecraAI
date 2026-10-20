"""Processes stored inbound messages in the background.

Messages from the same owner are handled one at a time, in arrival order.
Different owners are handled in parallel. A message that raises is retried
up to `worker_max_attempts` times, then marked failed.
"""

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import InboundMessage
from app.handlers import Handler

log = logging.getLogger(__name__)


class Worker:
    def __init__(
        self, sessionmaker: async_sessionmaker, handler: Handler, poll_seconds: float, max_attempts: int
    ) -> None:
        self.sessionmaker = sessionmaker
        self.handler = handler
        self.poll_seconds = poll_seconds
        self.max_attempts = max_attempts
        self.retry_base_seconds = 1.0
        self.wakeup = asyncio.Event()
        self._lanes: dict[str, asyncio.Task] = {}
        self._main: asyncio.Task | None = None

    async def start(self) -> None:
        await self.recover()
        await self.handler.resume_unfinished()
        self._main = asyncio.create_task(self._loop(), name="inbound-worker")

    async def stop(self) -> None:
        tasks = [t for t in [self._main, *self._lanes.values()] if t]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def notify(self) -> None:
        self.wakeup.set()

    async def recover(self) -> None:
        """Messages left 'processing' by a crash go back to the queue."""
        async with self.sessionmaker() as session:
            await session.execute(
                update(InboundMessage).where(InboundMessage.status == "processing").values(status="pending")
            )
            await session.commit()

    async def _loop(self) -> None:
        while True:
            try:
                await self.dispatch()
            except Exception:
                log.exception("worker dispatch failed")
            try:
                await asyncio.wait_for(self.wakeup.wait(), timeout=self.poll_seconds)
            except TimeoutError:
                pass
            self.wakeup.clear()

    async def dispatch(self) -> None:
        """Start one lane per sender that has pending messages."""
        async with self.sessionmaker() as session:
            senders = (
                await session.execute(
                    select(InboundMessage.sender).where(InboundMessage.status == "pending").distinct()
                )
            ).scalars()
            for sender in senders:
                lane = self._lanes.get(sender)
                if lane is None or lane.done():
                    self._lanes[sender] = asyncio.create_task(self.drain(sender))

    async def drain(self, sender: str) -> None:
        while await self.process_next(sender):
            pass

    async def process_next(self, sender: str) -> bool:
        """Handle the oldest pending message from `sender`. False when none left."""
        async with self.sessionmaker() as session:
            msg = (
                await session.execute(
                    select(InboundMessage)
                    .where(InboundMessage.sender == sender, InboundMessage.status == "pending")
                    .order_by(InboundMessage.received_at, InboundMessage.message_id)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
            ).scalar_one_or_none()
            if msg is None:
                return False
            msg.status = "processing"
            msg.attempts += 1
            await session.commit()

        try:
            await self.handler.handle(msg)
        except Exception as exc:
            log.exception("handling message %s failed (attempt %s)", msg.message_id, msg.attempts)
            status = "failed" if msg.attempts >= self.max_attempts else "pending"
            await self._finish(msg.message_id, status, repr(exc)[:2000])
            if status == "pending":
                await asyncio.sleep(min(self.retry_base_seconds * 2**msg.attempts, 30))
            return True
        await self._finish(msg.message_id, "done", None)
        return True

    async def _finish(self, message_id: str, status: str, error: str | None) -> None:
        async with self.sessionmaker() as session:
            await session.execute(
                update(InboundMessage)
                .where(InboundMessage.message_id == message_id)
                .values(status=status, last_error=error, processed_at=datetime.now(UTC))
            )
            await session.commit()
