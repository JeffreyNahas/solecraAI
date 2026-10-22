"""Database-level guarantees of migration 0005 (one open draft per lead, one send per key)."""

import pytest
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from app.db.models import EmailDraft, EmailSend
from app.db.session import make_engine, make_sessionmaker
from app.leads import LeadIn, insert_lead


@pytest.fixture
async def sm(settings):
    engine = make_engine(settings)
    yield make_sessionmaker(engine)
    await engine.dispose()


async def make_lead(sm) -> int:
    async with sm() as session:
        lead, _ = await insert_lead(session, LeadIn(source="permit", source_ref="P1", address="1 rue A"))
        await session.commit()
        return lead.id


async def test_one_open_initial_draft_per_lead(sm):
    lead_id = await make_lead(sm)
    async with sm() as session:
        session.add(EmailDraft(lead_id=lead_id, lead_ids=[lead_id], status="pending"))
        await session.commit()
    async with sm() as session:
        session.add(EmailDraft(lead_id=lead_id, lead_ids=[lead_id], status="drafting"))
        with pytest.raises(IntegrityError):
            await session.commit()
    async with sm() as session:  # closed drafts don't count: a new one is allowed
        session.add(EmailDraft(lead_id=lead_id, lead_ids=[lead_id], status="rejected"))
        await session.commit()


async def test_send_key_is_queued_once(sm):
    lead_id = await make_lead(sm)
    row = {
        "idempotency_key": f"initial:{lead_id}:test",
        "lead_id": lead_id,
        "mode": "test",
        "intended_to": "info@example.ca",
        "subject": "Objet",
        "rendered_text": "Bonjour",
        "content_sha256": "0" * 64,
    }
    async with sm() as session:
        queued = []
        for subject in ("Objet", "Autre"):  # a retry with the same key
            result = await session.execute(
                insert(EmailSend)
                .values(**{**row, "subject": subject})
                .on_conflict_do_nothing(index_elements=["idempotency_key"])
                .returning(EmailSend.idempotency_key)
            )
            queued.append(result.scalar_one_or_none())
        await session.commit()
        stored = await session.get(EmailSend, row["idempotency_key"])
    assert queued == [row["idempotency_key"], None]
    assert stored.subject == "Objet" and stored.status == "queued" and stored.attempts == 0
