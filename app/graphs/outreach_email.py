"""Outreach email graph: AI draft → output guard → owner approval → queue the email.

    compose ──(guard ok)──► request_approval ──► wait (interrupt) ──► apply
       │ (blocked)                ▲                                  │
       ▼                          └──── AI changes / refused edit ───┤
      END                                                            ▼
                                       approve / own text ok → queue send (email_sends) → END
                                       reject → lead disqualified → END · expire → lead requalified → END

One run per company draft: `thread_id = run_id = "outreach_email:<draft_id>"`. The draft row and
its leads (moved `qualified → drafted`) are created by the 08:00 job before the run starts.

Safety (CLAUDE.md §2, §10):
- `wait` only calls interrupt(); every other node can re-run after a crash, so each checks what
  it already did (draft status, history entry for this approval, send row by idempotency key).
- The graph never sends email: approving inserts one `email_sends` row keyed
  `initial:<primary lead>:<mode>` (ON CONFLICT DO NOTHING). Gmail sending is #49.
- In test mode an approved email to anyone outside EMAIL_TEST_RECIPIENTS is `held`, never sent.
- Owners are shown drafts by `app/outreach/present.py`, not by this graph: the run is already
  paused when the buttons go out (the API's per-run lock doesn't cover the scheduler process).
"""

import logging
from datetime import UTC, datetime
from typing import Any, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app import approvals
from app.channel import ChannelError
from app.db.audit import audit
from app.db.models import EmailDraft, EmailSend, Lead
from app.deps import Deps
from app.drafting import compose, guard_context, guard_owner_text, llm_record, parse_owner_text, revise
from app.guards.output import GuardContext, check_rendered
from app.leads import TransitionError, transition
from app.llm import LLMError
from app.outreach.render import Identity, new_unsubscribe_token, render

log = logging.getLogger(__name__)

ITEM_TYPE = "outreach_email"
ACTOR = "outreach_email"


class OutreachState(TypedDict, total=False):
    run_id: str
    draft_id: int
    approval_id: int
    decision: dict[str, Any]
    next: str  # ask | end (route after apply)
    outcome: str


def run_id_for(draft_id: int) -> str:
    return f"{ITEM_TYPE}:{draft_id}"


async def load(deps: Deps, draft_id: int) -> tuple[EmailDraft, list[Lead]]:
    async with deps.sessionmaker() as session:
        draft = await session.get(EmailDraft, draft_id)
        assert draft is not None, f"draft {draft_id} not found"
        leads = list((await session.execute(select(Lead).where(Lead.id.in_(draft.lead_ids)))).scalars())
    primary_first = sorted(leads, key=lambda lead: (lead.id != draft.lead_id, lead.id))
    return draft, primary_first


def context_for(deps: Deps, draft: EmailDraft, leads: list[Lead]) -> GuardContext:
    s = deps.settings
    return guard_context(
        leads,
        language=draft.language,
        allowed=s.allowed_numbers(),
        blocked=s.blocked_terms(),
        website_host=Identity.from_settings(s).website_host,
    )


async def move_leads(session, lead_ids: list[int], frm: str, to: str, reason: str | None = None) -> None:
    """Move each lead still in `frm` to `to`. Leads already moved (a re-run) or changed meanwhile
    (e.g. an unsubscribe made them do_not_contact) are left alone."""
    for lead_id in lead_ids:
        lead = await session.get(Lead, lead_id)
        if lead is None or lead.status != frm:
            continue
        try:
            await transition(session, lead_id, to, expected_version=lead.version, actor=ACTOR, reason=reason)
        except TransitionError:
            log.warning("lead %s changed while moving %s -> %s", lead_id, frm, to)


