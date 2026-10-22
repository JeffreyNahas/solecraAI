"""Unsubscribe page and instant opt-out (#48): GET never unsubscribes, POST does everything at once."""

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import func, select, update

from app.db.models import Approval, AuditLog, EmailDraft, EmailSend, InboundMessage, Lead, Owner, Suppression
from app.graphs import outreach_email
from app.jobs.outreach_draft import outreach_draft_job
from app.main import create_app
from app.runs import RunHandle
from app.suppression import is_suppressed
from tests.helpers import OWNER_1, OWNER_2
from tests.test_outreach_flow import THURSDAY, ScriptedLLM, add_lead

TOKEN = "tok-abc123"


@pytest.fixture
def app_with_company(settings, channel):
    settings = settings.model_copy(
        update={
            "company_name": "Solecra",
            "company_sender_name": "Carl Abi Yaghi",
            "company_address": "1 rue Exemple, Saint-Laurent, QC",
            "company_phone": "+1 514 555-0100",
            "company_email": "info@solecra.example",
            "company_website": "https://solecrablinds.com",
            "public_base_url": "https://bot.solecra.example",
        }
    )

    @asynccontextmanager
    async def _run():
        app = create_app(settings, channel=channel)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                yield app, client

    return _run


async def open_windows(sm) -> None:
    """Both owners wrote to the bot just now (free-form WhatsApp text can reach them)."""
    async with sm() as session:
        await session.execute(update(Owner).values(last_inbound_at=datetime.now(UTC)))
        await session.commit()


async def add_send(sm, key, to, status="queued", token=None, lead_id=None, mode="live") -> str:
    async with sm() as session:
        session.add(
            EmailSend(
                idempotency_key=key,
                lead_id=lead_id,
                mode=mode,
                intended_to=to,
                subject="Objet",
                rendered_text="Bonjour",
                content_sha256="0" * 64,
                unsubscribe_token=token,
                status=status,
            )
        )
        await session.commit()
    return key


async def statuses(sm, model, column="status", order="id") -> list[str]:
    async with sm() as session:
        return list(
            (await session.execute(select(getattr(model, column)).order_by(getattr(model, order)))).scalars()
        )


async def test_get_shows_the_page_and_never_unsubscribes(app_with_company):
    async with app_with_company() as (app, client):
        sm = app.state.sessionmaker
        await add_send(sm, "initial:1:live", "info@acme.example", token=TOKEN)
        for _ in range(3):  # a mail scanner opening the link repeatedly
            resp = await client.get(f"/u/{TOKEN}")
            assert resp.status_code == 200
        assert "Me désabonner" in resp.text and "Unsubscribe" in resp.text and "Solecra" in resp.text
        assert resp.headers["cache-control"] == "no-store" and "noindex" in resp.headers["x-robots-tag"]
        assert f'action="/u/{TOKEN}"' in resp.text
        async with sm() as session:
            assert not await is_suppressed(session, "info@acme.example")
            assert await session.scalar(select(func.count()).select_from(Suppression)) == 0


async def test_post_suppresses_the_address_and_the_company_domain(app_with_company, channel):
    async with app_with_company() as (app, client):
        sm = app.state.sessionmaker
        a1 = await add_lead(sm, "A1", "info@acme.example", 80)
        a2 = await add_lead(sm, "A2", "ventes@acme.example", 70)
        other = await add_lead(sm, "B", "info@autre.example", 70)
        await add_send(sm, "initial:1:live", "info@acme.example", token=TOKEN, lead_id=a1)

        resp = await client.post(f"/u/{TOKEN}")

        assert (
            resp.status_code == 200
            and "Vous êtes désabonné" in resp.text
            and "You are unsubscribed" in resp.text
        )
        async with sm() as session:
            assert await is_suppressed(session, "info@acme.example")
            assert await is_suppressed(session, "anyone@acme.example")  # the whole company domain
            assert not await is_suppressed(session, "info@autre.example")
            reasons = set((await session.execute(select(Suppression.reason))).scalars())
            assert reasons == {"unsubscribe"}
            leads = {lead.id: lead.status for lead in (await session.execute(select(Lead))).scalars()}
        assert leads == {a1: "do_not_contact", a2: "do_not_contact", other: "qualified"}


