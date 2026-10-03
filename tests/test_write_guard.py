import asyncio

import pytest

from agent_kit.card import Budget
from agent_kit.registry import ToolRegistry
from agent_kit.write_guard import TransportError, WriteGuard, WriteRefused


class Backend:
    def __init__(self, fail_after_commit=False, fail_before_commit=False, slow_after_commit=0.0):
        self.items, self.forwards = {}, 0
        self.fail_after_commit, self.fail_before_commit = fail_after_commit, fail_before_commit
        self.slow_after_commit = slow_after_commit

    async def forward(self, **payload):
        self.forwards += 1
        await asyncio.sleep(0.01)  # a real await point so concurrent callers interleave
        if self.fail_before_commit:
            raise TransportError("timeout")
        self.items.setdefault(payload["k"], []).append(payload)
        if self.fail_after_commit:
            raise TransportError("timeout after commit")
        await asyncio.sleep(self.slow_after_commit)
        return {"ok": payload["k"]}

    async def exists(self, key):
        return key in self.items


def guard(b, forward=None, **budget):
    reg = ToolRegistry()
    reg.register("save", forward or b.forward, writes=True)
    return WriteGuard(reg, lambda tool, key: b.exists(key), Budget(**budget))


async def test_concurrent_duplicate_saves_one_forward():
    b = Backend()
    g = guard(b)
    results = await asyncio.gather(*[g.save("t", "save", "A", {"k": "A", "n": i}) for i in range(5)])
    assert b.forwards == 1
    assert all(r == results[0] for r in results)
    assert len(g.ledger("t")) == 1


async def test_second_save_of_same_item_is_noop_returning_first_result():
    b = Backend()
    g = guard(b)
    first = await g.save("t", "save", "A", {"k": "A", "v": 1})
    second = await g.save("t", "save", "A", {"k": "A", "v": 2})
    assert second == first and b.forwards == 1 and b.items["A"][0]["v"] == 1
    await g.save("other-task", "save", "A", {"k": "A", "v": 3})  # same key, other task: allowed
    assert b.forwards == 2


async def test_timeout_then_exists_records_success_no_duplicate():
    b = Backend(fail_after_commit=True)
    g = guard(b)
    result = await g.save("t", "save", "A", {"k": "A"})
    assert result["recovered_after_transport_error"] is True
    assert b.forwards == 1  # no blind retry that would duplicate the item
    assert [e.item_key for e in g.ledger("t")] == ["A"]


async def test_timeout_not_exists_fails():
    b = Backend(fail_before_commit=True)
    g = guard(b)
    with pytest.raises(WriteRefused):
        await g.save("t", "save", "A", {"k": "A"})
    assert g.ledger("t") == []


async def test_save_budget_enforced():
    b = Backend()
    g = guard(b, max_saves=2)
    await g.save("t", "save", "A", {"k": "A"})
    await g.save("t", "save", "B", {"k": "B"})
    with pytest.raises(WriteRefused, match="budget"):
        await g.save("t", "save", "C", {"k": "C"})
    assert b.forwards == 2 and "C" not in b.items
    await g.save("t", "save", "A", {"k": "A"})  # repeat of a saved item costs nothing


async def test_validation_runs_before_forward():
    b = Backend()
    reg = ToolRegistry()
    reg.register("save", b.forward, writes=True)

    def validate(p):
        if "k" not in p:
            raise WriteRefused("missing k")

    g = WriteGuard(reg, lambda t, k: b.exists(k), Budget(), validate)
    with pytest.raises(WriteRefused):
        await g.save("t", "save", "A", {})
    assert b.forwards == 0


