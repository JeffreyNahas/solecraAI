"""Turn a WhatsApp webhook body into a flat list of incoming messages.

Only the fields we use are modelled; everything else is kept in `raw`.
Delivery/read receipts ("statuses") are ignored for now.
"""

from typing import Any, Literal

from pydantic import BaseModel

from app.config import normalize_phone


class IncomingMessage(BaseModel):
    message_id: str
    sender: str  # digits only
    msg_type: Literal["text", "button", "other"]
    text: str | None = None
    button_id: str | None = None
    timestamp: int | None = None
    raw: dict[str, Any]


def parse_webhook(body: dict[str, Any]) -> list[IncomingMessage]:
    messages: list[IncomingMessage] = []
    if body.get("object") != "whatsapp_business_account":
        return messages
    for entry in body.get("entry") or []:
        for change in entry.get("changes") or []:
            if change.get("field") != "messages":
                continue
            value = change.get("value") or {}
            for msg in value.get("messages") or []:
                parsed = _parse_one(msg)
                if parsed:
                    messages.append(parsed)
    return messages


def _parse_one(msg: dict[str, Any]) -> IncomingMessage | None:
    message_id = msg.get("id")
    sender = normalize_phone(str(msg.get("from") or ""))
    if not message_id or not sender:
        return None
    msg_type = msg.get("type")
    ts = msg.get("timestamp")
    common = {
        "message_id": message_id,
        "sender": sender,
        "timestamp": int(ts) if ts and str(ts).isdigit() else None,
        "raw": msg,
    }
    if msg_type == "text":
        return IncomingMessage(msg_type="text", text=(msg.get("text") or {}).get("body", ""), **common)
    if msg_type == "interactive":
        interactive = msg.get("interactive") or {}
        reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
        if reply.get("id"):
            return IncomingMessage(
                msg_type="button", button_id=reply["id"], text=reply.get("title"), **common
            )
    if msg_type == "button":
        # Quick-reply button on a template message.
        button = msg.get("button") or {}
        return IncomingMessage(
            msg_type="button", button_id=button.get("payload"), text=button.get("text"), **common
        )
    return IncomingMessage(msg_type="other", **common)
