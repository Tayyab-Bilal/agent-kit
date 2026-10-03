"""Wire types. Mirrors A2A concepts (task, needs-input, artifacts) but stays in-process."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Artifact(BaseModel):
    kind: str
    key: str
    result: dict[str, Any] = Field(default_factory=dict)


class PendingAction(BaseModel):
    """A risky action waiting for a real user "yes". Frozen: nothing may edit it after proposal."""

    model_config = ConfigDict(frozen=True)

    id: str
    action: str
    args: dict[str, Any]
    version_stamp: str
    question: str


class TaskResult(BaseModel):
    status: Literal["completed", "needs_input", "failed"]
    message: str = ""
    artifacts: list[Artifact] = Field(default_factory=list)
    pending_action: PendingAction | None = None
