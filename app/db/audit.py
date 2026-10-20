from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog


def audit(
    session: AsyncSession,
    actor: str,
    action: str,
    entity: str | None = None,
    entity_id: str | int | None = None,
    **details: Any,
) -> None:
    """Add an audit row to the session (caller commits)."""
    session.add(
        AuditLog(
            actor=actor,
            action=action,
            entity=entity,
            entity_id=None if entity_id is None else str(entity_id),
            details=details,
        )
    )
