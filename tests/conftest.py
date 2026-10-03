from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from agent_kit import runner
from agent_kit.agents.notes_curator.tools import NotesBackend, build_tools
from agent_kit.card import AgentCard
from agent_kit.confirm import ConfirmGate
from agent_kit.handles import HandleStore
from agent_kit.llm import Final, ToolCall
from agent_kit.registry import BUILTIN_AGENTS, LoadedAgent, ToolRegistry, discover
from agent_kit.result import TaskResult
from agent_kit.write_guard import WriteGuard


@dataclass
class Env:
    backend: NotesBackend
    tools: ToolRegistry
    loaded: LoadedAgent
    guard: WriteGuard
    gate: ConfirmGate
    handles: HandleStore

    @property
    def card(self) -> AgentCard:
        return self.loaded.card

    async def run(self, llm: Any, card: AgentCard | None = None, task_id: str = "t1",
                  **kw: Any) -> TaskResult:
        return await runner.run(
            card or self.card, runner.Task(task_id, "tidy my notes"), llm, self.tools,
            self.handles, self.guard, self.gate, skill_text=self.loaded.skill_text,
            version_of=self.backend.version_of, **kw,
        )


@pytest.fixture
def env() -> Env:
    backend = NotesBackend()
    tools = build_tools(backend)
    loaded = discover(BUILTIN_AGENTS, tools).agents["notes_curator"]
    guard = WriteGuard(tools, lambda tool, key: backend.note_exists(key), loaded.card.budget)
    return Env(backend, tools, loaded, guard, ConfirmGate(), HandleStore())


def tool_observations(llm: Any) -> list[str]:
    """Everything the model was shown from tools, taken from the last transcript it saw."""
    return [m["content"] for m in llm.seen[-1] if m["role"] == "tool"]


def calls(*pairs: tuple[str, dict[str, Any]]) -> list[ToolCall]:
    return [ToolCall(n, a) for n, a in pairs]


DONE = Final("done")
