"""Idempotent, budgeted writes.

Order: same-item check -> budget -> validate -> forward -> on unclear transport error, ask
whether the item exists -> ledger. A per-key lock makes parallel duplicate saves collapse to one.

The forward runs as its own task behind `asyncio.shield`. If the turn is cancelled (deadline,
client gone) the in-flight commit is NOT abandoned: it finishes, and is written to the ledger when
it does. A retry of the same key while it is still running joins it instead of forwarding again.
If the commit's outcome was lost (the forward failed after cancellation, or the process cannot
tell), the key stays "uncertain" and the next save asks `exists` first.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

from agent_kit.card import Budget, GuardSpec
from agent_kit.registry import ToolRegistry, ToolSpec

Key = tuple[str, str, str]  # (task_id, tool, item_key)


class TransportError(Exception):
    """The write may or may not have landed (timeout, dropped connection)."""


class WriteRefused(Exception):
    """Budget, validation or failed save. The message is safe to show the model."""


@dataclass(frozen=True)
class LedgerEntry:
    task_id: str
    tool: str
    item_key: str
    result: dict[str, Any]
    kind: str = "saved"


def _as_dict(result: Any) -> dict[str, Any]:
    return result if isinstance(result, dict) else {"value": result}


class WriteGuard:
    def __init__(
        self,
        tools: ToolRegistry,
        exists: Callable[[str, str], Awaitable[bool]],
        budget: Budget,
        validate: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        # `tools` is the default registry `save` forwards through; the runner passes the registry
        # it opened for the chatting user instead.
        self._tools, self._exists, self._budget, self._validate = tools, exists, budget, validate
        self._ledger: dict[Key, LedgerEntry] = {}
        self._attempts: dict[str, int] = {}
        self._locks: dict[Key, asyncio.Lock] = {}
        self._uncertain: set[Key] = set()
        self._inflight: dict[Key, asyncio.Future[Any]] = {}

    def ledger(self, task_id: str) -> list[LedgerEntry]:
        return [e for e in self._ledger.values() if e.task_id == task_id]

    def record(self, task_id: str, tool: str, item_key: str, result: dict[str, Any], kind: str) -> None:
        """Ledger entry for something code (not a guarded save) did, e.g. a confirmed action."""
        self._ledger[(task_id, tool, item_key)] = LedgerEntry(task_id, tool, item_key, result, kind)

    def end_task(self, task_id: str) -> None:
        """Free this task's bookkeeping. Call once the task's result has been consumed."""
        for table in (self._ledger, self._locks):
            for key in [k for k in table if k[0] == task_id]:
                del table[key]
        self._uncertain = {k for k in self._uncertain if k[0] != task_id}
        self._attempts.pop(task_id, None)

    def _commit(self, key: Key, result: dict[str, Any]) -> dict[str, Any]:
        self._ledger[key] = LedgerEntry(key[0], key[1], key[2], result)
        return result

    def _settle_abandoned(self, key: Key, task: asyncio.Future[Any]) -> None:
        """A forward whose caller was cancelled has finished: record it if it succeeded."""
        if task.cancelled() or task.exception() is not None:
            return  # outcome unknown: the key stays uncertain and the next save checks `exists`
        if key in self._uncertain and key not in self._ledger:  # (end_task clears `_uncertain`)
            self._uncertain.discard(key)
            self._commit(key, _as_dict(task.result()))

    def expose(self, tools: ToolRegistry, specs: Iterable[GuardSpec], task_id: str) -> ToolRegistry:
        """A copy of `tools` where every guarded tool keeps its own name, description and
        signature (so the model sees the same schema) but its calls route through `save`."""
        exposed = tools.copy()
        for spec in specs:
            original = tools.spec(spec.tool)
            exposed.put(ToolSpec(
                spec.tool, self._guarded_fn(spec, original, tools, task_id), original.writes,
                original.description,
            ))
        return exposed

    def _guarded_fn(
        self, spec: GuardSpec, original: ToolSpec, tools: ToolRegistry, task_id: str
    ) -> Callable[..., Awaitable[dict[str, Any]]]:
        async def guarded(**payload: Any) -> dict[str, Any]:
            key = payload.get(spec.item_key)
            if not isinstance(key, (str, int)) or key == "":
                raise WriteRefused(f"{spec.item_key!r} is required to identify the item")
            return await self.save(task_id, spec.tool, str(key), payload, tools=tools)

        guarded.__signature__ = inspect.signature(original.fn)  # type: ignore[attr-defined]
        return guarded

    async def save(
        self, task_id: str, tool: str, item_key: str, payload: dict[str, Any],
        *, tools: ToolRegistry | None = None,
    ) -> dict[str, Any]:
        key = (task_id, tool, item_key)
        async with self._locks.setdefault(key, asyncio.Lock()):
            if key in self._ledger:  # same item again: no-op returning the first result
                return self._ledger[key].result
            forward = self._inflight.get(key)  # an earlier cancelled save may still be committing
            if forward is None:
                if key in self._uncertain:
                    if await self._exists(tool, item_key):
                        return self._commit(
                            key, {"item_key": item_key, "recovered_after_interruption": True})
                    self._uncertain.discard(key)
                if self._attempts.get(task_id, 0) >= self._budget.max_saves:
                    raise WriteRefused("save budget exhausted")
                if self._validate:
                    self._validate(payload)
                self._attempts[task_id] = self._attempts.get(task_id, 0) + 1
                self._uncertain.add(key)  # stays set until we know the outcome
                forward = asyncio.ensure_future((tools or self._tools).call(tool, payload))
                self._inflight[key] = forward
                forward.add_done_callback(lambda _t: self._inflight.pop(key, None))
            try:
                result = await asyncio.shield(forward)  # our cancellation must not cancel the commit
            except asyncio.CancelledError:
                forward.add_done_callback(lambda t: self._settle_abandoned(key, t))
                raise
            except (TransportError, TimeoutError):
                if not await self._exists(tool, item_key):
                    self._uncertain.discard(key)
                    raise WriteRefused("save failed and the item does not exist") from None
                result = {"item_key": item_key, "recovered_after_transport_error": True}
            except Exception:
                self._uncertain.discard(key)  # a definite failure, nothing was written
                raise
            self._uncertain.discard(key)
            return self._commit(key, _as_dict(result))
