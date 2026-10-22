"""Approval button ids: "<action>:<approval_id>:<version>".

approve / edit / reject answer an approval. After Edit, items that the AI can revise offer a
second choice: edit_text (the owner types the new text) or edit_ai (the owner says what to change).
"""

from typing import Literal

from pydantic import BaseModel

Action = Literal["approve", "edit", "reject", "edit_text", "edit_ai"]
ACTIONS: tuple[Action, ...] = ("approve", "edit", "reject", "edit_text", "edit_ai")
EDIT_MODES = {"edit_text": "text", "edit_ai": "ai"}


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


def edit_mode_buttons(approval_id: int, version: int) -> list[tuple[str, str]]:
    """Second step of Edit for items the AI can revise: owner's own text, or tell the AI."""
    return [
        (ButtonId(action="edit_text", approval_id=approval_id, version=version).encode(), "My own text"),
        (ButtonId(action="edit_ai", approval_id=approval_id, version=version).encode(), "Tell AI changes"),
    ]
