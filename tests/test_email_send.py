"""Gmail sending (#49): one email per run, every guard re-checked just before the call, and every
way the call can fail ends in the right state. Gmail is a mock transport; database, job and
sender are real."""

import base64
import email
import email.policy
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select, update

from app.db.models import EmailSend, Lead, Setting, Touch
from app.db.session import make_engine, make_sessionmaker
from app.deps import Deps
from app.jobs import JOBS
from app.jobs.email_send import email_send_job
from app.leads import LeadIn, email_domain, insert_lead
from app.outreach.render import Identity, new_unsubscribe_token, render
from app.runs import RunHandle
from app.scheduler import SCHEDULE
from app.settings_store import (
    EMAIL_NEXT_SEND_AT,
    OUTREACH_DAILY_CAP,
    OUTREACH_LIVE,
    OUTREACH_PAUSED,
    set_setting,
)
from app.suppression import suppress
from app.tools.gmail import GmailClient
from tests.helpers import FakeChannel

THURSDAY_10 = datetime(2026, 10, 22, 14, 0, tzinfo=UTC)  # 10:00 in Montréal
TO = "info@groupehd.example"


class FakeGoogle:
    """Token endpoint always works; sends follow `script` (a Response or an exception per call)."""

    def __init__(self) -> None:
        self.sends: list[httpx.Request] = []
        self.token_calls = 0
        self.script: list[httpx.Response | Exception] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            self.token_calls += 1
            return httpx.Response(200, json={"access_token": f"at{self.token_calls}", "expires_in": 3600})
        self.sends.append(request)
        reply = self.script.pop(0) if self.script else None
        if isinstance(reply, Exception):
            raise reply
        return reply or httpx.Response(
            200, json={"id": f"msg{len(self.sends)}", "threadId": f"thr{len(self.sends)}"}
        )

    def message(self, index: int = 0):
        raw = __import__("json").loads(self.sends[index].content)["raw"]
        return email.message_from_bytes(base64.urlsafe_b64decode(raw), policy=email.policy.default)


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
            "email_test_recipients": f"{TO},autre@groupehd.example",
            "gmail_client_id": "cid",
            "gmail_client_secret": "secret",
            "gmail_refresh_token": "refresh",
            "gmail_sender": "solecra.test@gmail.com",
        }
    )
    engine = make_engine(settings)
    sm = make_sessionmaker(engine)
    google = FakeGoogle()
    http = httpx.AsyncClient(transport=httpx.MockTransport(google))
    deps = Deps(settings, sm, FakeChannel(), gmail=GmailClient(settings, http=http))
    yield deps, google, sm
    await http.aclose()
    await engine.dispose()


async def queue(sm, settings, ref="a", to=TO, *, lead_status="approved", status="queued", mode=None) -> str:
    """An approved email waiting in the queue, as `queue_send` leaves it."""
    token = new_unsubscribe_token()
    rendered = render(
        subject="Stores et rideaux pour votre projet",
        body="Nous aimerions vous offrir une évaluation gratuite sur place.",
        language="fr",
        contact_name="Marie Tremblay",
        consent_source_url=f"https://{email_domain(to)}/contact",
        consent_role="developer",
        identity=Identity.from_settings(settings),
        unsubscribe_token=token,
    )
    async with sm() as session:
        lead, _ = await insert_lead(
            session, LeadIn(source="permit", source_ref=ref, address=f"{ref} rue Exemple")
        )
        await session.execute(
            update(Lead)
            .where(Lead.id == lead.id)
            .values(
                status=lead_status,
                email=to,
                email_domain=email_domain(to),
                consent_source_url=f"https://{email_domain(to)}/contact",
                consent_captured_at=datetime.now(UTC),
                consent_role="developer",
            )
        )
        key = f"initial:{lead.id}:{mode or settings.email_mode}"
        session.add(
            EmailSend(
                idempotency_key=key,
                lead_id=lead.id,
                mode=mode or settings.email_mode,
                intended_to=to,
                subject=rendered.subject,
                rendered_text=rendered.text,
                content_sha256=rendered.sha256,
                unsubscribe_token=token,
                status=status,
            )
        )
        await session.commit()
        return key


async def run(deps, now=THURSDAY_10, *, expect_error: str | None = None) -> RunHandle:
    handle = RunHandle(0, "email_send")
    if expect_error:
        with pytest.raises(RuntimeError, match=expect_error):
            await email_send_job(deps, handle, now=now)
    else:
        await email_send_job(deps, handle, now=now)
    return handle


