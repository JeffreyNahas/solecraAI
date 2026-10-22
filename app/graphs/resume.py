"""Deliver an owner's (or the system's) decision to the graph run waiting on an approval.

Shared by the WhatsApp handler (owner taps) and jobs (e.g. draft expiry). What happens depends on
the run's saved state:
- paused on *this* approval → resume with the decision;
- stopped between steps (crashed after the decision was delivered, before the next pause) →
  continue without a decision, so no old decision can reach a later pause;
- finished, missing, or paused on a newer approval (edit loop) → only mark it delivered:
  resuming would restart the graph from the beginning or hand this old decision to another
  approval.
Callers in the API hold the run's lock (`Handler._run_lock`) around this.
"""

import logging
from typing import Any

from langgraph.types import Command
from sqlalchemy.ext.asyncio import async_sessionmaker

from app import approvals
from app.db.models import Approval

log = logging.getLogger(__name__)


def waiting_on(state: Any, approval_id: int) -> bool:
    """True if the saved graph state is paused in an interrupt() for this approval."""
    for interrupt in getattr(state, "interrupts", ()) or ():
        value = getattr(interrupt, "value", None)
        if isinstance(value, dict) and value.get("approval_id") == approval_id:
            return True
    return False


async def deliver(graph: Any, sessionmaker: async_sessionmaker, approval: Approval) -> None:
    decision = (approval.payload or {}).get("decision")
    if not decision:
        log.error("approval %s has no stored decision", approval.id)
        return
    config = {"configurable": {"thread_id": approval.run_id}}
    state = await graph.aget_state(config)
    if waiting_on(state, approval.id):
        log.info("resuming run %s with %s", approval.run_id, decision["action"])
        await graph.ainvoke(Command(resume=decision), config)
    elif state.next and not state.interrupts:
        log.info("continuing interrupted run %s", approval.run_id)
        await graph.ainvoke(None, config)
    else:
        log.warning(
            "run %s is not waiting on approval %s; decision not delivered again", approval.run_id, approval.id
        )
    async with sessionmaker() as session:
        await approvals.mark_resumed(session, approval.id)
        await session.commit()
