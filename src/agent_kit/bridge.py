"""Orchestrator bridge: one delegate tool generated from the registry, or nothing at all.

Nothing enabled means None, so the orchestrator's tool list is byte-for-byte what it was before.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Literal

from langchain_core.tools import StructuredTool
from pydantic import Field, create_model

from agent_kit.registry import AgentRegistry


def make_delegate_tool(
    registry: AgentRegistry,
    enabled: bool | Mapping[str, bool],
    dispatch: Callable[[str, str], Awaitable[str]],
) -> StructuredTool | None:
    """`enabled` is a master switch (bool) or one flag per agent name; only enabled agents are
    listed, and if none are, there is no tool at all."""
    names = registry.enabled_names(enabled)
    if not names:
        return None
    catalogue = "\n".join(f"- {n}: {registry.agents[n].card.description}" for n in names)
    args = create_model(
        "DelegateInput",
        agent=(Literal[tuple(names)], Field(description="Which specialist to delegate to")),  # type: ignore[valid-type]
        task=(str, Field(description="What the specialist should do")),
    )

    async def delegate(agent: str, task: str) -> str:
        return await dispatch(agent, task)

    return StructuredTool.from_function(
        coroutine=delegate,
        name="delegate_to_specialist",
        description=f"Delegate a multi-step task to a specialist agent.\n{catalogue}",
        args_schema=args,
    )

