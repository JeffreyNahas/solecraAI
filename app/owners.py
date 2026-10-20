from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.audit import audit
from app.db.models import Owner


async def sync_owners(sessionmaker: async_sessionmaker, owners: dict[str, str]) -> None:
    """Make the owners table match OWNER_PHONES (env is the source of truth)."""
    async with sessionmaker() as session:
        for phone, name in owners.items():
            await session.execute(
                insert(Owner)
                .values(phone=phone, name=name, role="owner", active=True)
                .on_conflict_do_update(index_elements=["phone"], set_={"name": name, "active": True})
            )
        stmt = update(Owner).values(active=False)
        if owners:
            stmt = stmt.where(Owner.phone.not_in(list(owners)))
        await session.execute(stmt)
        audit(session, "system", "owners_synced", "owners", None, count=len(owners))
        await session.commit()


async def get_owner(session: AsyncSession, phone: str) -> Owner | None:
    owner = await session.get(Owner, phone)
    return owner if owner and owner.active else None


async def active_owners(session: AsyncSession) -> list[Owner]:
    result = await session.execute(select(Owner).where(Owner.active).order_by(Owner.phone))
    return list(result.scalars())
