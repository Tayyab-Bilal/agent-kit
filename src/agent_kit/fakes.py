"""Deterministic scripted LLM for tests and examples. No network, no keys."""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from typing import Any

from agent_kit.card import AgentCard
from agent_kit.llm import Final, ToolCall
from agent_kit.opener import OpenRefused
from agent_kit.registry import ToolRegistry

Step = list[ToolCall] | Final | Callable[[list[dict[str, str]]], list[ToolCall] | Final]


class ScriptedLLM:
    """Plays back one scripted step per call; a callable step can read the transcript."""

    def __init__(self, script: list[Step]) -> None:
        self._script = list(script)
        self.seen: list[list[dict[str, str]]] = []
        self.tools_seen: list[list[dict[str, Any]]] = []

    async def step(
        self, transcript: list[dict[str, str]], tools: list[dict[str, Any]] | None = None
    ) -> list[ToolCall] | Final:
        self.seen.append(list(transcript))
        self.tools_seen.append(tools or [])
        if not self._script:
            return Final("script exhausted")
        step = self._script.pop(0)
        return step(transcript) if callable(step) else step


class FakeBackendOpener:
    """A fake backend that enforces per-user permissions, like a real one would.

    `grants` says which tools each user may use; `build(user)` returns that user's tools, bound to
    that user's own data. An unknown user, a forbidden tool or a missing tool refuses the open.
    """

    def __init__(
        self, build: Callable[[str], ToolRegistry], grants: Mapping[str, Collection[str]]
    ) -> None:
        self._build, self._grants = build, grants
        self.opened: list[str] = []  # who each open() was for, so tests can count them

    async def open(self, card: AgentCard, user: str) -> ToolRegistry:
        granted = self._grants.get(user)
        if granted is None:
            raise OpenRefused(f"unknown user {user!r}")
        tools = self._build(user)
        forbidden = [t for t in card.tools if t in tools and t not in granted]
        missing = [t for t in card.tools if t not in tools]
        if forbidden or missing:
            raise OpenRefused(f"forbidden for {user!r}: {forbidden}, missing: {missing}")
        self.opened.append(user)
        return tools.subset(card.tools)
