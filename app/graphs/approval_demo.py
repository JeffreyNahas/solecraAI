"""Gate 1 demo: draft -> ask owners (buttons) -> wait (interrupt) -> report result.

The wait survives restarts because state is checkpointed in Postgres
(thread_id = run_id). Resume with:
    graph.ainvoke(Command(resume=decision), {"configurable": {"thread_id": run_id}})

Note: on resume LangGraph re-runs the interrupted node from the top, so the
`wait_for_owner` node does nothing except call interrupt(). Sends live in
other nodes and use idempotency keys.
"""

import logging
from typing import Any, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app import approvals
from app.channel import ChannelError
from app.db.audit import audit
from app.deps import Deps
from app.owners import active_owners
from app.whatsapp.buttons import approval_buttons

log = logging.getLogger(__name__)

ITEM_TYPE = "demo_draft"

DEMO_DRAFT = (
    "Objet : Stores et rideaux pour votre projet\n\n"
    "Bonjour,\n\n"
    "Nous sommes Solecra, une entreprise montréalaise spécialisée dans les stores et "
    "rideaux résidentiels et commerciaux. Nous offrons une évaluation gratuite sur place.\n\n"
    "Au plaisir,\nL'équipe Solecra"
)


class DemoState(TypedDict, total=False):
    run_id: str
    requested_by: str
    draft: str
    approval_id: int
    decision: dict[str, Any]
    outcome: str


def build_approval_demo(deps: Deps, checkpointer: BaseCheckpointSaver):
    async def make_draft(state: DemoState) -> DemoState:
        return {"draft": DEMO_DRAFT}

    async def request_approval(state: DemoState) -> DemoState:
        async with deps.sessionmaker() as session:
            approval = await approvals.get_or_create_pending(
                session, state["run_id"], ITEM_TYPE, None, {"draft": state["draft"]}
            )
            audit(session, "system", "approval_requested", "approval", approval.id, run_id=state["run_id"])
            owners = await active_owners(session)
            await session.commit()

        body = f"📝 Draft ready for approval (demo)\n\n{state['draft']}"
        for owner in owners:
            key = f"approval:{approval.id}:v{approval.version}:ask:{owner.phone}"
            try:
                await deps.channel.send_buttons(
                    owner.phone, body, approval_buttons(approval.id, approval.version), key
                )
            except ChannelError:
                # One owner being unreachable must not block the other.
                log.exception("could not send approval %s to %s", approval.id, owner.phone)
        return {"approval_id": approval.id}

    async def wait_for_owner(state: DemoState) -> DemoState:
        decision = interrupt({"approval_id": state["approval_id"]})
        return {"decision": decision}

    async def report(state: DemoState) -> DemoState:
        decision = state["decision"]
        action = decision.get("action")
        name = decision.get("by_name", "an owner")
        if action == "approve":
            outcome, text = "approved", f"✅ Approved by {name}. (Demo: nothing was sent.)"
        elif action == "edited":
            outcome = "edited"
            text = f"✏️ Edited by {name}. Final text:\n\n{decision.get('text', '')}"
        else:
            outcome, text = "rejected", f"❌ Rejected by {name}. Draft discarded."

        async with deps.sessionmaker() as session:
            owners = await active_owners(session)
            audit(session, decision.get("by", "unknown"), f"demo_{outcome}", "approval", state["approval_id"])
            await session.commit()
        for owner in owners:
            key = f"approval:{state['approval_id']}:result:{owner.phone}"
            try:
                await deps.channel.send_text(owner.phone, text, key)
            except ChannelError:
                log.exception("could not send result to %s", owner.phone)
        log.info("approval_demo run %s finished: %s", state["run_id"], outcome)
        return {"outcome": outcome}

    graph = StateGraph(DemoState)
    graph.add_node("make_draft", make_draft)
    graph.add_node("request_approval", request_approval)
    graph.add_node("wait_for_owner", wait_for_owner)
    graph.add_node("report", report)
    graph.add_edge(START, "make_draft")
    graph.add_edge("make_draft", "request_approval")
    graph.add_edge("request_approval", "wait_for_owner")
    graph.add_edge("wait_for_owner", "report")
    graph.add_edge("report", END)
    return graph.compile(checkpointer=checkpointer)
