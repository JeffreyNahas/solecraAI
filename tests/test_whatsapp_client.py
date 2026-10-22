import json

import httpx
import pytest
from sqlalchemy import select

from app.channel import ChannelError
from app.db.models import OutboundMessage
from app.db.session import make_engine, make_sessionmaker
from app.whatsapp.client import WhatsAppClient


@pytest.fixture
async def make_client(settings):
    settings = settings.model_copy(
        update={
            "whatsapp_access_token": "tok",
            "whatsapp_phone_number_id": "123",
            "whatsapp_api_version": "v23.0",
        }
    )
    engine = make_engine(settings)
    sm = make_sessionmaker(engine)

    async def no_sleep(_):
        return None

    def factory(handler):
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return WhatsAppClient(settings, sm, http=http, sleep=no_sleep), sm

    yield factory
    await engine.dispose()


async def test_send_buttons_payload_and_idempotency(make_client):
    requests = []

    def handler(request: httpx.Request):
        requests.append(request)
        return httpx.Response(200, json={"messages": [{"id": "wamid.OUT1"}]})

    client, sm = make_client(handler)
    buttons = [("approve:1:1", "Approve"), ("reject:1:1", "Reject")]
    assert await client.send_buttons("15145550001", "Draft", buttons, key="k1") == "wamid.OUT1"
    assert await client.send_buttons("15145550001", "Draft", buttons, key="k1") == "wamid.OUT1"

    assert len(requests) == 1
    req = requests[0]
    assert str(req.url) == "https://graph.facebook.com/v23.0/123/messages"
    assert req.headers["Authorization"] == "Bearer tok"
    body = json.loads(req.content)
    assert body["interactive"]["action"]["buttons"][0] == {
        "type": "reply",
        "reply": {"id": "approve:1:1", "title": "Approve"},
    }
    async with sm() as session:
        row = (await session.execute(select(OutboundMessage))).scalar_one()
    assert row.status == "sent" and row.wa_message_id == "wamid.OUT1"


async def test_retries_server_errors(make_client):
    responses = iter([httpx.Response(503), httpx.Response(200, json={"messages": [{"id": "wamid.OK"}]})])
    client, _ = make_client(lambda request: next(responses))
    assert await client.send_text("15145550001", "hi", key="k2") == "wamid.OK"


async def test_client_error_not_retried_and_marked_failed(make_client):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(400, json={"error": {"message": "bad"}})

    client, sm = make_client(handler)
    with pytest.raises(ChannelError):
        await client.send_text("15145550001", "hi", key="k3")
    assert len(calls) == 1
    async with sm() as session:
        row = await session.get(OutboundMessage, "k3")
    assert row.status == "failed" and "HTTP 400" in row.error


async def test_long_text_is_split_never_cut(make_client):
    """An owner must see every word of a draft they approve (review of #10's plan)."""
    sent = []

    def handler(request: httpx.Request):
        sent.append(json.loads(request.content)["text"]["body"])
        return httpx.Response(200, json={"messages": [{"id": f"wamid.{len(sent)}"}]})

    client, sm = make_client(handler)
    paragraphs = [f"Paragraphe {i} " + "mot " * 200 for i in range(8)]  # ~8 × 1,015 chars
    body = "\n\n".join(paragraphs)
    await client.send_text("15145550001", body, key="long")
    await client.send_text("15145550001", body, key="long")  # retry: nothing sent twice

    assert len(sent) == 2 and all(len(part) <= 4096 for part in sent)
    assert " ".join(" ".join(sent).split()) == " ".join(body.split())  # every word delivered
    assert sent[1].startswith("Paragraphe")  # split at a paragraph break
    async with sm() as session:
        keys = sorted((await session.execute(select(OutboundMessage.idempotency_key))).scalars())
    assert keys == ["long", "long:part2"]


def test_split_text_without_breaks_keeps_everything():
    from app.whatsapp.client import split_text

    text = "x" * 9000
    parts = split_text(text)
    assert [len(p) for p in parts] == [4096, 4096, 808] and "".join(parts) == text
    # A whitespace-only tail never becomes an empty message (Meta rejects those).
    assert split_text("x" * 4096 + "\n\n") == ["x" * 4096]
    assert split_text("") == [""]


@pytest.mark.parametrize(
    ("body", "buttons", "error"),
    [
        ("x" * 1025, [("a:1:1", "Approve")], "1025 characters"),
        ("ok", [("a:1:1", "A title that is far too long")], "over 20"),
        ("ok", [], "1 to 3"),
    ],
)
async def test_buttons_over_limits_are_refused_not_cut(make_client, body, buttons, error):
    def handler(request):
        raise AssertionError("must not reach Meta")

    client, _ = make_client(handler)
    with pytest.raises(ValueError, match=error):
        await client.send_buttons("15145550001", body, buttons, key="k")


async def test_send_template_payload(make_client):
    requests = []

    def handler(request: httpx.Request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"messages": [{"id": "wamid.T1"}]})

    client, _ = make_client(handler)
    wa_id = await client.send_template(
        "15145550001", "drafts_ready", "en_US", ["5"], key="t1", button_payloads=["show_drafts"]
    )
    assert wa_id == "wamid.T1"
    template = requests[0]["template"]
    assert requests[0]["type"] == "template"
    assert template["name"] == "drafts_ready" and template["language"] == {"code": "en_US"}
    assert template["components"] == [
        {"type": "body", "parameters": [{"type": "text", "text": "5"}]},
        {
            "type": "button",
            "sub_type": "quick_reply",
            "index": "0",
            "parameters": [{"type": "payload", "payload": "show_drafts"}],
        },
    ]
