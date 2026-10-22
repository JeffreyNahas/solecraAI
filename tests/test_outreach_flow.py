"""Outreach drafting end to end (#47): 08:00 job → AI draft → guard → owners on WhatsApp →
approve / edit / reject / expire → exactly one queued email. The model is scripted; the database,
graph, handler and presenter are real."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import func, select, update

from app.db.models import Approval, EmailDraft, EmailSend, InboundMessage, Lead, Owner
from app.db.session import make_engine, make_sessionmaker
from app.deps import Deps
from app.drafting import DraftEmail
from app.graphs.outreach_email import ITEM_TYPE, build_outreach_graph
from app.handlers import GraphEntry, Handler
from app.jobs.outreach_draft import outreach_draft_job
from app.leads import LeadIn, email_domain, insert_lead
from app.llm import LLMError, LLMResult
from app.outreach import present
from app.owners import sync_owners
from app.runs import RunHandle
from app.settings_store import OUTREACH_DAILY_CAP, OUTREACH_PAUSED, set_setting
from app.suppression import suppress
from tests.helpers import OWNER_1, OWNER_2, FakeChannel
from tests.test_web_and_consent import fetcher

THURSDAY = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
GOOD = DraftEmail(
    subject="Stores et rideaux pour votre projet",
    body=(
        "Nous avons vu votre projet résidentiel et nous aimerions vous aider "
        "avec les stores et les rideaux.\n\n"
        "Solecra fournit et installe des stores et des rideaux pour les projets à Montréal.\n\n"
        "Nous vous offrons une évaluation gratuite sur place, au moment qui vous convient."
    ),
)
BAD = DraftEmail(
    subject="Promo", body="Nos prix sont les meilleurs : 20 % de rabais pendant deux semaines seulement."
)


class ScriptedLLM:
    def __init__(self) -> None:
        self.replies: list[DraftEmail | Exception] = []
        self.users: list[str] = []

    async def structured(self, step, system, user, schema):
        assert step == "email_draft" and schema is DraftEmail
        self.users.append(user)
        reply = self.replies.pop(0) if self.replies else GOOD
        if isinstance(reply, Exception):
            raise reply
        return LLMResult(reply, "anthropic/claude-sonnet-5.5", 1000, 200, 0.01)


@pytest.fixture
async def env(settings):
    settings = settings.model_copy(
        update={
            "company_sender_name": "Carl Abi Yaghi",
            "company_address": "1 rue Exemple, Saint-Laurent, QC",
            "company_phone": "+1 514 555-0100",
            "company_email": "info@solecra.example",
            "company_website": "https://solecrablinds.com",
            "public_base_url": "https://bot.solecra.example",
            "email_test_recipients": "info@groupehd.example",
        }
    )
    engine = make_engine(settings)
    sm = make_sessionmaker(engine)
    await sync_owners(sm, settings.owners())
    async with sm() as session:  # Alice wrote recently (window open); Bob didn't
        await session.execute(
            update(Owner).where(Owner.phone == OWNER_1).values(last_inbound_at=datetime.now(UTC))
        )
        await session.commit()
    channel, llm, saver = FakeChannel(), ScriptedLLM(), InMemorySaver()
    deps = Deps(settings, sm, channel, llm=llm, web=fetcher({}), checkpointer=saver)

    async def next_draft():
        await present.present_next(deps)

    async def show(owner):
        await present.present_next(deps, only_phone=owner.phone)

    graph = build_outreach_graph(deps, saver)
    handler = Handler(
        deps,
        {ITEM_TYPE: GraphEntry(graph, ai_edits=True, after_decision=next_draft)},
        commands={"drafts": show, present.SHOW_DRAFTS: show},
    )
    yield deps, handler, channel, llm, sm
    await engine.dispose()


async def add_lead(sm, ref, email, score, *, status="qualified", address=None, captured=None, **extra) -> int:
    async with sm() as session:
        lead, _ = await insert_lead(
            session,
            LeadIn(
                source="permit",
                source_ref=ref,
                address=address or f"{ref} rue Exemple",
                source_details={"work": "new_construction", "building": "multi_residential", "units": 40},
            ),
        )
        await session.execute(
            update(Lead)
            .where(Lead.id == lead.id)
            .values(
                status=status,
                score=score,
                email=email,
                email_domain=email_domain(email),
                business_name=extra.get("business_name", f"Entreprise {ref}"),
                consent_source_url=f"https://{(email_domain(email) or 'annuaire.example')}/contact",
                consent_captured_at=captured or datetime.now(UTC),
                consent_role="developer",
                relevance_note="Developer of the building at this address.",
                research_details=extra.get("research_details"),
            )
        )
        await session.commit()
        return lead.id


async def run_job(deps, now=THURSDAY) -> RunHandle:
    run = RunHandle(0, "outreach_draft")
    await outreach_draft_job(deps, run, now=now)
    return run


def msg(sender, *, button=None, text=None) -> InboundMessage:
    return InboundMessage(
        message_id=f"wamid.{uuid.uuid4().hex[:8]}",
        sender=sender,
        msg_type="button" if button else "text",
        button_id=button,
        text=text,
        payload={},
    )


async def pending(sm) -> list[Approval]:
    async with sm() as session:
        return list(
            (
                await session.execute(
                    select(Approval).where(Approval.status == "pending").order_by(Approval.id)
                )
            ).scalars()
        )


async def get(sm, model, key):
    async with sm() as session:
        return await session.get(model, key)


async def test_job_drafts_one_email_per_company_and_tells_owners(env):
    deps, handler, channel, llm, sm = env
    a1 = await add_lead(sm, "A1", "info@groupehd.example", 90)
    a2 = await add_lead(sm, "A2", "ventes@groupehd.example", 70)
    b = await add_lead(sm, "B", "renotremblay@gmail.com", 80)
    c = await add_lead(sm, "C", "info@contacted.example", 85)
    d = await add_lead(sm, "D", "hello@contacted.example", 60, status="sent")

    run = await run_job(deps)

    assert run.counts["drafts_ready"] == 2 and run.counts["already_contacted"] == 1
    assert run.counts["pending_for_owners"] == 2
    async with sm() as session:
        drafts = list((await session.execute(select(EmailDraft).order_by(EmailDraft.id))).scalars())
    assert [(dr.lead_id, sorted(dr.lead_ids), dr.status) for dr in drafts] == [
        (a1, sorted([a1, a2]), "pending"),
        (b, [b], "pending"),
    ]
    assert (await get(sm, Lead, a2)).primary_lead_id == a1 and (await get(sm, Lead, a2)).status == "drafted"
    lead_c = await get(sm, Lead, c)
    assert lead_c.primary_lead_id == d and lead_c.status == "qualified"  # company already emailed

    # Alice (window open): morning text, then the first draft with its evidence, then buttons.
    alice = channel.to(OWNER_1)
    assert alice[0]["body"] == "Good morning! 2 email drafts are ready."
    assert (
        "To: info@groupehd.example" in alice[1]["body"] and "Subject: Stores et rideaux" in alice[1]["body"]
    )
    assert "A2 rue Exemple" in alice[1]["body"]  # both permits of the company
    assert alice[2]["type"] == "buttons" and [t for _, t in alice[2]["buttons"]] == [
        "Approve",
        "Edit",
        "Reject",
    ]
    # Bob (window closed): only the approved template, with the Show drafts payload.
    [bob] = channel.to(OWNER_2)
    assert (bob["type"], bob["name"], bob["params"], bob["button_payloads"]) == (
        "template",
        "drafts_ready",
        ["2"],
        ["show_drafts"],
    )
    # The model got the facts as a data block.
    assert llm.users[0].startswith("<lead>") and "A2 rue Exemple" in llm.users[0]


async def test_approve_queues_exactly_one_email_and_shows_the_next(env):
    deps, handler, channel, llm, sm = env
    a1 = await add_lead(sm, "A1", "info@groupehd.example", 90)
    b = await add_lead(sm, "B", "renotremblay@gmail.com", 80)
    await run_job(deps)
    first, second = await pending(sm)

    await handler.handle(msg(OWNER_1, button=f"approve:{first.id}:1"))
    await handler.handle(msg(OWNER_2, button=f"approve:{first.id}:1"))  # too late

    async with sm() as session:
        sends = list((await session.execute(select(EmailSend))).scalars())
    [send] = sends
    assert send.idempotency_key == f"initial:{a1}:test" and send.status == "queued"  # on the test list
    assert send.approved_by == OWNER_1 and send.intended_to == "info@groupehd.example"
    assert send.rendered_text.startswith("Bonjour,\n\n" + GOOD.body)
    assert "Carl Abi Yaghi, au nom de Solecra" in send.rendered_text
    assert f"https://bot.solecra.example/u/{send.unsubscribe_token}" in send.rendered_text
    assert (await get(sm, Lead, a1)).status == "approved"
    assert (await get(sm, EmailDraft, send.draft_id)).status == "approved"
    assert channel.to(OWNER_2)[-1]["body"] == "Already handled by Alice (approved)."
    # The next draft was shown right after the decision.
    assert any(f"approval:{second.id}:v1:ask:{OWNER_1}" == m["key"] for m in channel.sent)

    # Re-delivering the decision (crash before resumed_at) never queues a second email.
    async with sm() as session:
        await session.execute(update(Approval).where(Approval.id == first.id).values(resumed_at=None))
        await session.commit()
    await handler.resume_unfinished()
    async with sm() as session:
        assert await session.scalar(select(func.count()).select_from(EmailSend)) == 1

    # A lead not on the test list is held, never queued, in test mode.
    await handler.handle(msg(OWNER_1, button=f"approve:{second.id}:1"))
    held = await get(sm, EmailSend, f"initial:{b}:test")
    assert held.status == "held"


async def test_reject_closes_the_lead(env):
    deps, handler, channel, llm, sm = env
    a1 = await add_lead(sm, "A1", "info@groupehd.example", 90)
    await run_job(deps)
    [approval] = await pending(sm)
    await handler.handle(msg(OWNER_1, button=f"reject:{approval.id}:1"))
    assert (await get(sm, Lead, a1)).status == "disqualified"
    async with sm() as session:
        assert await session.scalar(select(func.count()).select_from(EmailSend)) == 0
        draft = await session.scalar(select(EmailDraft))
    assert draft.status == "rejected"


async def test_owner_text_is_sent_as_written_after_the_guard(env):
    deps, handler, channel, llm, sm = env
    a1 = await add_lead(sm, "A1", "info@groupehd.example", 90)
    await run_job(deps)
    [approval] = await pending(sm)
    await handler.handle(msg(OWNER_1, button=f"edit:{approval.id}:1"))
    await handler.handle(msg(OWNER_1, button=f"edit_text:{approval.id}:2"))

    # A refused text: the owner is told why, and the draft comes back unchanged.
    await handler.handle(msg(OWNER_1, text="Nos prix sont imbattables, 30 % de rabais cette semaine."))
    assert "can't be sent" in channel.to(OWNER_1)[-3]["body"]
    [again] = await pending(sm)
    assert again.id != approval.id

    await handler.handle(msg(OWNER_1, button=f"edit:{again.id}:1"))
    await handler.handle(msg(OWNER_1, button=f"edit_text:{again.id}:2"))
    own = (
        "Objet: Votre projet rue Exemple\n"
        "Bonjour,\n"
        "Nous aimerions vous rencontrer pour une évaluation gratuite sur place "
        "de vos besoins en stores et rideaux."
    )
    await handler.handle(msg(OWNER_1, text=own))

    send = await get(sm, EmailSend, f"initial:{a1}:test")
    assert send.subject == "Votre projet rue Exemple"
    assert send.rendered_text.startswith(
        "Bonjour,\n\nNous aimerions vous rencontrer pour une évaluation gratuite sur place"
    )
    draft = await get(sm, EmailDraft, send.draft_id)
    assert (draft.author, draft.revision, draft.status) == ("owner", 2, "approved")
    assert draft.history[0]["subject"] == GOOD.subject
    assert len(llm.users) == 1  # no AI call for an owner's own text


async def test_ai_changes_are_shown_again_before_sending(env):
    deps, handler, channel, llm, sm = env
    a1 = await add_lead(sm, "A1", "info@groupehd.example", 90)
    await run_job(deps)
    [approval] = await pending(sm)
    shorter = DraftEmail(
        subject="Votre projet", body=GOOD.body.split("\n\n")[0] + "\n\n" + GOOD.body.split("\n\n")[2]
    )
    llm.replies = [shorter]
    await handler.handle(msg(OWNER_1, button=f"edit:{approval.id}:1"))
    await handler.handle(msg(OWNER_1, button=f"edit_ai:{approval.id}:2"))
    await handler.handle(msg(OWNER_1, text="plus court"))

    [again] = await pending(sm)
    assert again.id != approval.id and "Owner's instruction: plus court" in llm.users[-1]
    draft = await get(sm, EmailDraft, int(again.item_id))
    assert (draft.subject, draft.revision, draft.status) == ("Votre projet", 2, "pending")
    assert any(m["key"] == f"approval:{again.id}:v1:detail:{OWNER_1}" for m in channel.sent)
    async with sm() as session:
        assert await session.scalar(select(func.count()).select_from(EmailSend)) == 0  # not sent: re-asked
    assert (await get(sm, Lead, a1)).status == "drafted"


async def test_a_draft_the_guard_blocks_twice_never_reaches_the_owners(env):
    deps, handler, channel, llm, sm = env
    a1 = await add_lead(sm, "A1", "info@groupehd.example", 90)
    llm.replies = [BAD, BAD]
    run = await run_job(deps)
    assert run.counts["drafts_blocked"] == 1 and len(llm.users) == 2
    assert "refused for these reasons" in llm.users[1]  # the retry got the guard's feedback
    assert (await get(sm, Lead, a1)).status == "qualified"
    assert await pending(sm) == [] and channel.sent == []


async def test_model_failure_puts_the_lead_back(env):
    deps, handler, channel, llm, sm = env
    a1 = await add_lead(sm, "A1", "info@groupehd.example", 90)
    llm.replies = [LLMError("model down", cost_usd=0.002)]
    with pytest.raises(LLMError, match="drafting failed for all 1"):
        await run_job(deps)
    assert (await get(sm, Lead, a1)).status == "qualified"
    async with sm() as session:
        draft = await session.scalar(select(EmailDraft))
    assert draft.status == "blocked" and "model down" in draft.guard["error"]


async def test_undecided_drafts_expire_after_two_business_days(env):
    deps, handler, channel, llm, sm = env
    a1 = await add_lead(sm, "A1", "info@groupehd.example", 90)
    await run_job(deps, now=datetime(2026, 10, 19, 12, tzinfo=UTC))  # Monday
    [approval] = await pending(sm)
    async with sm() as session:
        await session.execute(
            update(Approval)
            .where(Approval.id == approval.id)
            .values(created_at=datetime(2026, 10, 19, 13, tzinfo=UTC))
        )
        await session.commit()

    run = await run_job(deps, now=datetime(2026, 10, 20, 12, tzinfo=UTC))  # Tuesday: 1 business day
    assert "drafts_expired" not in run.counts
    run = await run_job(deps, now=datetime(2026, 10, 21, 12, tzinfo=UTC))  # Wednesday: 2
    assert run.counts["drafts_expired"] == 1
    assert (await get(sm, Approval, approval.id)).status == "rejected"
    async with sm() as session:
        statuses = list((await session.execute(select(EmailDraft.status).order_by(EmailDraft.id))).scalars())
    assert statuses[0] == "expired"
    # The lead went back to qualified and was drafted again the same morning.
    assert (await get(sm, Lead, a1)).status == "drafted" and statuses[1] == "pending"


async def test_cap_pause_and_holidays(env):
    deps, handler, channel, llm, sm = env
    for i, score in enumerate([90, 80, 70]):
        await add_lead(sm, f"L{i}", f"info@firm{i}.example", score)
    async with sm() as session:
        await set_setting(session, OUTREACH_DAILY_CAP, 2, OWNER_1)
        await session.commit()
    assert (await run_job(deps)).counts["drafts_ready"] == 2
    assert (await run_job(deps)).counts == {"daily_cap_reached": 1, "pending_for_owners": 2}

    assert (await run_job(deps, now=datetime(2026, 10, 12, 12, tzinfo=UTC))).counts == {
        "not_a_business_day": 1
    }
    async with sm() as session:
        await set_setting(session, OUTREACH_PAUSED, True, OWNER_2)
        await session.commit()
    assert (await run_job(deps)).counts == {"paused": 1}


async def test_suppressed_and_stale_consent(env):
    deps, handler, channel, llm, sm = env
    gone = await add_lead(sm, "S", "info@optedout.example", 90)
    old = await add_lead(
        sm,
        "O",
        "info@oldfirm.example",
        80,
        captured=datetime.now(UTC) - timedelta(days=45),
        business_name="Old Firm",
    )
    async with sm() as session:
        await suppress(session, "optedout.example", "unsubscribe")
        await session.commit()
    deps.web = fetcher({"https://oldfirm.example/contact": "<h1>Old Firm</h1><p>Plus de courriel ici.</p>"})

    run = await run_job(deps)

    assert (await get(sm, Lead, gone)).status == "do_not_contact"
    assert (await get(sm, Lead, old)).status == "no_valid_contact"  # address no longer published
    assert run.counts["suppressed"] == 1 and run.counts["consent_no_longer_valid"] == 1
    assert "drafts_ready" not in run.counts


async def test_flagged_directory_address_and_show_drafts_button(env):
    deps, handler, channel, llm, sm = env
    research = {
        "answer": {"link_source_url": "https://news.example/projet"},
        "consent_check": {
            "evidence": {"needs_owner_check": True, "excerpt": "Rénos Express renos@gmail.com"}
        },
    }
    await add_lead(sm, "F", "renos@gmail.com", 90, research_details=research)
    await run_job(deps)
    [approval] = await pending(sm)
    assert channel.to(OWNER_2)[0]["type"] == "template"

    # Bob taps "Show drafts" on the template: his window is now open (the webhook records it).
    async with sm() as session:
        await session.execute(
            update(Owner).where(Owner.phone == OWNER_2).values(last_inbound_at=datetime.now(UTC))
        )
        await session.commit()
    await handler.handle(msg(OWNER_2, button="show_drafts"))
    detail, buttons = channel.to(OWNER_2)[-2:]
    assert "⚠️ Directory address" in detail["body"] and "Rénos Express renos@gmail.com" in detail["body"]
    assert "Company ↔ project: https://news.example/projet" in detail["body"]
    assert buttons["buttons"][0] == (f"approve:{approval.id}:1", "Approve (checked)")


async def test_drafts_command_when_nothing_waits(env):
    deps, handler, channel, llm, sm = env
    assert await present.present_next(deps) is None
    await handler.handle(msg(OWNER_1, text="drafts"))
    assert channel.sent == []  # the API's command answers "No email drafts waiting." (main.py)
