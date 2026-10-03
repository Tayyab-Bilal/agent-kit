"""Run as the chatting user: the card's allow-list is opened once, as that user.

Why: if the agent used one shared service identity, the backend could not enforce the user's own
permissions and a prompt trick could read someone else's data. Opening the tools as the user means
the backend, not this kit, says what is allowed. Anything missing or forbidden refuses the run.
"""

from __future__ import annotations

from typing import Protocol

from agent_kit.card import AgentCard
from agent_kit.registry import ToolRegistry


class OpenRefused(Exception):
    """A tool the card needs is missing or the user may not use it. The run must not start."""


class ToolOpener(Protocol):
    async def open(self, card: AgentCard, user: str) -> ToolRegistry:
        """Return a registry holding exactly `card.tools`, bound to `user`. Raise OpenRefused
        (or any exception) if that is not possible."""
        ...
