"""Consent gate: code proposes, a separate judge reads the user's reply, stored args execute.

What is protected: the gate keeps the only authoritative copy of every pending action, keyed by
id and bound to a task. A caller can hand back only an id; the action, args and version that run
are the stored ones. Each id works once, so a replay or a forged/other-task id executes nothing.
What is NOT protected: the gate lives in process memory. A real deployment must keep this table
in storage the model and the user-facing client cannot write to.
"""

from __future__ import annotations

import copy
import json
import unicodedata
import uuid
from dataclasses import dataclass
from typing import Any

from agent_kit.consent import ConsentJudge
from agent_kit.result import PendingAction

# Values are JSON-encoded (quoted, newlines escaped) so data cannot break out of the template.
QUESTION_TEMPLATE = (
    "The assistant wants to run action {action} with exactly these arguments: {args}. "
    "Reply yes to allow it or no to cancel."
)
_INVISIBLE = {"Cc", "Cf", "Zl", "Zp"}  # control, format (bidi, zero-width), line separators


def _safe_json(value: Any) -> str:
    """JSON with real Unicode (so Arabic titles stay readable) but no invisible characters,
    which could reorder or hide text in the question the user reads."""
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return "".join(
        f"\\u{ord(c):04x}" if unicodedata.category(c) in _INVISIBLE and ord(c) < 0x10000
        else f"\\U{ord(c):08x}" if unicodedata.category(c) in _INVISIBLE
        else c
        for c in text
    )


@dataclass(frozen=True)
class Execute:
    action: str
    args: dict[str, Any]


@dataclass(frozen=True)
class Cancel:
    reason: str


@dataclass(frozen=True)
class AskAgain:
    action: str
    args: dict[str, Any]
    reason: str


class ConfirmGate:
    def __init__(self) -> None:
        self._pending: dict[str, tuple[str, PendingAction]] = {}  # id -> (task_id, stored copy)

    def end_task(self, task_id: str) -> None:
        """Drop a finished task's unanswered pending actions."""
        self._pending = {i: e for i, e in self._pending.items() if e[0] != task_id}

    @staticmethod
    def question_for(action: str, args: dict[str, Any]) -> str:
        return QUESTION_TEMPLATE.format(action=_safe_json(action), args=_safe_json(args))

    def propose(self, task_id: str, action: str, args: dict[str, Any], version: str) -> PendingAction:
        stored = copy.deepcopy(args)  # later edits to the caller's dict must not leak in
        pending = PendingAction(
            id=uuid.uuid4().hex, action=action, args=stored, version_stamp=version,
            question=self.question_for(action, stored),
        )
        self._pending[pending.id] = (task_id, pending)
        return pending

    async def resolve(
        self, pending_id: str, task_id: str, user_reply: str, judge: ConsentJudge,
        current_version: str,
    ) -> Execute | Cancel | AskAgain:
        entry = self._pending.get(pending_id)
        if entry is None or entry[0] != task_id:
            return Cancel("unknown, already used, or belongs to another task")
        del self._pending[pending_id]  # single use, whatever the verdict
        pending = entry[1]
        try:
            verdict = await judge.judge(pending.question, user_reply)
        except Exception:  # fail closed: a broken judge never confirms
            return Cancel("consent judge failed")
        if verdict == "no":
            return Cancel("user declined")
        if verdict != "yes":
            return Cancel("reply was not a clear yes")
        args = copy.deepcopy(pending.args)
        if current_version != pending.version_stamp:
            return AskAgain(pending.action, args, "the data changed since the question was asked")
        return Execute(pending.action, args)
