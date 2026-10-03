"""Agent card: strict, fail-closed config. An agent is a folder; this is its contract."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

if TYPE_CHECKING:
    from agent_kit.registry import ToolRegistry


class CardError(ValueError):
    pass


class _Strict(BaseModel):
    # Unknown keys are almost always typos of a safety setting; reject rather than ignore.
    model_config = ConfigDict(extra="forbid")


class GuardSpec(_Strict):
    tool: str
    item_key: str  # payload field that identifies the item being written


class Budget(_Strict):
    max_steps: StrictInt = Field(25, gt=0)
    max_saves: StrictInt = Field(5, ge=0)
    max_tool_calls: StrictInt = Field(60, gt=0)  # every call the model makes, handle queries included
    max_backend_calls: StrictInt = Field(40, gt=0)  # only calls that reach the backend (the MCP-call budget)
    deadline_s: float = Field(120, gt=0, allow_inf_nan=False)  # inf would disable the deadline


class AgentCard(_Strict):
    name: str
    description: str
    skill: str
    tools: list[str]
    guarded_writes: list[GuardSpec] = Field(default_factory=list)
    confirm_actions: list[str] = Field(default_factory=list)
    budget: Budget = Field(default_factory=Budget)

    @model_validator(mode="after")
    def _guards_use_allowed_tools(self) -> AgentCard:
        guarded = {g.tool for g in self.guarded_writes}
        confirm = set(self.confirm_actions)
        for tool in guarded | confirm:
            if tool not in self.tools:
                raise CardError(f"{tool!r} is guarded/confirm-gated but not in tools")
        if guarded & confirm:
            raise CardError(f"tool cannot be both guarded and confirm-gated: {guarded & confirm}")
        return self

    def validate_against(self, registry: ToolRegistry) -> None:
        """Every write tool the card can reach must be guarded or confirm-gated."""
        protected = {g.tool for g in self.guarded_writes} | set(self.confirm_actions)
        for tool in self.tools:
            if tool in registry and registry.is_write(tool) and tool not in protected:
                raise CardError(f"write tool {tool!r} is neither guarded nor confirm-gated")
