"""Runtime settings the owners or jobs change while the system runs (`settings` table).

Env vars (`app/config.py`) hold deployment config; this table holds what changes at runtime:
pausing outreach, the daily cap, when the next email may go out. Every write is audited.
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.audit import audit
from app.db.models import Setting

OUTREACH_PAUSED = "outreach_paused"  # bool
OUTREACH_DAILY_CAP = "outreach_daily_cap"  # int; falls back to OUTREACH_DAILY_CAP env
EMAIL_NEXT_SEND_AT = "email_next_send_at"  # ISO datetime: no email before this


async def get_setting(session: AsyncSession, key: str, default: Any = None) -> Any:
    value = await session.scalar(select(Setting.value).where(Setting.key == key))
    return default if value is None else value


async def set_setting(session: AsyncSession, key: str, value: Any, updated_by: str) -> None:
    """Insert or replace a setting and audit the change (caller commits)."""
    await session.execute(
        insert(Setting)
        .values(key=key, value=value, updated_by=updated_by)
        .on_conflict_do_update(
            index_elements=["key"],
            set_={"value": value, "updated_by": updated_by, "updated_at": datetime.now().astimezone()},
        )
    )
    audit(session, updated_by, "setting_changed", "setting", key, value=value)


async def outreach_paused(session: AsyncSession) -> bool:
    return bool(await get_setting(session, OUTREACH_PAUSED, False))


async def outreach_daily_cap(session: AsyncSession, default: int) -> int:
    """The owners' cap if set and valid, else the env default. Never negative."""
    value = await get_setting(session, OUTREACH_DAILY_CAP, default)
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


async def email_next_send_at(session: AsyncSession) -> datetime | None:
    value = await get_setting(session, EMAIL_NEXT_SEND_AT)
    if not value:
        return None
    try:
        when = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)  # never naive: compared to now(UTC)
