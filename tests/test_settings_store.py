from sqlalchemy import select

from app import settings_store as store
from app.db.models import AuditLog
from app.db.session import make_engine, make_sessionmaker


async def test_settings_round_trip_and_audit(settings):
    engine = make_engine(settings)
    sm = make_sessionmaker(engine)
    try:
        async with sm() as session:
            assert await store.outreach_paused(session) is False
            assert await store.outreach_daily_cap(session, default=15) == 15
            assert await store.email_next_send_at(session) is None

            await store.set_setting(session, store.OUTREACH_PAUSED, True, "15145550001")
            await store.set_setting(session, store.OUTREACH_DAILY_CAP, 10, "15145550001")
            await store.set_setting(session, store.OUTREACH_DAILY_CAP, 12, "15145550002")  # replaces
            await store.set_setting(session, store.EMAIL_NEXT_SEND_AT, "2026-10-23T13:20:00+00:00", "system")
            await session.commit()

        async with sm() as session:
            assert await store.outreach_paused(session) is True
            assert await store.outreach_daily_cap(session, default=15) == 12
            assert (await store.email_next_send_at(session)).isoformat() == "2026-10-23T13:20:00+00:00"
            changes = (
                await session.execute(select(AuditLog.entity_id).where(AuditLog.action == "setting_changed"))
            ).scalars()
            assert sorted(changes) == sorted(
                ["outreach_paused", "outreach_daily_cap", "outreach_daily_cap", "email_next_send_at"]
            )
    finally:
        await engine.dispose()


async def test_bad_values_fall_back_safely(settings):
    engine = make_engine(settings)
    sm = make_sessionmaker(engine)
    try:
        async with sm() as session:
            await store.set_setting(session, store.OUTREACH_DAILY_CAP, "lots", "system")
            await store.set_setting(session, store.EMAIL_NEXT_SEND_AT, "not a date", "system")
            await session.commit()
            assert await store.outreach_daily_cap(session, default=15) == 15
            assert await store.email_next_send_at(session) is None
            await store.set_setting(session, store.OUTREACH_DAILY_CAP, -3, "system")
            assert await store.outreach_daily_cap(session, default=15) == 0
    finally:
        await engine.dispose()
