"""LLM seams. Real providers live behind these Protocols; this repo ships only fakes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ToolCall:
    name: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass
class Final:
    message: str
    # Whatever the model says it produced. The runner ignores it: artifacts come from the ledger.
    asserted_artifacts: list[str] = field(default_factory=list)


class AgentLLM(Protocol):
    async def step(
        self, transcript: list[dict[str, str]], tools: list[dict[str, Any]]
    ) -> list[ToolCall] | Final:
        """`tools` describes what the model may call (name, description, args), so a real
        adapter can build provider tool definitions. Fakes may ignore it."""
        ...


class TextLLM(Protocol):
    async def complete(self, prompt: str, temperature: float = 0.0) -> str: ...