async def test_free_mail_unsubscribe_covers_only_that_address(app_with_company):
    async with app_with_company() as (app, client):
        sm = app.state.sessionmaker
        mine = await add_lead(sm, "A", "renotremblay@gmail.com", 80)
        someone = await add_lead(sm, "B", "autre.personne@gmail.com", 70)
        await add_send(sm, "initial:1:live", "renotremblay@gmail.com", token=TOKEN, lead_id=mine)
        await client.post(f"/u/{TOKEN}")
        async with sm() as session:
            assert await is_suppressed(session, "renotremblay@gmail.com")
            assert not await is_suppressed(session, "autre.personne@gmail.com")
            assert not await is_suppressed(session, "x@gmail.com")  # gmail.com itself is never suppressed
            assert (await session.get(Lead, mine)).status == "do_not_contact"
            assert (await session.get(Lead, someone)).status == "qualified"


async def test_underscore_in_an_address_does_not_match_other_people(app_with_company):
    """LIKE would treat "_" as a wildcard: j_doe@gmail.com must not hit jxdoe@gmail.com."""
    async with app_with_company() as (app, client):
        sm = app.state.sessionmaker
        mine = await add_lead(sm, "A", "j_doe@gmail.com", 80)
        other = await add_lead(sm, "B", "jxdoe@gmail.com", 70)
        await add_send(sm, "initial:1:live", "j_doe@gmail.com", token=TOKEN, lead_id=mine)
        await add_send(sm, "initial:2:live", "jxdoe@gmail.com", token="other-token", lead_id=other)
        await client.post(f"/u/{TOKEN}")
        async with sm() as session:
            assert (await session.get(Lead, other)).status == "qualified"
            assert (await session.get(EmailSend, "initial:2:live")).status == "queued"


async def test_unsent_emails_are_cancelled_but_a_sent_one_is_left_alone(app_with_company, channel):
    async with app_with_company() as (app, client):
        sm = app.state.sessionmaker
        await open_windows(sm)
        await add_send(sm, "initial:1:live", "info@acme.example", status="sent", token=TOKEN)
        await add_send(sm, "initial:2:live", "ventes@acme.example", status="queued", token="t2")
        await add_send(sm, "initial:3:live", "projets@acme.example", status="held", token="t3")
        await add_send(sm, "initial:4:live", "info@autre.example", status="queued", token="t4")
        await client.post(f"/u/{TOKEN}")
        assert await statuses(sm, EmailSend, order="idempotency_key") == [
            "sent",
            "cancelled",
            "cancelled",
            "queued",
        ]
    # Two queued/held emails were cancelled: the owners hear about it right away.
    texts = [m["body"] for m in channel.sent if m["type"] == "text"]
    assert any("2 queued email(s) were cancelled" in t for t in texts)
    assert {m["to"] for m in channel.sent} == {OWNER_1, OWNER_2}


async def test_owners_are_not_pinged_when_nothing_was_pending(app_with_company, channel):
    async with app_with_company() as (app, client):
        sm = app.state.sessionmaker
        await add_send(sm, "initial:1:live", "info@acme.example", status="sent", token=TOKEN)
        await client.post(f"/u/{TOKEN}")
    assert channel.sent == []


async def test_repeating_and_unknown_tokens_are_harmless(app_with_company):
    async with app_with_company() as (app, client):
        sm = app.state.sessionmaker
        await add_send(sm, "initial:1:live", "info@acme.example", token=TOKEN)
        first = await client.post(f"/u/{TOKEN}")
        again = await client.post(f"/u/{TOKEN}")
        assert first.text == again.text and "Vous êtes désabonné" in again.text
        for bad in ("nope", "x" * 500, "a%20b"):
            page = await client.get(f"/u/{bad}")
            posted = await client.post(f"/u/{bad}")
            assert page.status_code == 200 and posted.status_code in (200, 404)
        unknown = await client.post("/u/nope")
        assert "Ce lien n'est plus valide" in unknown.text and "info@solecra.example" in unknown.text
        assert "acme" not in unknown.text  # reveals nothing about who was emailed
        async with sm() as session:
            assert (
                await session.scalar(select(func.count()).select_from(Suppression)) == 2
            )  # address + domain


