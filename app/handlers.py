"""What the bot does with each owner message (runs in the worker, not the webhook)."""

import logging
import uuid
from typing import Any

from langgraph.types import Command

from app import approvals
from app.db.audit import audit
from app.db.models import Approval, InboundMessage, Owner
from app.deps import Deps
from app.owners import get_owner
from app.whatsapp.buttons import parse_button_id

log = logging.getLogger(__name__)

HELP_TEXT = (
    "Commands:\n"
    "• test draft: start a demo approval\n"
    "• help: show this message\n"
    "Anything else is echoed back (Gate 1 test mode)."
)


class Handler:
    def __init__(self, deps: Deps, demo_graph: Any) -> None:
        self.deps = deps
        self.demo_graph = demo_graph

    async def handle(self, msg: InboundMessage) -> None:
        async with self.deps.sessionmaker() as session:
            owner = await get_owner(session, msg.sender)
        if owner is None:
            # Allowlist is also checked in the webhook; this guards queued messages
            # from an owner who was removed since.
            log.info("ignoring message %s from non-owner", msg.message_id)
            return

        if msg.msg_type == "button":
            log.info("button from %s: %s", owner.name, msg.button_id)
            await self._on_button(owner, msg)
        elif msg.msg_type == "text":
            log.info("text from %s: %s", owner.name, msg.text)
            await self._on_text(owner, msg)
        else:
            await self._reply(owner, msg, "I can only read text messages for now.")

    # --- buttons -----------------------------------------------------------

    async def _on_button(self, owner: Owner, msg: InboundMessage) -> None:
        button = parse_button_id(msg.button_id)
        if button is None:
            await self._reply(owner, msg, "Sorry, I didn't recognise that button.")
            return

        new_status = {"approve": "approved", "reject": "rejected", "edit": "editing"}[button.action]
        decision = None
        if button.action != "edit":
            decision = {"action": button.action, "by": owner.phone, "by_name": owner.name}

        async with self.deps.sessionmaker() as session:
            won = await approvals.transition(
                session,
                button.approval_id,
                expected_status="pending",
                version=button.version,
                new_status=new_status,
                owner_phone=owner.phone,
                decision=decision,
            )
            if won:
                audit(
                    session, owner.phone, f"approval_{new_status}", "approval", won.id, version=button.version
                )
            await session.commit()
            current = won or await session.get(Approval, button.approval_id)

        if current is None:
            await self._reply(owner, msg, "That request no longer exists.")
            return

        if won is None:
            await self._on_lost_tap(owner, msg, current)
            return

        if button.action == "edit":
            await self._reply(
                owner, msg, "✏️ Send me the full replacement text. I'll use it exactly as written."
            )
            return
        await self._resume(current)

    async def _on_lost_tap(self, owner: Owner, msg: InboundMessage, approval: Approval) -> None:
        # Retry of our own tap whose resume didn't finish (e.g. crash): finish it.
        if (
            approval.decided_by == owner.phone
            and approval.status in approvals.FINAL_STATUSES
            and approval.resumed_at is None
        ):
            await self._resume(approval)
            return
        if approval.status == "editing" and approval.decided_by == owner.phone:
            await self._reply(owner, msg, "✏️ Still waiting for your replacement text.")
            return
        if approval.decided_by and approval.decided_by != owner.phone:
            async with self.deps.sessionmaker() as session:
                other = await session.get(Owner, approval.decided_by)
            who = other.name if other else "the other owner"
            text = f"Already handled by {who} ({approval.status})."
        else:
            text = f"Already handled ({approval.status})."
        await self._reply(owner, msg, text)

    # --- text --------------------------------------------------------------

    async def _on_text(self, owner: Owner, msg: InboundMessage) -> None:
        text = (msg.text or "").strip()

        async with self.deps.sessionmaker() as session:
            editing = await approvals.editing_for(session, owner.phone)
        if editing is not None:
            await self._finish_edit(owner, editing, text)
            return

        command = text.lower()
        if command == "test draft":
            await self._start_demo(owner)
        elif command in ("help", "aide", "?"):
            await self._reply(owner, msg, HELP_TEXT)
        else:
            await self._reply(owner, msg, f"Echo: {text}")

    async def _finish_edit(self, owner: Owner, approval: Approval, text: str) -> None:
        decision = {"action": "edited", "by": owner.phone, "by_name": owner.name, "text": text}
        async with self.deps.sessionmaker() as session:
            done = await approvals.transition(
                session,
                approval.id,
                expected_status="editing",
                version=approval.version,
                new_status="edited",
                owner_phone=owner.phone,
                decision=decision,
            )
            if done:
                audit(session, owner.phone, "approval_edited", "approval", done.id)
            await session.commit()
        if done:
            await self._resume(done)

    async def _start_demo(self, owner: Owner) -> None:
        run_id = f"approval_demo:{uuid.uuid4()}"
        async with self.deps.sessionmaker() as session:
            audit(session, owner.phone, "demo_started", "run", run_id)
            await session.commit()
        log.info("starting approval_demo run %s", run_id)
        await self.demo_graph.ainvoke(
            {"run_id": run_id, "requested_by": owner.phone},
            {"configurable": {"thread_id": run_id}},
        )

    # --- helpers -----------------------------------------------------------

    async def _resume(self, approval: Approval) -> None:
        decision = (approval.payload or {}).get("decision")
        if not decision:
            log.error("approval %s has no stored decision", approval.id)
            return
        log.info("resuming run %s with %s", approval.run_id, decision["action"])
        await self.demo_graph.ainvoke(
            Command(resume=decision), {"configurable": {"thread_id": approval.run_id}}
        )
        async with self.deps.sessionmaker() as session:
            await approvals.mark_resumed(session, approval.id)
            await session.commit()

    async def resume_unfinished(self) -> None:
        """At startup: resume graphs whose decision was saved but never delivered."""
        async with self.deps.sessionmaker() as session:
            pending = await approvals.unresumed_decisions(session)
        for approval in pending:
            try:
                await self._resume(approval)
            except Exception:
                log.exception("could not resume approval %s", approval.id)

    async def _reply(self, owner: Owner, msg: InboundMessage, text: str) -> None:
        await self.deps.channel.send_text(owner.phone, text, key=f"reply:{msg.message_id}")
