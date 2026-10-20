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
