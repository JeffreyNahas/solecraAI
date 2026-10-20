"""WhatsApp Cloud API sender (Graph API /messages).

Every send is recorded in `outbound_messages` under an idempotency key; if a
key was already sent, we return the stored message id without calling Meta.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.channel import ChannelError
from app.config import Settings
from app.db.models import OutboundMessage

log = logging.getLogger(__name__)

MAX_BODY = 1024  # interactive body limit; text limit is 4096
RETRY_STATUSES = {429, 500, 502, 503, 504}


class WhatsAppClient:
    def __init__(
        self,
        settings: Settings,
        sessionmaker: async_sessionmaker,
        http: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        max_attempts: int = 3,
    ) -> None:
        self.settings = settings
        self.sessionmaker = sessionmaker
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(10.0))
        self.sleep = sleep
        self.max_attempts = max_attempts

    @property
    def url(self) -> str:
        s = self.settings
        return f"https://graph.facebook.com/{s.whatsapp_api_version}/{s.whatsapp_phone_number_id}/messages"

    async def aclose(self) -> None:
        await self.http.aclose()

    async def send_text(self, to: str, body: str, key: str) -> str | None:
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "text",
            "text": {"preview_url": False, "body": body[:4096]},
        }
        return await self._send(to, payload, key)

    async def send_buttons(self, to: str, body: str, buttons: list[tuple[str, str]], key: str) -> str | None:
        if not 1 <= len(buttons) <= 3:
            raise ValueError("WhatsApp reply buttons: 1 to 3 allowed")
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": body[:MAX_BODY]},
                "action": {
                    "buttons": [
                        {"type": "reply", "reply": {"id": bid, "title": title[:20]}} for bid, title in buttons
                    ]
                },
            },
        }
        return await self._send(to, payload, key)

    async def _send(self, to: str, payload: dict[str, Any], key: str) -> str | None:
        async with self.sessionmaker() as session:
            await session.execute(
                insert(OutboundMessage)
                .values(idempotency_key=key, recipient=to, payload=payload, status="pending")
                .on_conflict_do_nothing(index_elements=["idempotency_key"])
            )
            await session.commit()
            row = (
                await session.execute(select(OutboundMessage).where(OutboundMessage.idempotency_key == key))
            ).scalar_one()
            if row.status == "sent":
                log.info("whatsapp send skipped, already sent key=%s", key)
                return row.wa_message_id

            try:
                wa_id = await self._post(payload)
            except ChannelError as exc:
                row.status = "failed"
                row.error = str(exc)[:2000]
                await session.commit()
                raise
            row.status = "sent"
            row.wa_message_id = wa_id
            row.error = None
            row.sent_at = datetime.now(UTC)
            await session.commit()
            log.info("whatsapp sent to=%s key=%s id=%s", to, key, wa_id)
            return wa_id

    async def _post(self, payload: dict[str, Any]) -> str | None:
        if self.settings.whatsapp_dry_run:
            log.info("DRY RUN whatsapp payload=%s", payload)
            return "dry-run"
        headers = {"Authorization": f"Bearer {self.settings.whatsapp_access_token}"}
        last_error = ""
        for attempt in range(1, self.max_attempts + 1):
            try:
                resp = await self.http.post(self.url, json=payload, headers=headers)
            except httpx.TransportError as exc:
                last_error = f"network error: {exc!r}"
            else:
                if resp.status_code < 300:
                    data = resp.json()
                    return (data.get("messages") or [{}])[0].get("id")
                last_error = f"HTTP {resp.status_code}: {resp.text[:500]}"
                if resp.status_code not in RETRY_STATUSES:
                    break
            if attempt < self.max_attempts:
                delay = 2 ** (attempt - 1)
                log.warning("whatsapp send failed (%s), retry in %ss", last_error, delay)
                await self.sleep(delay)
        log.error("whatsapp send failed: %s", last_error)
        raise ChannelError(last_error)
