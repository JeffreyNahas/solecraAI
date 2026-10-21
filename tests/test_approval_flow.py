"""Gate 1: "test draft" -> buttons to both owners -> restart -> Approve -> run completes."""

import json

from sqlalchemy import select

from app.db.models import Approval
from tests.helpers import OWNER_1, OWNER_2, button_payload, sign, text_payload, wait_idle


async def post(client, payload):
    body = json.dumps(payload).encode()
    resp = await client.post("/webhook", content=body, headers={"X-Hub-Signature-256": sign(body)})
    assert resp.status_code == 200


async def start_demo(app, client) -> Approval:
    await post(client, text_payload(OWNER_1, "Test draft", "wamid.START"))
    await wait_idle(app)
    async with app.state.sessionmaker() as session:
        return (await session.execute(select(Approval))).scalar_one()


async def get_approval(app, approval_id) -> Approval:
    async with app.state.sessionmaker() as session:
        return await session.get(Approval, approval_id)


def bodies(channel, phone):
    return [m["body"] for m in channel.to(phone)]


async def test_buttons_sent_to_both_owners(running_app, channel):
    async with running_app() as (app, client):
        approval = await start_demo(app, client)
    assert approval.status == "pending" and approval.version == 1
    for phone in (OWNER_1, OWNER_2):
        [msg] = channel.to(phone)
        assert msg["type"] == "buttons"
        assert [b[0] for b in msg["buttons"]] == [
            f"approve:{approval.id}:1",
            f"edit:{approval.id}:1",
            f"reject:{approval.id}:1",
        ]
        assert "Bonjour" in msg["body"]  # French first


async def test_gate1_approve_after_restart(running_app, channel):
    # First "process": start the demo, then shut everything down.
    async with running_app() as (app, client):
        approval = await start_demo(app, client)

    # Second "process": fresh app, pool and graph; only Postgres survives.
    async with running_app() as (app, client):
        await post(client, button_payload(OWNER_2, f"approve:{approval.id}:1", "wamid.TAP1"))
        await wait_idle(app)
        final = await get_approval(app, approval.id)

    assert final.status == "approved" and final.decided_by == OWNER_2 and final.resumed_at is not None
    for phone in (OWNER_1, OWNER_2):
        assert any(b.startswith("✅ Approved by Bob") for b in bodies(channel, phone))


async def test_second_owner_gets_already_handled(running_app, channel):
    async with running_app() as (app, client):
        approval = await start_demo(app, client)
        await post(client, button_payload(OWNER_1, f"approve:{approval.id}:1", "wamid.TAP_A"))
        await post(client, button_payload(OWNER_2, f"reject:{approval.id}:1", "wamid.TAP_B"))
        await wait_idle(app)
        final = await get_approval(app, approval.id)
    assert final.status == "approved" and final.decided_by == OWNER_1
    assert "Already handled by Alice (approved)." in bodies(channel, OWNER_2)
    assert not any(b.startswith("❌") for b in bodies(channel, OWNER_2))


async def test_same_tap_redelivered_is_harmless(running_app, channel):
    async with running_app() as (app, client):
        approval = await start_demo(app, client)
        tap = button_payload(OWNER_1, f"approve:{approval.id}:1", "wamid.TAP_SAME")
        await post(client, tap)
        await post(client, tap)
        await wait_idle(app)
    assert sum(b.startswith("✅") for b in bodies(channel, OWNER_1)) == 1


async def test_reject(running_app, channel):
    async with running_app() as (app, client):
        approval = await start_demo(app, client)
        await post(client, button_payload(OWNER_1, f"reject:{approval.id}:1", "wamid.REJ"))
        await wait_idle(app)
        final = await get_approval(app, approval.id)
    assert final.status == "rejected"
    assert any(b.startswith("❌ Rejected by Alice") for b in bodies(channel, OWNER_2))


async def test_edit_uses_replacement_text(running_app, channel):
    async with running_app() as (app, client):
        approval = await start_demo(app, client)
        await post(client, button_payload(OWNER_1, f"edit:{approval.id}:1", "wamid.EDIT"))
        await wait_idle(app)
        assert (await get_approval(app, approval.id)).status == "editing"
        assert any("replacement text" in b for b in bodies(channel, OWNER_1))

        # The other owner can no longer act on the old buttons.
        await post(client, button_payload(OWNER_2, f"approve:{approval.id}:1", "wamid.LATE"))
        # Owners are processed in parallel; let Owner 2's tap finish first so the order is fixed.
        await wait_idle(app)
        await post(client, text_payload(OWNER_1, "Bonjour, nouveau texte.", "wamid.NEWTEXT"))
        await wait_idle(app)
        final = await get_approval(app, approval.id)

    assert final.status == "edited" and final.payload["decision"]["text"] == "Bonjour, nouveau texte."
    assert "Already handled by Alice (editing)." in bodies(channel, OWNER_2)
    assert any("Final text:\n\nBonjour, nouveau texte." in b for b in bodies(channel, OWNER_2))


async def test_stale_version_rejected(running_app, channel):
    async with running_app() as (app, client):
        approval = await start_demo(app, client)
        await post(client, button_payload(OWNER_1, f"approve:{approval.id}:99", "wamid.STALE"))
        await wait_idle(app)
        final = await get_approval(app, approval.id)
    assert final.status == "pending"
    assert "Already handled (pending)." in bodies(channel, OWNER_1)


async def test_decision_saved_but_not_resumed_is_finished_on_startup(running_app, channel):
    """Crash between saving the decision and resuming the graph."""
    async with running_app() as (app, client):
        approval = await start_demo(app, client)
        async with app.state.sessionmaker() as session:
            row = await session.get(Approval, approval.id)
            row.status, row.version, row.decided_by = "approved", 2, OWNER_1
            row.payload = {
                **row.payload,
                "decision": {"action": "approve", "by": OWNER_1, "by_name": "Alice"},
            }
            await session.commit()

    async with running_app() as (app, _):
        final = await get_approval(app, approval.id)
    assert final.resumed_at is not None
    assert any(b.startswith("✅ Approved by Alice") for b in bodies(channel, OWNER_1))
