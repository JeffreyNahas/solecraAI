"""Approval records. First action wins:
UPDATE approvals SET ... WHERE id=:id AND status=:expected AND version=:v
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Approval

FINAL_STATUSES = ("approved", "edited", "rejected")


async def get_or_create_pending(
    session: AsyncSession, run_id: str, item_type: str, item_id: str | None, payload: dict[str, Any]
) -> Approval:
    """Reuse the run's open approval if a node re-runs (crash between steps)."""
    existing = (
        await session.execute(
            select(Approval).where(
                Approval.run_id == run_id,
                Approval.item_type == item_type,
                Approval.status.in_(("pending", "editing")),
            )
        )
    ).scalar_one_or_none()
    if existing:
        return existing
    approval = Approval(
        run_id=run_id, item_type=item_type, item_id=item_id, status="pending", version=1, payload=payload
    )
    session.add(approval)
    await session.flush()
    return approval


async def transition(
    session: AsyncSession,
    approval_id: int,
    *,
    expected_status: str,
    version: int,
    new_status: str,
    owner_phone: str,
    decision: dict[str, Any] | None = None,
) -> Approval | None:
    """Atomically move an approval to a new status. Returns None if someone else won."""
    values: dict[str, Any] = {
        "status": new_status,
        "version": Approval.version + 1,
        "decided_by": owner_phone,
        "decided_at": datetime.now(UTC),
    }
    if decision is not None:
        values["payload"] = Approval.payload.op("||")({"decision": decision})
    result = await session.execute(
        update(Approval)
        .where(
            Approval.id == approval_id,
            Approval.status == expected_status,
            Approval.version == version,
        )
        .values(**values)
        .returning(Approval)
        .execution_options(synchronize_session=False)
    )
    return result.scalar_one_or_none()


async def editing_for(session: AsyncSession, owner_phone: str) -> Approval | None:
    """The approval this owner tapped Edit on and still owes replacement text for."""
    result = await session.execute(
        select(Approval)
        .where(Approval.status == "editing", Approval.decided_by == owner_phone)
        .order_by(Approval.decided_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def unresumed_decisions(session: AsyncSession) -> list[Approval]:
    result = await session.execute(
        select(Approval).where(Approval.status.in_(FINAL_STATUSES), Approval.resumed_at.is_(None))
    )
    return list(result.scalars())


async def mark_resumed(session: AsyncSession, approval_id: int) -> None:
    await session.execute(
        update(Approval).where(Approval.id == approval_id).values(resumed_at=datetime.now(UTC))
    )
