"""Approval resume safety and the two-step Edit (#46).

A decision must only be delivered to a graph that is paused on that same approval. Without the
check, LangGraph restarts a missing thread from START and an edit loop could hand an old decision
to a newer approval.
"""

import uuid
from typing import Any, TypedDict

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from sqlalchemy import func, select

from app import approvals
from app.db.models import Approval, InboundMessage
from app.db.session import make_engine, make_sessionmaker
from app.deps import Deps
from app.handlers import GraphEntry, Handler
from app.owners import sync_owners
from tests.helpers import OWNER_1, OWNER_2, FakeChannel

ITEM = "test_loop"


class LoopState(TypedDict, total=False):
    run_id: str
    approval_id: int
    decisions: list[dict[str, Any]]
    rounds: int


def build_loop_graph(deps: Deps, checkpointer, starts: list[str], crash: list[bool] | None = None):
    """ask → wait → (edited: ask again with a NEW approval) → done. Records every start.
    `crash=[True]` makes the next re-ask raise once (a container restart mid-step)."""

    async def ask(state: LoopState) -> LoopState:
        if not state.get("rounds"):
            starts.append(state["run_id"])
        elif crash and crash[0]:
            crash[0] = False
            raise RuntimeError("container restarted")
        async with deps.sessionmaker() as session:
            approval = await approvals.get_or_create_pending(session, state["run_id"], ITEM, None, {})
            await session.commit()
        return {"approval_id": approval.id, "rounds": state.get("rounds", 0) + 1}

    async def wait(state: LoopState) -> LoopState:
        decision = interrupt({"approval_id": state["approval_id"]})
        return {"decisions": [*state.get("decisions", []), decision]}

    def route(state: LoopState) -> str:
        return "ask" if state["decisions"][-1]["action"] == "edited" else END

    g = StateGraph(LoopState)
    g.add_node("ask", ask)
    g.add_node("wait", wait)
    g.add_edge(START, "ask")
    g.add_edge("ask", "wait")
    g.add_conditional_edges("wait", route, ["ask", END])
    return g.compile(checkpointer=checkpointer)


@pytest.fixture
async def env(settings):
    engine = make_engine(settings)
    sm = make_sessionmaker(engine)
    await sync_owners(sm, settings.owners())
    channel = FakeChannel()
    deps = Deps(settings, sm, channel)
    starts: list[str] = []
    crash = [False]
    graph = build_loop_graph(deps, InMemorySaver(), starts, crash)
    handler = Handler(deps, {ITEM: GraphEntry(graph, ai_edits=True)})
    handler.crash = crash  # test hook
    yield handler, graph, sm, channel, starts
    await engine.dispose()


async def start(graph) -> str:
    run_id = f"loop:{uuid.uuid4()}"
    await graph.ainvoke({"run_id": run_id}, {"configurable": {"thread_id": run_id}})
    return run_id


async def open_approval(sm, run_id) -> Approval:
    async with sm() as session:
        return (
            await session.execute(
                select(Approval).where(Approval.run_id == run_id, Approval.status == "pending")
            )
        ).scalar_one()


def msg(sender: str, *, button: str | None = None, text: str | None = None) -> InboundMessage:
    return InboundMessage(
        message_id=f"wamid.{uuid.uuid4().hex[:8]}",
        sender=sender,
        msg_type="button" if button else "text",
        button_id=button,
        text=text,
        payload={},
    )


async def waiting_on(graph, run_id) -> list[int]:
    state = await graph.aget_state({"configurable": {"thread_id": run_id}})
    return [i.value["approval_id"] for i in state.interrupts]


async def test_missing_thread_is_not_restarted(env):
    """A decided approval whose thread has no checkpoint: before the fix, resuming ran the
    graph from START (new approval, new owner messages)."""
    handler, graph, sm, channel, starts = env
    async with sm() as session:
        ghost = Approval(
            run_id="loop:ghost",
            item_type=ITEM,
            status="approved",
            version=2,
            decided_by=OWNER_1,
            payload={"decision": {"action": "approve", "approval_id": -1}},
        )
        session.add(ghost)
        await session.commit()

    await handler.resume_unfinished()

    async with sm() as session:
        assert await session.scalar(select(func.count()).select_from(Approval)) == 1  # nothing new
        assert (await session.get(Approval, ghost.id)).resumed_at is not None
    assert starts == []


async def test_finished_thread_is_not_resumed_again(env):
    handler, graph, sm, channel, starts = env
    run_id = await start(graph)
    approval = await open_approval(sm, run_id)
    await handler.handle(msg(OWNER_1, button=f"approve:{approval.id}:1"))
    assert await waiting_on(graph, run_id) == []  # finished

    async with sm() as session:  # crash before resumed_at was saved
        row = await session.get(Approval, approval.id)
        row.resumed_at = None
        await session.commit()
    await handler.resume_unfinished()

    state = await graph.aget_state({"configurable": {"thread_id": run_id}})
    assert len(state.values["decisions"]) == 1 and starts == [run_id]
    async with sm() as session:
        assert (await session.get(Approval, approval.id)).resumed_at is not None