async def test_one_click_post_from_a_mail_provider_unsubscribes(app_with_company):
    """RFC 8058: the provider POSTs `List-Unsubscribe=One-Click` to the header URL, no browser."""
    async with app_with_company() as (app, client):
        sm = app.state.sessionmaker
        lead = await add_lead(sm, "A", "info@acme.example", 80)
        await add_send(sm, "initial:1:live", "info@acme.example", token=TOKEN, lead_id=lead)
        resp = await client.post(
            f"/u/{TOKEN}",
            content="List-Unsubscribe=One-Click",
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        assert resp.status_code == 200
        async with sm() as session:
            assert (await session.get(Lead, lead)).status == "do_not_contact"


async def test_audit_log_has_the_domain_never_the_address(app_with_company):
    async with app_with_company() as (app, client):
        sm = app.state.sessionmaker
        lead = await add_lead(sm, "A", "info@acme.example", 80)
        await add_send(sm, "initial:1:live", "info@acme.example", token=TOKEN, lead_id=lead)
        await client.post(f"/u/{TOKEN}")
        async with sm() as session:
            rows = list((await session.execute(select(AuditLog))).scalars())
        blob = " ".join(f"{r.actor} {r.action} {r.entity} {r.entity_id} {r.details}" for r in rows)
        assert "acme.example" in blob and "info@acme.example" not in blob


async def test_an_unsubscribe_closes_the_draft_waiting_for_approval(app_with_company, channel):
    """Full path: a real draft is pending for the owners; the prospect unsubscribes; the draft is
    closed, the approval rejected by the system, the lead is do_not_contact, nothing is queued."""
    async with app_with_company() as (app, client):
        sm, deps = app.state.sessionmaker, app.state.deps
        deps.llm = ScriptedLLM()
        deps.web = None
        lead = await add_lead(sm, "A1", "info@groupehd.example", 90)
        await open_windows(sm)
        await outreach_draft_job(deps, RunHandle(0, "outreach_draft"), now=THURSDAY)
        async with sm() as session:
            approval = await session.scalar(select(Approval).where(Approval.status == "pending"))
            draft = await session.scalar(select(EmailDraft))
        assert approval is not None and draft.status == "pending"
        # The same company was emailed before and has a token; it now opts out.
        await add_send(sm, "initial:0:live", "info@groupehd.example", status="sent", token=TOKEN)

        await client.post(f"/u/{TOKEN}")

        async with sm() as session:
            approval = await session.get(Approval, approval.id)
            draft = await session.get(EmailDraft, draft.id)
            assert (approval.status, approval.decided_by, approval.resumed_at is not None) == (
                "rejected",
                "system",
                True,
            )
            assert approval.payload["decision"]["action"] == "unsubscribed"
            assert draft.status == "cancelled"
            assert (await session.get(Lead, lead)).status == "do_not_contact"
            assert await session.scalar(select(func.count()).select_from(EmailSend)) == 1  # only the old one
        assert any("1 draft(s) waiting for approval were closed" in m["body"] for m in channel.sent)
        # The owner's old buttons are now stale: nothing can queue an email for this company.
        await client.post(f"/u/{TOKEN}")
        async with sm() as session:
            assert await session.scalar(select(func.count()).select_from(EmailSend)) == 1


async def test_later_drafting_skips_an_unsubscribed_company(app_with_company):
    async with app_with_company() as (app, client):
        sm, deps = app.state.sessionmaker, app.state.deps
        deps.llm = ScriptedLLM()
        await add_send(sm, "initial:0:live", "info@acme.example", status="sent", token=TOKEN)
        await client.post(f"/u/{TOKEN}")
        new_permit = await add_lead(sm, "NEW", "projets@acme.example", 95)  # a later project, same company
        run = RunHandle(0, "outreach_draft")
        await outreach_draft_job(deps, run, now=THURSDAY)
        async with sm() as session:
            assert (await session.get(Lead, new_permit)).status == "do_not_contact"
        assert run.counts.get("suppressed") == 1 and deps.llm.users == []


async def test_owner_notice_waits_for_an_open_window(app_with_company, channel):
    """Review of #53: free-form text outside the 24 h window is lost, so it isn't sent."""
    async with app_with_company() as (app, client):
        sm = app.state.sessionmaker
        await add_send(sm, "initial:2:live", "ventes@acme.example", status="queued", token=TOKEN)
        await client.post(f"/u/{TOKEN}")  # nobody wrote to the bot recently
        assert await statuses(sm, EmailSend, order="idempotency_key") == ["cancelled"]
    assert channel.sent == []


async def test_a_test_mode_unsubscribe_covers_the_address_only(app_with_company):
    """Review of #53: a tester on the company's own domain must not suppress the whole domain."""
    async with app_with_company() as (app, client):
        sm = app.state.sessionmaker
        tester = await add_lead(sm, "T", "jeff@solecra.example", 80)
        colleague = await add_lead(sm, "C", "carl@solecra.example", 70)
        await add_send(sm, "initial:1:test", "jeff@solecra.example", token=TOKEN, lead_id=tester, mode="test")
        await client.post(f"/u/{TOKEN}")
        async with sm() as session:
            assert await is_suppressed(session, "jeff@solecra.example")
            assert not await is_suppressed(session, "carl@solecra.example")
            assert (await session.get(Lead, tester)).status == "do_not_contact"
            assert (await session.get(Lead, colleague)).status == "qualified"


async def test_an_old_button_after_an_unsubscribe_says_why(app_with_company, channel):
    async with app_with_company() as (app, client):
        sm, deps, handler = app.state.sessionmaker, app.state.deps, app.state.handler
        deps.llm = ScriptedLLM()
        deps.web = None
        await add_lead(sm, "A1", "info@groupehd.example", 90)
        await open_windows(sm)
        await outreach_draft_job(deps, RunHandle(0, "outreach_draft"), now=THURSDAY)
        async with sm() as session:
            approval = await session.scalar(select(Approval).where(Approval.status == "pending"))
        await add_send(sm, "initial:0:live", "info@groupehd.example", status="sent", token=TOKEN)
        await client.post(f"/u/{TOKEN}")

        await handler.handle(inbound(OWNER_1, button=f"approve:{approval.id}:1"))

        assert channel.to(OWNER_1)[-1]["body"] == "Closed: the prospect unsubscribed."
        async with sm() as session:
            assert await session.scalar(select(func.count()).select_from(EmailSend)) == 1


def inbound(sender, *, button=None):
    return InboundMessage(
        message_id=f"wamid.{uuid.uuid4().hex[:8]}",
        sender=sender,
        msg_type="button",
        button_id=button,
        payload={},
    )


async def test_unsubscribe_racing_an_approval_leaves_no_queued_email(app_with_company, monkeypatch):
    """Review of #53 (blocking): the owner taps Approve while the prospect unsubscribes. The
    approval is paused right after its email row is inserted (not yet committed); the unsubscribe
    runs meanwhile. Either order must end with no queued email and a do_not_contact lead."""
    async with app_with_company() as (app, client):
        sm, deps, handler = app.state.sessionmaker, app.state.deps, app.state.handler
        deps.llm = ScriptedLLM()
        deps.web = None
        lead = await add_lead(sm, "A1", "info@groupehd.example", 90)
        await outreach_draft_job(deps, RunHandle(0, "outreach_draft"), now=THURSDAY)
        async with sm() as session:
            approval = await session.scalar(select(Approval).where(Approval.status == "pending"))
        await add_send(sm, "initial:0:live", "info@groupehd.example", status="sent", token=TOKEN)

        inserted, release = asyncio.Event(), asyncio.Event()
        real_move = outreach_email.move_leads

        async def paused_move(*args, **kwargs):
            inserted.set()  # the email row is inserted but not committed
            await release.wait()
            return await real_move(*args, **kwargs)

        monkeypatch.setattr(outreach_email, "move_leads", paused_move)
        tap = asyncio.create_task(handler.handle(inbound(OWNER_1, button=f"approve:{approval.id}:1")))
        await asyncio.wait_for(inserted.wait(), 10)
        unsubscribe_request = asyncio.create_task(client.post(f"/u/{TOKEN}"))
        await asyncio.sleep(0.5)
        assert not unsubscribe_request.done()  # waiting for the approval's lock on the lead
        release.set()
        await asyncio.gather(tap, unsubscribe_request)

        async with sm() as session:
            sends = {
                s.idempotency_key: s.status for s in (await session.execute(select(EmailSend))).scalars()
            }
            assert sends[f"initial:{lead}:test"] == "cancelled"  # queued, then cancelled by the opt-out
            assert (await session.get(Lead, lead)).status == "do_not_contact"
