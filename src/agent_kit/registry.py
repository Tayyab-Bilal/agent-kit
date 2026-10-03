"""Tool registry and agent discovery. A broken card excludes only that agent (fail closed)."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from agent_kit.card import AgentCard, CardError

BUILTIN_AGENTS = Path(__file__).parent / "agents"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    fn: Callable[..., Awaitable[Any]]
    writes: bool
    description: str


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(
        self, name: str, fn: Callable[..., Awaitable[Any]], *, writes: bool = False,
        description: str = "",
    ) -> None:
        self._tools[name] = ToolSpec(name, fn, writes, description)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def spec(self, name: str) -> ToolSpec:
        return self._tools[name]

    def put(self, spec: ToolSpec) -> None:
        """Add or replace a tool from a ready-made spec (used to swap in a guarded version)."""
        self._tools[spec.name] = spec

    def copy(self) -> ToolRegistry:
        other = ToolRegistry()
        other._tools = dict(self._tools)
        return other

    def subset(self, names: Iterable[str]) -> ToolRegistry:
        """Only these tools. Anything not named is physically absent, not just hidden."""
        other = ToolRegistry()
        other._tools = {n: self._tools[n] for n in names}
        return other

    def is_write(self, name: str) -> bool:
        return self._tools[name].writes

    def check_args(self, name: str, args: dict[str, Any]) -> None:
        """Raises TypeError if args don't fit the tool's signature (checked before anything is stored)."""
        inspect.signature(self._tools[name].fn).bind(**args)

    def schema(self, name: str) -> dict[str, Any]:
        spec = self._tools[name]
        params = inspect.signature(spec.fn).parameters
        args = {
            p: {"type": getattr(a.annotation, "__name__", str(a.annotation)),
                "required": a.default is inspect.Parameter.empty}
            for p, a in params.items()
        }
        return {"name": name, "description": spec.description, "args": args}

    async def call(self, name: str, args: dict[str, Any]) -> Any:
        return await self._tools[name].fn(**args)


@dataclass(frozen=True)
class LoadedAgent:
    card: AgentCard
    skill_text: str


@dataclass
class AgentRegistry:
    agents: dict[str, LoadedAgent] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)  # folder name -> why it was excluded

    def names(self) -> list[str]:
        return sorted(self.agents)

    def enabled_names(self, flags: bool | Mapping[str, bool]) -> list[str]:
        """Agents the orchestrator may see. A bool is a master switch; a mapping is one flag per
        agent, and an agent with no flag is off (new agents ship dark)."""
        if isinstance(flags, bool):
            return self.names() if flags else []
        return [n for n in self.names() if flags.get(n, False)]


def load_agent(folder: Path, tools: ToolRegistry) -> LoadedAgent:
    data = yaml.safe_load((folder / "agent.yaml").read_text())
    if not isinstance(data, dict):
        raise CardError("agent.yaml must be a mapping")
    card = AgentCard.model_validate(data)
    if card.name != folder.name:
        raise CardError(f"card name {card.name!r} must match folder {folder.name!r}")
    card.validate_against(tools)
    missing = [t for t in card.tools if t not in tools]
    if missing:  # never offer an agent that can only fail at run time
        raise CardError(f"tools not registered: {missing}")
    skill = (folder / card.skill).resolve()
    if not skill.is_relative_to(folder.resolve()):
        raise CardError("skill path must stay inside the agent folder")
    return LoadedAgent(card, skill.read_text())


def discover(root: Path, tools: ToolRegistry) -> AgentRegistry:
    reg = AgentRegistry()
    for card_file in sorted(root.glob("*/agent.yaml")):
        folder = card_file.parent
        try:
            reg.agents[folder.name] = load_agent(folder, tools)
        except Exception as exc:  # any load problem excludes this agent, never the others
            reg.errors[folder.name] = f"{type(exc).__name__}: {exc}"
    return reg
