"""Do-not-email list (CASL). Checked before every send, forever; entries are never removed by code."""

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.audit import audit
from app.db.models import Suppression

REASONS = ("unsubscribe", "bounce", "do_not_contact")


def _domain(email: str) -> str:
    return email.strip().lower().rsplit("@", 1)[-1]


async def is_suppressed(session: AsyncSession, email: str) -> bool:
    """True if this exact address or its whole domain is suppressed."""
    address = email.strip().lower()
    result = await session.execute(
        select(Suppression.email_or_domain).where(
            Suppression.email_or_domain.in_([address, _domain(address)])
        )
    )
    return result.first() is not None


async def suppress(session: AsyncSession, email_or_domain: str, reason: str, actor: str = "system") -> None:
    """Add an address (`a@b.com`) or a whole domain (`b.com`). Idempotent."""
    if reason not in REASONS:
        raise ValueError(f"unknown suppression reason {reason!r}")
    value = email_or_domain.strip().lower()
    await session.execute(
        insert(Suppression)
        .values(email_or_domain=value, reason=reason)
        .on_conflict_do_nothing(index_elements=["email_or_domain"])
    )
    # Log only the domain: the audit log should not become a second copy of the address list.
    audit(session, actor, "suppressed", "suppression", _domain(value), reason=reason)
