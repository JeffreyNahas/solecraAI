"""What the bot does with each owner message (runs in the worker, not the webhook).

Approval buttons and edit texts are routed to the graph that owns the approval, by its
`item_type` (`Handler.graphs`). A graph is only resumed when it is actually waiting on that
approval: see `_resume`.
"""

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from langgraph.types import Command

from app import approvals
from app.db.audit import audit
from app.db.models import Approval, InboundMessage, Owner
from app.deps import Deps
from app.graphs import approval_demo
from app.owners import get_owner
from app.whatsapp.buttons import EDIT_MODES, edit_mode_buttons, parse_button_id

log = logging.getLogger(__name__)

HELP_TEXT = (
    "Commands:\n"
    "• test draft: start a demo approval\n"
    "• help: show this message\n"
    "Anything else is echoed back (Gate 1 test mode)."
)

EDIT_PROMPTS = {
    "text": "✍️ Send me the full replacement text. I'll use it exactly as written.",
    "ai": '🤖 Tell me what to change (e.g. "shorter", "mention offices"). I\'ll revise and ask you again.',
}


@dataclass(frozen=True)
class GraphEntry:
    """A compiled graph that owns approvals of one `item_type`.

    ai_edits: after Edit, offer "My own text" / "Tell AI changes" instead of asking for text."""

    graph: Any
    ai_edits: bool = False


class Handler:
    def __init__(self, deps: Deps, graphs: dict[str, GraphEntry]) -> None:
        self.deps = deps
        self.graphs = graphs

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
        if button.action in EDIT_MODES:
            await self._on_edit_mode(
                owner, msg, button.approval_id, button.version, EDIT_MODES[button.action]
            )
            return

        new_status = {"approve": "approved", "reject": "rejected", "edit": "editing"}[button.action]
        decision = None
        if button.action != "edit":
            decision = {
                "action": button.action,
                "by": owner.phone,
                "by_name": owner.name,
                "approval_id": button.approval_id,
            }

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
            entry = self.graphs.get(won.item_type)
            if entry is not None and entry.ai_edits:
                await self.deps.channel.send_buttons(
                    owner.phone,
                    "✏️ How do you want to edit it?",
                    edit_mode_buttons(won.id, won.version),
                    key=f"reply:{msg.message_id}",
                )
            else:
                await self._reply(owner, msg, EDIT_PROMPTS["text"])
            return
        await self._resume(current)

    async def _on_edit_mode(
        self, owner: Owner, msg: InboundMessage, approval_id: int, version: int, mode: str
    ) -> None:
        async with self.deps.sessionmaker() as session:
            chosen = await approvals.set_edit_mode(
                session, approval_id, owner_phone=owner.phone, version=version, mode=mode
            )
            await session.commit()
            current = chosen or await session.get(Approval, approval_id)
        if chosen is not None:
            await self._reply(owner, msg, EDIT_PROMPTS[mode])
        elif current is None:
            await self._reply(owner, msg, "That request no longer exists.")
        else:
            await self._on_lost_tap(owner, msg, current)

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
            await self._reply(owner, msg, "✏️ Still waiting for your edit.")
            return
        if approval.decided_by and approval.decided_by != owner.phone:
            async with self.deps.sessionmaker() as session:
                other = await session.get(Owner, approval.decided_by)
            who = other.name if other else "the other owner"
            if approval.status == "editing":
                text = f"{who} is editing this draft."
            else:
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
            await self._finish_edit(owner, msg, editing, text)
            return

        command = text.lower()
        if command == "test draft":
            await self._start_demo(owner)
        elif command in ("help", "aide", "?"):
            await self._reply(owner, msg, HELP_TEXT)
        else:
            await self._reply(owner, msg, f"Echo: {text}")

    async def _finish_edit(self, owner: Owner, msg: InboundMessage, approval: Approval, text: str) -> None:
        entry = self.graphs.get(approval.item_type)
        mode = (approval.payload or {}).get("edit_mode")
        if mode is None and entry is not None and entry.ai_edits:
            # The owner typed before choosing how to edit: ask again rather than guess.
            await self.deps.channel.send_buttons(
                owner.phone,
                "✏️ First choose how to edit it:",
                edit_mode_buttons(approval.id, approval.version),
                key=f"reply:{msg.message_id}",
            )
            return
        decision = {
            "action": "edited",
            "by": owner.phone,
            "by_name": owner.name,
            "approval_id": approval.id,
            "mode": mode or "text",
            "text": text,
        }
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
                audit(session, owner.phone, "approval_edited", "approval", done.id, mode=decision["mode"])
            await session.commit()
        if done:
            await self._resume(done)

    async def _start_demo(self, owner: Owner) -> None:
        run_id = f"approval_demo:{uuid.uuid4()}"
        async with self.deps.sessionmaker() as session:
            audit(session, owner.phone, "demo_started", "run", run_id)
            await session.commit()
        log.info("starting approval_demo run %s", run_id)
        await self.graphs[approval_demo.ITEM_TYPE].graph.ainvoke(
            {"run_id": run_id, "requested_by": owner.phone},
            {"configurable": {"thread_id": run_id}},
        )

    # --- helpers -----------------------------------------------------------

    async def _resume(self, approval: Approval) -> None:
        """Deliver an owner's decision to the graph waiting on this approval.

        Only when the graph's saved state is paused on *this* approval. Otherwise (thread
        finished, missing, or now waiting on a newer approval after an edit loop) resuming would
        restart the graph from the beginning or hand this old decision to another approval, so
        the decision is just marked delivered."""
        decision = (approval.payload or {}).get("decision")
        if not decision:
            log.error("approval %s has no stored decision", approval.id)
            return
        entry = self.graphs.get(approval.item_type)
        if entry is None:
            log.error("no graph for approval %s (item_type %s)", approval.id, approval.item_type)
            return
        config = {"configurable": {"thread_id": approval.run_id}}
        state = await entry.graph.aget_state(config)
        if _waiting_on(state, approval.id):
            log.info("resuming run %s with %s", approval.run_id, decision["action"])
            await entry.graph.ainvoke(Command(resume=decision), config)
        else:
            log.warning(
                "run %s is not waiting on approval %s; decision not delivered again",
                approval.run_id,
                approval.id,
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


def _waiting_on(state: Any, approval_id: int) -> bool:
    """True if the saved graph state is paused in an interrupt() for this approval."""
    for interrupt in getattr(state, "interrupts", ()) or ():
        value = getattr(interrupt, "value", None)
        if isinstance(value, dict) and value.get("approval_id") == approval_id:
            return True
    return False