async def test_cancel_mid_save_then_retry_joins_the_inflight_commit():
    """S4: the deadline cancels a save while the commit is in flight; the retry must not duplicate it."""
    b = Backend(slow_after_commit=0.3)
    g = guard(b)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(g.save("t", "save", "A", {"k": "A"}), 0.1)
    assert b.forwards == 1 and len(b.items["A"]) == 1
    result = await g.save("t", "save", "A", {"k": "A"})  # retry while the first commit still runs
    assert result == {"ok": "A"}  # it joined the first commit and got its real result
    assert b.forwards == 1 and len(b.items["A"]) == 1  # still exactly one backend copy
    assert [e.item_key for e in g.ledger("t")] == ["A"]  # and the ledger knows about it


async def test_shielded_commit_finishes_and_is_recorded_after_cancellation():
    b = Backend(slow_after_commit=0.2)
    g = guard(b)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(g.save("t", "save", "A", {"k": "A"}), 0.05)
    assert g.ledger("t") == []  # the turn is gone and the commit is still in flight
    await asyncio.sleep(0.4)  # nobody retries; the commit completes on its own
    assert [e.item_key for e in g.ledger("t")] == ["A"]  # recorded without a second save
    assert await g.save("t", "save", "A", {"k": "A"}) == {"ok": "A"}
    assert b.forwards == 1 and not g._uncertain and not g._inflight


async def test_cancelled_commit_with_lost_outcome_is_recovered_by_existence_check():
    b = Backend(fail_after_commit=True)  # lands, then reports a transport error
    g = guard(b)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(g.save("t", "save", "A", {"k": "A"}), 0.005)  # cancelled mid-forward
    await asyncio.sleep(0.1)  # the abandoned forward fails after the item landed
    result = await g.save("t", "save", "A", {"k": "A"})
    assert result["recovered_after_interruption"] is True
    assert b.forwards == 1 and len(b.items["A"]) == 1


async def test_non_dict_forward_result_is_normalised():
    async def returns_string(**_):
        return "saved!"

    g = guard(Backend(), forward=returns_string)
    assert await g.save("t", "save", "A", {}) == {"value": "saved!"}
    assert g.ledger("t")[0].result == {"value": "saved!"}


async def test_two_guarded_tools_forward_to_their_own_tool():
    seen = []
    reg = ToolRegistry()

    async def save_note(**p):
        seen.append(("note", p))
        return {"id": "n"}

    async def save_task(**p):
        seen.append(("task", p))
        return {"id": "t"}

    reg.register("save_note", save_note, writes=True)
    reg.register("save_task", save_task, writes=True)
    g = WriteGuard(reg, lambda tool, key: asyncio.sleep(0, False), Budget())
    await g.save("t", "save_note", "X", {"title": "X"})
    await g.save("t", "save_task", "X", {"name": "X"})  # same item key, different tool: both run
    assert seen == [("note", {"title": "X"}), ("task", {"name": "X"})]
    assert {e.tool for e in g.ledger("t")} == {"save_note", "save_task"}


async def test_end_task_frees_bookkeeping():
    b = Backend()
    g = guard(b)
    await g.save("t", "save", "A", {"k": "A"})
    g.end_task("t")
    assert g.ledger("t") == [] and not g._locks and not g._attempts and not g._uncertain


async def test_uncertain_key_with_item_missing_forwards_again():
    b = Backend(slow_after_commit=0)
    forwards = []
    g = guard(b)
    g._uncertain.add(("t", "save", "A"))  # as left behind by an interrupted save
    orig = g._tools.call

    async def spy(tool, payload):
        forwards.append(tool)
        return await orig(tool, payload)

    g._tools.call = spy
    await g.save("t", "save", "A", {"k": "A"})
    assert forwards == ["save"] and len(b.items["A"]) == 1  # never landed, so really written


async def test_exists_receives_the_correct_tool_name():
    seen = []
    reg = ToolRegistry()

    async def fail(**_):
        raise TransportError("t")

    reg.register("save_task", fail, writes=True)

    async def exists(tool, key):
        seen.append((tool, key))
        return True

    g = WriteGuard(reg, exists, Budget())
    await g.save("t", "save_task", "K", {})
    assert seen == [("save_task", "K")]
