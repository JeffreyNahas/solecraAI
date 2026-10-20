"""Send a signed fake WhatsApp webhook to a running server (no Meta needed).

    uv run python -m scripts.fake_webhook text 15145550001 "test draft"
    uv run python -m scripts.fake_webhook button 15145550001 approve:1:1

Reads WHATSAPP_APP_SECRET from .env. Target URL: --url (default localhost:8000).
"""

import argparse
import hashlib
import hmac
import json
import time
import uuid

import httpx

from app.config import Settings


def build(kind: str, sender: str, value: str) -> dict:
    msg: dict = {"from": sender, "id": f"wamid.fake.{uuid.uuid4().hex}", "timestamp": str(int(time.time()))}
    if kind == "text":
        msg.update(type="text", text={"body": value})
    else:
        msg.update(
            type="interactive",
            interactive={"type": "button_reply", "button_reply": {"id": value, "title": value}},
        )
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "0",
                "changes": [
                    {"field": "messages", "value": {"messaging_product": "whatsapp", "messages": [msg]}}
                ],
            }
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=["text", "button"])
    parser.add_argument("sender", help="phone, digits with country code")
    parser.add_argument("value", help="message text or button id")
    parser.add_argument("--url", default="http://localhost:8000/webhook")
    args = parser.parse_args()

    body = json.dumps(build(args.kind, args.sender, args.value)).encode()
    secret = Settings().whatsapp_app_secret.encode()
    signature = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    resp = httpx.post(args.url, content=body, headers={"X-Hub-Signature-256": signature})
    print(resp.status_code, resp.text)


if __name__ == "__main__":
    main()
