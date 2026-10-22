"""Create one qualified TEST lead so the whole outreach path can be tried end to end (#49).

    uv run python -m scripts.seed_test_lead --email you+test1@gmail.com

Then: `python -m scripts.run_job outreach_draft` (draft on WhatsApp) → tap Approve →
`python -m scripts.run_job email_send` (set EMAIL_TEST_IGNORES_SCHEDULE=true to send outside 09:00–16:00).

Refuses in live mode, and refuses any address that is not in EMAIL_TEST_RECIPIENTS. A company
already emailed isn't emailed again, so use a new `+alias` address for each trial.
"""

import argparse
import asyncio
import sys
from datetime import UTC, datetime

from app.config import get_settings
from app.db.audit import audit
from app.db.models import Lead
from app.db.session import make_engine, make_sessionmaker
from app.leads import email_domain


async def main(email: str | None) -> int:
    settings = get_settings()
    if settings.email_mode != "test":
        print("Refusing: EMAIL_MODE is not 'test'. Test leads are never created in live mode.")
        return 1
    allowed = settings.test_recipients()
    address = (email or (sorted(allowed)[0] if allowed else "")).strip().lower()
    if not address or address not in allowed:
        print(f"Refusing: {address or 'no address'} is not in EMAIL_TEST_RECIPIENTS. Add it there first.")
        return 1

    engine = make_engine(settings)
    sessionmaker = make_sessionmaker(engine)
    now = datetime.now(UTC)
    async with sessionmaker() as session:
        lead = Lead(
            source="manual",
            source_ref=f"test-seed-{now:%Y%m%d%H%M%S}",
            business_name="Entreprise Test (essai interne)",
            contact_name="Essai Interne",
            role="developer",
            email=address,
            email_domain=email_domain(address),
            address="1 rue Essai, Montréal",
            website=settings.company_website or None,
            status="qualified",
            score=99,
            language="fr",
            consent_source_url=settings.company_website or "https://example.com/contact",
            consent_captured_at=now,
            consent_role="developer",
            relevance_note="Internal test lead: not a real prospect.",
            source_details={"work": "new_construction", "building": "multi_residential", "units": 40},
        )
        session.add(lead)
        await session.flush()
        audit(session, "seed_test_lead", "test_lead_created", "lead", lead.id)
        await session.commit()
        print(f"Created test lead {lead.id} (qualified). Next: python -m scripts.run_job outreach_draft")
    await engine.dispose()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--email", help="a test address; default: the first of EMAIL_TEST_RECIPIENTS")
    sys.exit(asyncio.run(main(parser.parse_args().email)))
