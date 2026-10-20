"""Approval button ids: "<action>:<approval_id>:<version>"."""

from typing import Literal

from pydantic import BaseModel

Action = Literal["approve", "edit", "reject"]
ACTIONS: tuple[Action, ...] = ("approve", "edit", "reject")


class ButtonId(BaseModel):
    action: Action
    approval_id: int
    version: int

    def encode(self) -> str:
        return f"{self.action}:{self.approval_id}:{self.version}"


def parse_button_id(raw: str | None) -> ButtonId | None:
    if not raw:
        return None
    parts = raw.split(":")
    if len(parts) != 3 or parts[0] not in ACTIONS:
        return None
    try:
        return ButtonId(action=parts[0], approval_id=int(parts[1]), version=int(parts[2]))
    except ValueError:
        return None


def approval_buttons(approval_id: int, version: int) -> list[tuple[str, str]]:
    """(id, title) pairs. WhatsApp allows max 3 buttons, titles max 20 chars."""
    return [
        (ButtonId(action="approve", approval_id=approval_id, version=version).encode(), "Approve"),
        (ButtonId(action="edit", approval_id=approval_id, version=version).encode(), "Edit"),
        (ButtonId(action="reject", approval_id=approval_id, version=version).encode(), "Reject"),
    ]