async def row(sm, key) -> EmailSend:
    async with sm() as session:
        return await session.get(EmailSend, key, populate_existing=True)


async def lead_status(sm, lead_id) -> str:
    async with sm() as session:
        return await session.scalar(select(Lead.status).where(Lead.id == lead_id))


# --- the happy path ------------------------------------------------------------


async def test_sends_one_email_records_it_and_waits_before_the_next(env):
    deps, google, sm = env
    first = await queue(sm, deps.settings, "a")
    second = await queue(sm, deps.settings, "b", to="autre@groupehd.example")

    handle = await run(deps)
    assert handle.counts == {"sent": 1}
    assert len(google.sends) == 1  # one per run
    sent = await row(sm, first)
    assert (sent.status, sent.gmail_message_id, sent.gmail_thread_id, sent.attempts) == (
        "sent",
        "msg1",
        "thr1",
        1,
    )
    assert sent.actual_to == TO and sent.sent_at is not None

    message = google.message()
    assert message["To"] == TO and message["Message-ID"] == sent.rfc_message_id
    assert message["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert message["List-Unsubscribe"] == f"<https://bot.solecra.example/u/{sent.unsubscribe_token}>"
    assert "Carl Abi Yaghi" in message["From"] and "solecra.test@gmail.com" in message["From"]
    assert message.get_content().startswith("Bonjour Marie Tremblay,")

    async with sm() as session:
        touch = (await session.execute(select(Touch))).scalar_one()
        assert (touch.direction, touch.type, touch.status, touch.gmail_thread_id) == (
            "out",
            "initial",
            "sent",
            "thr1",
        )
        assert touch.rfc_message_id == sent.rfc_message_id and touch.email_send_id == first
        next_at = datetime.fromisoformat(
            await session.scalar(select(Setting.value).where(Setting.key == EMAIL_NEXT_SEND_AT))
        )
    assert THURSDAY_10 + timedelta(minutes=15) <= next_at <= THURSDAY_10 + timedelta(minutes=35)
    assert await lead_status(sm, sent.lead_id) == "sent"

    # Too soon: nothing leaves. After the gap: the second one, still one per run.
    assert (await run(deps, THURSDAY_10 + timedelta(minutes=5))).counts == {"waiting_for_gap": 1}
    assert len(google.sends) == 1
    assert (await run(deps, THURSDAY_10 + timedelta(minutes=40))).counts == {"sent": 1}
    assert len(google.sends) == 2 and (await row(sm, second)).status == "sent"
    assert (await run(deps, THURSDAY_10 + timedelta(minutes=80))).counts == {"nothing_queued": 1}


async def test_a_sent_email_is_never_sent_again(env):
    deps, google, sm = env
    key = await queue(sm, deps.settings)
    await run(deps)
    await run(deps, THURSDAY_10 + timedelta(hours=2))
    await run(deps, THURSDAY_10 + timedelta(hours=3))
    assert len(google.sends) == 1 and (await row(sm, key)).status == "sent"


async def test_token_is_reused_between_sends(env):
    deps, google, sm = env
    await queue(sm, deps.settings, "a")
    await queue(sm, deps.settings, "b", to="autre@groupehd.example")
    await run(deps)
    await run(deps, THURSDAY_10 + timedelta(minutes=40))
    assert google.token_calls == 1


# --- when it must not send ----------------------------------------------------


@pytest.mark.parametrize(
    "when",
    [
        datetime(2026, 10, 24, 14, 0, tzinfo=UTC),  # Saturday
        datetime(2026, 10, 22, 12, 59, tzinfo=UTC),  # 08:59
        datetime(2026, 10, 22, 20, 0, tzinfo=UTC),  # 16:00
        datetime(2026, 10, 12, 14, 0, tzinfo=UTC),  # Thanksgiving Monday
    ],
)
async def test_outside_business_hours_nothing_leaves(env, when):
    deps, google, sm = env
    await queue(sm, deps.settings)
    assert (await run(deps, when)).counts == {"outside_hours": 1}
    assert google.sends == []


async def test_test_mode_can_ignore_the_schedule(env):
    deps, google, sm = env
    deps.settings = deps.settings.model_copy(update={"email_test_ignores_schedule": True})
    await queue(sm, deps.settings, "a")
    await queue(sm, deps.settings, "b", to="autre@groupehd.example")
    saturday_night = datetime(2026, 10, 25, 3, 0, tzinfo=UTC)
    await run(deps, saturday_night)
    await run(deps, saturday_night)  # no gap either
    assert len(google.sends) == 2


async def test_paused_and_daily_cap(env):
    deps, google, sm = env
    await queue(sm, deps.settings, "a")
    await queue(sm, deps.settings, "b", to="autre@groupehd.example")
    async with sm() as session:
        await set_setting(session, OUTREACH_PAUSED, True, "test")
        await session.commit()
    assert (await run(deps)).counts == {"paused": 1}
    async with sm() as session:
        await set_setting(session, OUTREACH_PAUSED, False, "test")
        await set_setting(session, OUTREACH_DAILY_CAP, 1, "test")
        await session.commit()
    await run(deps)
    assert (await run(deps, THURSDAY_10 + timedelta(minutes=40))).counts == {"daily_cap_reached": 1}
    assert len(google.sends) == 1
    # The next morning the cap is fresh.
    assert (await run(deps, THURSDAY_10 + timedelta(days=1))).counts == {"sent": 1}


async def test_re_checks_before_sending(env):
    deps, google, sm = env
    unsubscribed = await queue(sm, deps.settings, "a", to="autre@groupehd.example")
    async with sm() as session:  # an unsubscribe landed after approval
        await suppress(session, "autre@groupehd.example", "unsubscribe")
        await session.commit()
    closed = await queue(sm, deps.settings, "b", lead_status="do_not_contact")
    assert (await run(deps)).counts == {"cancelled": 2}
    assert google.sends == []
    assert (await row(sm, unsubscribed)).status == "cancelled" and (
        await row(sm, closed)
    ).status == "cancelled"


async def test_domain_suppression_blocks_too(env):
    deps, google, sm = env
    key = await queue(sm, deps.settings)
    async with sm() as session:
        await suppress(session, "groupehd.example", "unsubscribe")
        await session.commit()
    await run(deps)
    assert google.sends == [] and (await row(sm, key)).status == "cancelled"


async def test_missing_consent_evidence_blocks(env):
    deps, google, sm = env
    key = await queue(sm, deps.settings)
    async with sm() as session:
        await session.execute(update(Lead).values(consent_source_url=None))
        await session.commit()
    await run(deps)
    assert google.sends == [] and (await row(sm, key)).status == "cancelled"


async def test_text_changed_after_approval_is_never_sent(env):
    deps, google, sm = env
    key = await queue(sm, deps.settings)
    async with sm() as session:
        await session.execute(
            update(EmailSend).values(rendered_text=EmailSend.rendered_text + "\nProfitez de 50 % de rabais.")
        )
        await session.commit()
    await run(deps)
    assert google.sends == [] and (await row(sm, key)).status == "failed"


async def test_missing_unsubscribe_link_is_never_sent(env):
    deps, google, sm = env
    key = await queue(sm, deps.settings)
    async with sm() as session:
        sent = await session.get(EmailSend, key)
        stripped = re.sub(r"https?://\S+/u/\S+", "", sent.rendered_text)
        sent.rendered_text = stripped
        from app.outreach.render import content_hash

        sent.content_sha256 = content_hash(sent.subject, stripped)
        await session.commit()
    await run(deps)
    assert google.sends == [] and (await row(sm, key)).status == "failed"


# --- test mode and live mode -----------------------------------------------------


async def test_test_mode_refuses_a_recipient_off_the_list_even_if_queued(env):
    deps, google, sm = env
    key = await queue(sm, deps.settings, to="prospect@vraie-entreprise.example")
    await run(deps, expect_error="not on the test list|test list")
    assert google.sends == []
    assert (await row(sm, key)).status == "held"


async def test_test_and_live_rows_do_not_mix(env):
    deps, google, sm = env
    await queue(sm, deps.settings, mode="live")  # leftover of another mode
    assert (await run(deps)).counts == {"nothing_queued": 1}
    assert google.sends == []


async def test_live_mode_needs_every_condition(env):
    deps, google, sm = env
    deps.settings = deps.settings.model_copy(update={"email_mode": "live", "email_test_recipients": ""})
    key = await queue(sm, deps.settings)
    await run(deps, expect_error="sign-off")
    assert google.sends == [] and (await row(sm, key)).status == "queued"

    async with sm() as session:
        await set_setting(session, OUTREACH_LIVE, {"signed_off_by": "Jeff", "at": "2026-11-01"}, "test")
        await session.commit()
    deps.settings = deps.settings.model_copy(update={"public_base_url": "https://abc-123.trycloudflare.com"})
    await run(deps, expect_error="permanent https")
    assert google.sends == []

    deps.settings = deps.settings.model_copy(update={"public_base_url": "https://bot.solecra.example"})
    assert (await run(deps)).counts == {"sent": 1}


async def test_live_mode_refuses_if_the_link_uses_an_old_address(env):
    deps, google, sm = env
    deps.settings = deps.settings.model_copy(update={"email_mode": "live"})
    key = await queue(sm, deps.settings)
    async with sm() as session:
        await set_setting(session, OUTREACH_LIVE, {"signed_off_by": "Jeff"}, "test")
        await session.commit()
    deps.settings = deps.settings.model_copy(update={"public_base_url": "https://bot.nouveau.example"})
    await run(deps)
    assert google.sends == [] and (await row(sm, key)).status == "failed"


async def test_gmail_not_connected(env):
    deps, google, sm = env
    deps.settings = deps.settings.model_copy(update={"gmail_refresh_token": ""})
    deps.gmail = GmailClient(deps.settings, http=deps.gmail.http)
    key = await queue(sm, deps.settings)
    await run(deps, expect_error="not connected")
    assert (await row(sm, key)).status == "queued"


# --- how the call can fail -------------------------------------------------------


def error(status: int, reason: str = "", text: str | None = None) -> httpx.Response:
    body = {"error": {"code": status, "errors": [{"reason": reason}]}}
    return httpx.Response(status, json=body) if text is None else httpx.Response(status, text=text)


async def test_certain_failures_go_back_in_the_queue_then_give_up(env):
    deps, google, sm = env
    key = await queue(sm, deps.settings)
    google.script = [
        httpx.ConnectError("down"),
        error(429, "rateLimitExceeded"),
        httpx.ConnectTimeout("slow"),
    ]
    await run(deps)
    assert (await row(sm, key)).status == "queued" and (await row(sm, key)).attempts == 1
    await run(deps)
    assert (await row(sm, key)).status == "queued" and (await row(sm, key)).attempts == 2
    await run(deps, expect_error="could not be sent after 3 tries")
    final = await row(sm, key)
    assert (final.status, final.attempts) == ("failed", 3)
    assert len(google.sends) == 3  # never a fourth


@pytest.mark.parametrize(
    "failure", [httpx.ReadTimeout("no answer"), error(500), error(503), httpx.RemoteProtocolError("cut")]
)
async def test_doubtful_failures_are_never_retried(env, failure):
    deps, google, sm = env
    key = await queue(sm, deps.settings)
    google.script = [failure]
    await run(deps, expect_error="may or may not have been sent")
    assert (await row(sm, key)).status == "unknown"
    assert await lead_status(sm, (await row(sm, key)).lead_id) == "approved"  # not marked sent
    await run(deps, THURSDAY_10 + timedelta(hours=2))
    await run(deps, THURSDAY_10 + timedelta(hours=3))
    assert len(google.sends) == 1  # the same email is never offered to Gmail again


async def test_owners_are_told_what_to_do_when_an_email_may_have_left(env):
    """Review of #54: the generic 'will try again' alert is wrong for a doubtful send."""
    from app.owners import sync_owners
    from tests.helpers import OWNER_1

    deps, google, sm = env
    await sync_owners(sm, deps.settings.owners())
    async with sm() as session:  # Alice wrote recently (window open); Bob didn't
        from app.db.models import Owner

        await session.execute(
            update(Owner).where(Owner.phone == OWNER_1).values(last_inbound_at=datetime.now(UTC))
        )
        await session.commit()
    reported = []

    class Issues:
        async def report_failure(self, **kwargs):
            reported.append(kwargs)

    deps.issues = Issues()
    key = await queue(sm, deps.settings)
    google.script = [error(503)]
    await run(deps, expect_error="may or may not")
    texts = [m["body"] for m in deps.channel.sent if m["to"] == OWNER_1]
    assert len(texts) == 1 and "may or may not have left" in texts[0] and "Sent folder" in texts[0]
    assert "@" not in texts[0] and key not in texts[0]
    assert len(reported) == 1 and key in reported[0]["error"] and "@" not in reported[0]["error"]

    # A crash leaves the same kind of doubt: same message.
    key2 = await queue(sm, deps.settings, "b", to="autre@groupehd.example")
    async with sm() as session:
        await session.execute(
            update(EmailSend).where(EmailSend.idempotency_key == key2).values(status="sending")
        )
        await session.execute(
            update(EmailSend)
            .where(EmailSend.idempotency_key == key2)
            .values(updated_at=datetime.now(UTC) - timedelta(minutes=11))
        )
        await session.commit()
    await run(deps, THURSDAY_10 + timedelta(hours=1), expect_error="interrupted")
    assert len([m for m in deps.channel.sent if m["to"] == OWNER_1]) == 2 and len(reported) == 2


async def test_an_email_that_cannot_be_built_is_refused_not_left_sending(env):
    deps, google, sm = env
    key = await queue(sm, deps.settings)
    async with sm() as session:
        from app.outreach.render import content_hash

        sent = await session.get(EmailSend, key)
        sent.subject = "Bonjour\r\nBcc: x@example.com"
        sent.content_sha256 = content_hash(sent.subject, sent.rendered_text)
        await session.commit()
    await run(deps, expect_error="refused an email")
    assert (await row(sm, key)).status == "failed" and google.sends == []


async def test_gmail_refusing_the_email_is_final(env):
    deps, google, sm = env
    key = await queue(sm, deps.settings)
    google.script = [error(400, "invalidArgument")]
    await run(deps, expect_error="refused an email")
    assert (await row(sm, key)).status == "failed"
    await run(deps, THURSDAY_10 + timedelta(hours=2))
    assert len(google.sends) == 1


async def test_expired_sign_in_stops_everything_and_keeps_the_email(env):
    deps, google, sm = env
    key = await queue(sm, deps.settings)

    def expired(request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(400, json={"error": "invalid_grant"})
        raise AssertionError("must not call Gmail without a token")

    deps.gmail = GmailClient(deps.settings, http=httpx.AsyncClient(transport=httpx.MockTransport(expired)))
    await run(deps, expect_error="needs to be reconnected")
    after = await row(sm, key)
    assert (after.status, after.attempts) == ("queued", 0)  # not used up by a sign-in problem


async def test_access_token_expiring_midway_is_refreshed_once(env):
    deps, google, sm = env
    key = await queue(sm, deps.settings)
    google.script = [error(401, "authError")]
    await run(deps)
    assert (await row(sm, key)).status == "sent"
    assert google.token_calls == 2 and len(google.sends) == 2


async def test_crash_between_the_send_and_the_record_never_sends_twice(env, monkeypatch):
    """The process dies after Gmail accepted the email but before we wrote it down."""
    deps, google, sm = env
    key = await queue(sm, deps.settings)
    from app.jobs import email_send

    async def die(*args, **kwargs):
        raise KeyboardInterrupt("process killed")

    monkeypatch.setattr(email_send, "record_sent", die)
    with pytest.raises(KeyboardInterrupt):
        await run(deps)
    monkeypatch.undo()
    assert (await row(sm, key)).status == "sending" and len(google.sends) == 1

    await run(deps, THURSDAY_10 + timedelta(minutes=3))  # too early to decide
    assert (await row(sm, key)).status == "sending"
    async with sm() as session:  # 11 minutes later
        await session.execute(update(EmailSend).values(updated_at=datetime.now(UTC) - timedelta(minutes=11)))
        await session.commit()
    await run(deps, THURSDAY_10 + timedelta(minutes=30), expect_error="interrupted")
    assert (await row(sm, key)).status == "unknown"
    await run(deps, THURSDAY_10 + timedelta(hours=3))
    assert len(google.sends) == 1


async def test_unsubscribing_while_queued_cancels_it(env):
    deps, google, sm = env
    from app.unsubscribe import apply_unsubscribe

    key = await queue(sm, deps.settings)
    async with sm() as session:
        result = await apply_unsubscribe(session, await session.get(EmailSend, key))
        await session.commit()
    assert result.emails_cancelled == 1
    assert (await run(deps)).counts == {"nothing_queued": 1} and google.sends == []


# --- structure ---------------------------------------------------------------


def test_only_the_gmail_module_can_send_email():
    root = Path(__file__).parent.parent
    offenders = [
        str(path.relative_to(root))
        for folder in ("app", "scripts")
        for path in (root / folder).rglob("*.py")
        if path.name != "gmail.py" and ("messages/send" in path.read_text() or "smtplib" in path.read_text())
    ]
    assert offenders == []


def test_job_is_registered_and_scheduled():
    assert "email_send" in JOBS
    assert dict(SCHEDULE)["email_send"] == "*/5 9-15 * * 1-5"


async def test_the_api_has_no_gmail_credentials_in_its_deps(running_app):
    async with running_app() as (app, _client):
        assert app.state.deps.gmail is None
