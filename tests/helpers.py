import asyncio
import copy
import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from app.db.models import InboundMessage

FIXTURES = Path(__file__).parent / "fixtures"
APP_SECRET = "test-app-secret"
OWNER_1 = "15145550001"
OWNER_2 = "15145550002"
STRANGER = "15145559999"


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


def sign(body: bytes, secret: str = APP_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def text_payload(sender: str, text: str, message_id: str) -> dict[str, Any]:
    data = copy.deepcopy(load_fixture("text_message.json"))
    msg = data["entry"][0]["changes"][0]["value"]["messages"][0]
    msg.update({"from": sender, "id": message_id, "text": {"body": text}})
    return data


def button_payload(sender: str, button_id: str, message_id: str) -> dict[str, Any]:
    data = copy.deepcopy(load_fixture("button_reply.json"))
    msg = data["entry"][0]["changes"][0]["value"]["messages"][0]
    msg.update({"from": sender, "id": message_id})
    msg["interactive"]["button_reply"]["id"] = button_id
    return data


class FakeChannel:
    """Records sends; honours idempotency keys like the real client."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.keys: set[str] = set()

    async def send_text(self, to: str, body: str, key: str) -> str | None:
        return self._record({"to": to, "body": body, "key": key, "type": "text"})

    async def send_buttons(self, to: str, body: str, buttons: list[tuple[str, str]], key: str) -> str | None:
        return self._record({"to": to, "body": body, "key": key, "type": "buttons", "buttons": buttons})

    def _record(self, item: dict[str, Any]) -> str:
        if item["key"] not in self.keys:
            self.keys.add(item["key"])
            self.sent.append(item)
        return f"wamid.fake{len(self.sent)}"

    def to(self, phone: str) -> list[dict[str, Any]]:
        return [m for m in self.sent if m["to"] == phone]


async def wait_idle(app, limit_seconds: float = 10.0) -> None:
    """Wait until the worker has processed every stored message."""
    sessionmaker = app.state.sessionmaker
    deadline = asyncio.get_running_loop().time() + limit_seconds
    while True:
        async with sessionmaker() as session:
            open_count = (
                await session.execute(
                    select(func.count())
                    .select_from(InboundMessage)
                    .where(InboundMessage.status.in_(("pending", "processing")))
                )
            ).scalar_one()
        lanes_busy = any(not t.done() for t in app.state.worker._lanes.values())
        if open_count == 0 and not lanes_busy:
            return
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError("worker did not go idle")
        await asyncio.sleep(0.05)
