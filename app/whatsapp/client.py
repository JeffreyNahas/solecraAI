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

MAX_BODY = 1024  # interactive (buttons) body limit
MAX_TEXT = 4096  # text message limit
MAX_BUTTON_TITLE = 20
RETRY_STATUSES = {429, 500, 502, 503, 504}


def split_text(body: str, limit: int = MAX_TEXT) -> list[str]:
    """Split a long message into parts of at most `limit` characters, at a paragraph or line
    break when possible. Nothing is ever dropped: owners must see every word they approve."""
    parts: list[str] = []
    rest = body
    while len(rest) > limit:
        cut = rest.rfind("\n\n", 0, limit)
        if cut <= 0:
            cut = rest.rfind("\n", 0, limit)
        if cut <= 0:
            cut = rest.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip("\n ")
    if rest or not parts:  # never an empty trailing message (Meta rejects it)
        parts.append(rest)
    return parts


def check_buttons(body: str, buttons: list[tuple[str, str]]) -> None:
    if not 1 <= len(buttons) <= 3:
        raise ValueError("WhatsApp reply buttons: 1 to 3 allowed")
    if len(body) > MAX_BODY:
        raise ValueError(f"buttons message body is {len(body)} characters (max {MAX_BODY})")
    for _, title in buttons:
        if len(title) > MAX_BUTTON_TITLE:
            raise ValueError(f"button title {title!r} is over {MAX_BUTTON_TITLE} characters")


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
        """Send a text; a body over 4,096 characters goes out as several messages (keys
        `key`, `key:part2`, ...). Returns the id of the last part."""
        wa_id = None
        for i, part in enumerate(split_text(body), start=1):
            payload = {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to,
                "type": "text",
                "text": {"preview_url": False, "body": part},
            }
            wa_id = await self._send(to, payload, key if i == 1 else f"{key}:part{i}")
        return wa_id

    async def send_buttons(self, to: str, body: str, buttons: list[tuple[str, str]], key: str) -> str | None:
        """Refuses (ValueError) a body or button title over WhatsApp's limits instead of cutting it:
        an owner must never approve text they couldn't see. Callers send long text first."""
        check_buttons(body, buttons)
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": body},
                "action": {
                    "buttons": [
                        {"type": "reply", "reply": {"id": bid, "title": title}} for bid, title in buttons
                    ]
                },
            },
        }
        return await self._send(to, payload, key)

    async def send_template(
        self,
        to: str,
        name: str,
        language: str,
        params: list[str],
        key: str,
        button_payloads: list[str] | None = None,
    ) -> str | None:
        """Send an approved template (the only kind allowed outside the 24 h window).
        `params` fill {{1}}, {{2}}... in the body; `button_payloads` set the quick-reply payloads,
        which come back as the button id when tapped."""
        components: list[dict[str, Any]] = []
        if params:
            components.append(
                {"type": "body", "parameters": [{"type": "text", "text": str(p)} for p in params]}
            )
        for index, button_payload in enumerate(button_payloads or []):
            components.append(
                {
                    "type": "button",
                    "sub_type": "quick_reply",
                    "index": str(index),
                    "parameters": [{"type": "payload", "payload": button_payload}],
                }
            )
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "template",
            "template": {"name": name, "language": {"code": language}, "components": components},
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
