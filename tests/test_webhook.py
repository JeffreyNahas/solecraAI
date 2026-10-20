import json
import logging

from sqlalchemy import select

from app.db.models import AuditLog, InboundMessage, Owner
from tests.helpers import OWNER_1, OWNER_2, STRANGER, load_fixture, sign, text_payload, wait_idle


async def post(client, payload, signature=None):
    body = json.dumps(payload).encode()
    return await client.post(
        "/webhook",
        content=body,
        headers={"X-Hub-Signature-256": signature or sign(body), "Content-Type": "application/json"},
    )


async def test_health(running_app):
    async with running_app() as (_, client):
        resp = await client.get("/health")
    assert resp.status_code == 200 and resp.json() == {"status": "ok"}


async def test_verify(running_app):
    async with running_app() as (_, client):
        ok = await client.get(
            "/webhook",
            params={"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "123"},
        )
        bad = await client.get(
            "/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "123"}
        )
    assert ok.status_code == 200 and ok.text == "123"
    assert bad.status_code == 403


async def test_owners_synced_from_env(running_app):
    async with running_app() as (app, _):
        async with app.state.sessionmaker() as session:
            owners = {o.phone: o.name for o in (await session.execute(select(Owner))).scalars()}
    assert owners == {OWNER_1: "Alice", OWNER_2: "Bob"}


async def test_bad_signature_rejected(running_app):
    async with running_app() as (app, client):
        resp = await post(client, load_fixture("text_message.json"), signature="sha256=deadbeef")
        async with app.state.sessionmaker() as session:
            rows = (await session.execute(select(InboundMessage))).scalars().all()
    assert resp.status_code == 401 and rows == []


async def test_text_processed_exactly_once(running_app, channel, caplog):
    caplog.set_level(logging.INFO)
    payload = text_payload(OWNER_1, "bonjour", "wamid.DUP")
    async with running_app() as (app, client):
        for _ in range(3):  # Meta may redeliver the same message
            assert (await post(client, payload)).status_code == 200
        await wait_idle(app)
    assert [m["body"] for m in channel.to(OWNER_1)] == ["Echo: bonjour"]
    assert sum("text from Alice: bonjour" in r.getMessage() for r in caplog.records) == 1


async def test_non_owner_ignored(running_app, channel):
    async with running_app() as (app, client):
        resp = await post(client, text_payload(STRANGER, "hi", "wamid.STRANGER"))
        await wait_idle(app)
        async with app.state.sessionmaker() as session:
            stored = (await session.execute(select(InboundMessage))).scalars().all()
            audits = (await session.execute(select(AuditLog.action))).scalars().all()
    assert resp.status_code == 200
    assert stored == [] and channel.sent == []
    assert "ignored_non_owner" in audits


async def test_status_events_acknowledged(running_app):
    async with running_app() as (_, client):
        resp = await post(client, load_fixture("status_failed.json"))
    assert resp.status_code == 200


async def test_messages_from_one_owner_handled_in_order(running_app, channel):
    async with running_app() as (app, client):
        for i in range(5):
            await post(client, text_payload(OWNER_1, f"m{i}", f"wamid.ORDER{i}"))
        await wait_idle(app)
    assert [m["body"] for m in channel.to(OWNER_1)] == [f"Echo: m{i}" for i in range(5)]


async def test_failed_handler_is_retried(running_app, channel, monkeypatch):
    calls = {"n": 0}
    original = channel.send_text

    async def flaky(to, body, key):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("temporary outage")
        return await original(to, body, key)

    monkeypatch.setattr(channel, "send_text", flaky)
    async with running_app() as (app, client):
        app.state.worker.retry_base_seconds = 0
        await post(client, text_payload(OWNER_1, "retry me", "wamid.RETRY"))
        await wait_idle(app)
        async with app.state.sessionmaker() as session:
            msg = await session.get(InboundMessage, "wamid.RETRY")
    assert msg.status == "done" and msg.attempts == 2
    assert [m["body"] for m in channel.to(OWNER_1)] == ["Echo: retry me"]