async def test_old_decision_never_reaches_a_newer_approval(env):
    """Edit loop: approval A edited → graph now waits on B. A crash left A 'not resumed'.
    On restart, A's decision must not be delivered to B."""
    handler, graph, sm, channel, starts = env
    run_id = await start(graph)
    first = await open_approval(sm, run_id)
    await handler.handle(msg(OWNER_1, button=f"edit:{first.id}:1"))
    await handler.handle(msg(OWNER_1, button=f"edit_text:{first.id}:2"))
    await handler.handle(msg(OWNER_1, text="Nouveau texte"))
    second = await open_approval(sm, run_id)
    assert second.id != first.id and await waiting_on(graph, run_id) == [second.id]

    async with sm() as session:
        row = await session.get(Approval, first.id)
        row.resumed_at = None
        await session.commit()
    await handler.resume_unfinished()

    assert await waiting_on(graph, run_id) == [second.id]  # still waiting for the owners
    state = await graph.aget_state({"configurable": {"thread_id": run_id}})
    assert len(state.values["decisions"]) == 1  # A's decision was not delivered a second time
    async with sm() as session:
        assert (await session.get(Approval, second.id)).status == "pending"


async def test_two_step_edit_with_ai_changes(env):
    handler, graph, sm, channel, starts = env
    run_id = await start(graph)
    approval = await open_approval(sm, run_id)

    await handler.handle(msg(OWNER_1, button=f"edit:{approval.id}:1"))
    [choice] = [m for m in channel.to(OWNER_1) if m["type"] == "buttons"]
    assert [b[0] for b in choice["buttons"]] == [f"edit_text:{approval.id}:2", f"edit_ai:{approval.id}:2"]

    # Typing before choosing: asked to choose, nothing decided.
    await handler.handle(msg(OWNER_1, text="plus court"))
    async with sm() as session:
        assert (await session.get(Approval, approval.id)).status == "editing"
    assert sum(m["type"] == "buttons" for m in channel.to(OWNER_1)) == 2

    # The other owner can't take over the edit.
    await handler.handle(msg(OWNER_2, button=f"edit_ai:{approval.id}:2"))
    assert channel.to(OWNER_2)[-1]["body"] == "Alice is editing this draft."

    await handler.handle(msg(OWNER_1, button=f"edit_ai:{approval.id}:2"))
    assert "Tell me what to change" in channel.to(OWNER_1)[-1]["body"]
    await handler.handle(msg(OWNER_1, text="plus court"))

    async with sm() as session:
        done = await session.get(Approval, approval.id)
    assert done.status == "edited" and done.resumed_at is not None
    assert done.payload["decision"] == {
        "action": "edited",
        "by": OWNER_1,
        "by_name": "Alice",
        "approval_id": approval.id,
        "mode": "ai",
        "text": "plus court",
    }
    state = await graph.aget_state({"configurable": {"thread_id": run_id}})
    assert state.values["decisions"][-1]["mode"] == "ai"


async def test_edit_mode_button_on_old_version_is_refused(env):
    handler, graph, sm, channel, starts = env
    run_id = await start(graph)
    approval = await open_approval(sm, run_id)
    await handler.handle(msg(OWNER_1, button=f"edit:{approval.id}:1"))
    await handler.handle(msg(OWNER_1, button=f"edit_text:{approval.id}:1"))  # stale version
    async with sm() as session:
        row = await session.get(Approval, approval.id)
    assert "edit_mode" not in row.payload
    assert channel.to(OWNER_1)[-1]["body"] == "✏️ Still waiting for your edit."


async def test_run_stopped_between_steps_is_finished_on_restart(env):
    """Review of #50: the decision was delivered, then the process died before the graph paused
    again. On restart the run must continue (not be dropped), without delivering the decision twice."""
    handler, graph, sm, channel, starts = env
    run_id = await start(graph)
    first = await open_approval(sm, run_id)
    await handler.handle(msg(OWNER_1, button=f"edit:{first.id}:1"))
    await handler.handle(msg(OWNER_1, button=f"edit_text:{first.id}:2"))
    handler.crash[0] = True
    with pytest.raises(RuntimeError, match="container restarted"):
        await handler.handle(msg(OWNER_1, text="Nouveau texte"))
    state = await graph.aget_state({"configurable": {"thread_id": run_id}})
    assert state.next == ("ask",) and not state.interrupts  # stopped between steps
    async with sm() as session:
        assert (await session.get(Approval, first.id)).resumed_at is None

    await handler.resume_unfinished()

    second = await open_approval(sm, run_id)
    assert await waiting_on(graph, run_id) == [second.id]  # the run went on to the next approval
    state = await graph.aget_state({"configurable": {"thread_id": run_id}})
    assert len(state.values["decisions"]) == 1
    async with sm() as session:
        assert (await session.get(Approval, first.id)).resumed_at is not None
