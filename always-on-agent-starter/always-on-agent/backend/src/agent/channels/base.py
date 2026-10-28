"""Channel interfaces for approval cards (F07, F14). Real Slack arrives in 1.11, Teams in 1.12.

A card shows exactly the content its hash covers. When a decision is made on one surface, every other
surface's card is updated so it can no longer be clicked.
"""

from datetime import datetime
from typing import Protocol

from pydantic import BaseModel


class ApprovalCard(BaseModel):
    action_id: str
    task_id: str | None
    title: str
    content: str  # what the approval covers, shown as is
    content_hash: str
    expires_at: datetime | None
    edited: bool = False
    note: str | None = None
    buttons: tuple[str, ...] = ("Approve", "Edit", "Reject")


class ApprovalChannel(Protocol):
    name: str

    async def post_card(self, card: ApprovalCard) -> str:
        """Post the card; returns a reference used to update it later."""
        ...

    async def update_card(self, ref: str, card: ApprovalCard, status: str, note: str | None) -> None:
        """Show the outcome (approved, rejected, edited, expired, invalidated) and disable the buttons."""
        ...


class MemoryChannel:
    """Keeps cards in memory: the console queue reads from here, and tests use it as the fake channel."""

    def __init__(self, name: str = "console") -> None:
        self.name = name
        self.cards: dict[str, ApprovalCard] = {}
        self.status: dict[str, str] = {}
        self.notes: dict[str, str | None] = {}
        self.fail_posts = 0

    async def post_card(self, card: ApprovalCard) -> str:
        if self.fail_posts:
            self.fail_posts -= 1
            raise ConnectionError(f"{self.name} unavailable")
        ref = f"{self.name}:{len(self.cards) + 1}"
        self.cards[ref] = card
        self.status[ref] = "open"
        return ref

    async def update_card(self, ref: str, card: ApprovalCard, status: str, note: str | None) -> None:
        self.cards[ref] = card
        self.status[ref] = status
        self.notes[ref] = note

    def open_cards(self) -> list[ApprovalCard]:
        return [card for ref, card in self.cards.items() if self.status[ref] == "open"]
