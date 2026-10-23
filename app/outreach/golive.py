"""What must be true before the system sends email to real prospects (CLAUDE.md §9 steps 7 and 15).

`live_problems` lists what is still missing; the sender refuses to run in live mode while the
list is not empty. Test mode needs none of this. The sign-off row (`settings.outreach_live`) is
written by a go-live script after the compliance review (#13), never by the model or a chat command.
"""

from urllib.parse import urlsplit

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.outreach.render import Identity, IdentityIncomplete
from app.settings_store import OUTREACH_LIVE, get_setting

TEMPORARY_HOSTS = ("trycloudflare.com", "ngrok", "localhost", "127.0.0.1", "loca.lt")


async def live_problems(session: AsyncSession, settings: Settings) -> list[str]:
    problems: list[str] = []
    if settings.email_mode != "live":
        problems.append("EMAIL_MODE is not 'live'")
    if not await get_setting(session, OUTREACH_LIVE):
        problems.append("the compliance sign-off (step 15) has not been recorded")
    try:
        Identity.from_settings(settings)
    except IdentityIncomplete as exc:
        problems.append(str(exc))
    parts = urlsplit(settings.public_base_url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host or any(t in host for t in TEMPORARY_HOSTS):
        problems.append(
            "PUBLIC_BASE_URL must be the permanent https address (unsubscribe links must keep working)"
        )
    return problems
