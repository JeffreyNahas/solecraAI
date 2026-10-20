"""Messaging channel interface, so WhatsApp could be swapped later.

`key` is an idempotency key: sending twice with the same key sends once.
"""

from typing import Protocol


class Channel(Protocol):
    async def send_text(self, to: str, body: str, key: str) -> str | None: ...

    async def send_buttons(
        self, to: str, body: str, buttons: list[tuple[str, str]], key: str
    ) -> str | None: ...


class ChannelError(Exception):
    pass