def build_outreach_graph(deps: Deps, checkpointer: BaseCheckpointSaver):
    async def compose_node(state: OutreachState) -> OutreachState:
        draft, leads = await load(deps, state["draft_id"])
        if draft.status != "drafting":  # composed before a crash
            return {"next": "ask" if draft.status == "pending" else "end"}
        if deps.llm is None:
            raise LLMError("no LLM configured (OPENROUTER_API_KEY empty)")
        composed = await compose(deps.llm, leads, draft.language, context_for(deps, draft, leads))
        async with deps.sessionmaker() as session:
            row = await session.get(EmailDraft, draft.id)
            row.subject, row.body = composed.subject, composed.body
            row.author, row.guard, row.llm = "ai", composed.guard.as_dict(), llm_record(composed)
            if composed.guard.ok:
                row.status = "pending"
            else:
                # Blocked twice by the guard: owners never see it; the lead can be drafted another day.
                row.status = "blocked"
                await move_leads(
                    session, draft.lead_ids, "drafted", "qualified", reason="draft blocked by guard"
                )
            audit(session, ACTOR, f"draft_{row.status}", "email_draft", draft.id, **composed.guard.as_dict())
            await session.commit()
        return {"next": "ask" if composed.guard.ok else "end"}

    async def request_approval(state: OutreachState) -> OutreachState:
        draft, _ = await load(deps, state["draft_id"])
        async with deps.sessionmaker() as session:
            approval = await approvals.get_or_create_pending(
                session,
                state["run_id"],
                ITEM_TYPE,
                str(draft.id),
                {"draft_id": draft.id, "revision": draft.revision},
            )
            audit(session, "system", "approval_requested", "approval", approval.id, draft_id=draft.id)
            await session.commit()
        return {"approval_id": approval.id}

    async def wait(state: OutreachState) -> OutreachState:
        decision = interrupt({"approval_id": state["approval_id"]})
        return {"decision": decision}

    async def apply(state: OutreachState) -> OutreachState:
        decision = state["decision"]
        action = decision.get("action")
        draft, leads = await load(deps, state["draft_id"])
        if action == "approve":
            return await queue_send(draft, leads, decision)
        if action == "reject":
            return await close(draft, "rejected", "drafted", "disqualified", "owner_rejected", decision)
        if action == "expire":
            return await close(draft, "expired", "drafted", "qualified", "draft expired", decision)
        if action == "edited":
            return await apply_edit(draft, leads, decision, state["approval_id"])
        log.error("unknown decision %r on draft %s", action, draft.id)
        return {"next": "end", "outcome": "unknown_decision"}

    async def close(
        draft: EmailDraft, status: str, frm: str, to: str, reason: str, decision: dict[str, Any]
    ) -> OutreachState:
        async with deps.sessionmaker() as session:
            row = await session.get(EmailDraft, draft.id)
            if row.status == "pending":
                row.status = status
                await move_leads(session, draft.lead_ids, frm, to, reason=reason)
                audit(session, decision.get("by", "system"), f"draft_{status}", "email_draft", draft.id)
            await session.commit()
        return {"next": "end", "outcome": status}

    async def queue_send(draft: EmailDraft, leads: list[Lead], decision: dict[str, Any]) -> OutreachState:
        """Approved: render the final email and queue it once. Re-running is harmless."""
        settings = deps.settings
        primary = leads[0]
        identity = Identity.from_settings(settings)
        mode = settings.email_mode
        key = f"initial:{primary.id}:{mode}"
        token = new_unsubscribe_token()
        email = render(
            subject=draft.subject or "",
            body=draft.body or "",
            language=draft.language,
            contact_name=primary.contact_name,
            consent_source_url=primary.consent_source_url,
            consent_role=primary.consent_role,
            identity=identity,
            unsubscribe_token=token,
        )
        final = check_rendered(
            email.text, email.headers, identity.required_in_footer(), email.unsubscribe_url
        )
        if not final.ok:  # code bug or config change: never queue an incomplete email
            raise RuntimeError(f"rendered email failed the final check: {final.violations}")
        intended = (primary.email or "").strip().lower()
        held = mode == "test" and intended not in settings.test_recipients()
        async with deps.sessionmaker() as session:
            inserted = await session.execute(
                insert(EmailSend)
                .values(
                    idempotency_key=key,
                    draft_id=draft.id,
                    revision=draft.revision,
                    lead_id=primary.id,
                    mode=mode,
                    intended_to=intended,
                    subject=email.subject,
                    rendered_text=email.text,
                    content_sha256=email.sha256,
                    unsubscribe_token=token,
                    status="held" if held else "queued",
                    approved_by=decision.get("by"),
                )
                .on_conflict_do_nothing(index_elements=["idempotency_key"])
                .returning(EmailSend.status)
            )
            status = inserted.scalar_one_or_none()
            row = await session.get(EmailDraft, draft.id)
            if row.status == "pending":
                row.status = "approved"
            await move_leads(session, draft.lead_ids, "drafted", "approved", reason="owner approved")
            if status is not None:
                audit(
                    session,
                    decision.get("by", "system"),
                    "outreach_queued",
                    "email_send",
                    key,
                    draft_id=draft.id,
                    revision=draft.revision,
                    status=status,
                )
            await session.commit()
        return {"next": "end", "outcome": status or "already_queued"}

    async def apply_edit(
        draft: EmailDraft, leads: list[Lead], decision: dict[str, Any], approval_id: int
    ) -> OutreachState:
        already = any(h.get("approval_id") == approval_id for h in draft.history or [])
        if already:  # re-run after a crash: the edit was saved, continue from it
            done = draft.author == "owner" and draft.status == "pending"
            return await queue_send(draft, leads, decision) if done else {"next": "ask"}

        ctx = context_for(deps, draft, leads)
        mode = decision.get("mode", "text")
        owner = decision.get("by", "")
        if mode == "ai":
            if deps.llm is None:
                await notify(
                    owner, approval_id, "🤖 The AI isn't available right now. The draft is unchanged."
                )
                return {"next": "ask"}
            try:
                revised = await revise(
                    deps.llm,
                    draft.subject or "",
                    draft.body or "",
                    decision.get("text", ""),
                    draft.language,
                    ctx,
                )
            except LLMError:
                log.exception("AI revision failed for draft %s", draft.id)
                await notify(
                    owner, approval_id, "🤖 The AI revision failed. The draft is unchanged; try again."
                )
                return {"next": "ask"}
            if not revised.guard.ok:
                await notify(owner, approval_id, blocked_text(revised.guard.violations, ai=True))
                return {"next": "ask"}
            await save_revision(draft, revised.subject, revised.body, "ai", approval_id, llm_record(revised))
            return {"next": "ask"}

        subject, body = parse_owner_text(decision.get("text", ""), draft.subject or "")
        result = guard_owner_text(subject, body, ctx)
        if not result.ok:
            await notify(owner, approval_id, blocked_text(result.violations, ai=False))
            return {"next": "ask"}
        draft = await save_revision(draft, subject, body, "owner", approval_id, None)
        # Decided 2026-10-22: the owner's own text is sent as written once it passes the guard.
        return await queue_send(draft, leads, decision)

    async def save_revision(
        draft: EmailDraft, subject: str, body: str, author: str, approval_id: int, llm: dict[str, Any] | None
    ) -> EmailDraft:
        async with deps.sessionmaker() as session:
            row = await session.get(EmailDraft, draft.id)
            # The text being replaced, and the approval whose edit replaced it (marks the edit done).
            previous = {
                "revision": row.revision,
                "author": row.author,
                "subject": row.subject,
                "body": row.body,
                "approval_id": approval_id,
                "replaced_at": datetime.now(UTC).isoformat(),
            }
            row.history = [*(row.history or []), previous]
            row.revision += 1
            row.subject, row.body, row.author = subject, body, author
            row.guard = {"ok": True, "violations": []}
            if llm:
                row.llm = llm
            audit(
                session, ACTOR, "draft_revised", "email_draft", row.id, revision=row.revision, author=author
            )
            await session.commit()
            await session.refresh(row)
            return row

    async def notify(phone: str, approval_id: int, text: str) -> None:
        if not phone:
            return
        try:
            await deps.channel.send_text(phone, text, key=f"approval:{approval_id}:edit_result:{phone}")
        except ChannelError:
            log.exception("could not tell %s about the edit result", phone)

    def route_compose(state: OutreachState) -> str:
        return "request_approval" if state.get("next") == "ask" else END

    def route_apply(state: OutreachState) -> str:
        return "request_approval" if state.get("next") == "ask" else END

    graph = StateGraph(OutreachState)
    graph.add_node("compose", compose_node)
    graph.add_node("request_approval", request_approval)
    graph.add_node("wait", wait)
    graph.add_node("apply", apply)
    graph.add_edge(START, "compose")
    graph.add_conditional_edges("compose", route_compose, ["request_approval", END])
    graph.add_edge("request_approval", "wait")
    graph.add_edge("wait", "apply")
    graph.add_conditional_edges("apply", route_apply, ["request_approval", END])
    return graph.compile(checkpointer=checkpointer)


def blocked_text(violations: list[str], *, ai: bool) -> str:
    who = "The AI's revision" if ai else "Your text"
    lines = "\n".join(f"• {v}" for v in violations)
    return f"⚠️ {who} can't be sent:\n{lines}\n\nThe draft is unchanged. I'll show it again."
