import pytest
from sqlalchemy import select

from app.db.models import AuditLog, Lead
from app.db.session import make_engine, make_sessionmaker
from app.leads import (
    LeadIn,
    TransitionError,
    allowed_transitions,
    email_domain,
    insert_lead,
    normalize_address,
    normalize_phone,
    transition,
)
from app.suppression import is_suppressed, suppress


def test_email_domain_ignores_free_mail():
    assert email_domain("Marie@DesignStudio.ca") == "designstudio.ca"
    assert email_domain("someone@gmail.com") is None
    assert email_domain("not-an-email") is None
    assert email_domain(None) is None


def test_normalize_phone():
    assert normalize_phone("(514) 555-0101") == "15145550101"
    assert normalize_phone("+1 514.555.0101") == "15145550101"
    assert normalize_phone("555-0101") is None


def test_normalize_address_matches_fr_en_variants():
    a = normalize_address("1234, Boulevard Saint-Laurent Ouest, Montréal QC H2X 1Y4")
    b = normalize_address("1234 boul. Saint Laurent O. Montreal, QC h2x1y4")
    assert a == b
    assert "h2x1y4" in a
    assert normalize_address("  ") is None


def test_allowed_transitions():
    assert allowed_transitions("new") == {"researched", "disqualified", "no_valid_contact", "do_not_contact"}
    assert "do_not_contact" in allowed_transitions("won")
    assert "disqualified" not in allowed_transitions("won")
    assert "sent" not in allowed_transitions("new")


@pytest.fixture
async def sm(settings):
    engine = make_engine(settings)
    yield make_sessionmaker(engine)
    await engine.dispose()


async def test_insert_dedupes_on_source_ref_domain_phone_address(sm):
    async with sm() as s:
        first, created = await insert_lead(
            s,
            LeadIn(
                source="permit",
                source_ref="P-1",
                email="info@acme.ca",
                phone="514 555 0101",
                address="10 rue Ontario Est",
            ),
        )
        assert created
        same_ref, c1 = await insert_lead(s, LeadIn(source="permit", source_ref="P-1"))
        same_domain, c2 = await insert_lead(s, LeadIn(source="news", email="sales@ACME.ca"))
        same_phone, c3 = await insert_lead(s, LeadIn(source="news", phone="+1 (514) 555-0101"))
        same_addr, c4 = await insert_lead(s, LeadIn(source="news", address="10 Ontario Street East"))
        other, c5 = await insert_lead(s, LeadIn(source="news", email="a@gmail.com"))
        free_mail, c6 = await insert_lead(s, LeadIn(source="news", email="b@gmail.com"))
        await s.commit()
    assert not any([c1, c2, c3, c4]) and {same_ref.id, same_domain.id, same_phone.id} == {first.id}
    assert same_addr.id == first.id
    assert c5 and c6 and other.id != free_mail.id  # free-mail domain never merges people


async def test_transition_checks_rules_and_version(sm):
    async with sm() as s:
        lead, _ = await insert_lead(s, LeadIn(source="manual", source_ref="x"))
        await s.commit()
        lead = await transition(s, lead.id, "researched", expected_version=1, actor="system")
        assert (lead.status, lead.version) == ("researched", 2)
        with pytest.raises(TransitionError, match="not allowed"):
            await transition(s, lead.id, "sent", expected_version=2, actor="system")
        with pytest.raises(TransitionError, match="changed by someone else"):
            await transition(s, lead.id, "qualified", expected_version=1, actor="system")
        await s.commit()
        actions = (await s.execute(select(AuditLog.action))).scalars().all()
    assert actions.count("lead_status") == 1 and "lead_created" in actions


async def test_suppression_by_address_and_domain(sm):
    async with sm() as s:
        await suppress(s, "Stop@Example.com", "unsubscribe")
        await suppress(s, "stop@example.com", "unsubscribe")  # idempotent
        await suppress(s, "blocked.ca", "do_not_contact")
        await s.commit()
        assert await is_suppressed(s, "stop@example.com")
        assert not await is_suppressed(s, "other@example.com")
        assert await is_suppressed(s, "anyone@blocked.ca")
        assert (await s.execute(select(Lead))).first() is None
