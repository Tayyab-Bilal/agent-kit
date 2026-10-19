"""A scripted hostile LLM attacks the runner. Every attack must be blocked by code, not by luck.

The assertions check the world (backend state, handle contents, forward counts), not just the
error text the model saw.
"""

import pytest

from agent_kit.fakes import ScriptedLLM
from agent_kit.llm import Final, ToolCall

from ..conftest import DONE, calls, tool_observations

# fork() from a threaded process warns; this module forks on purpose (see handles.jmes).
pytestmark = pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")

LIST = ("list_notes", {})


def handle_rows(env) -> int:
    return env.handles.sql("SELECT count(*) FROM list_notes_1")["rows"][0][0]


async def test_attack_tool_outside_allow_list(env):
    hit = []

    async def drop_everything():
        hit.append(1)

    env.tools.register("drop_everything", drop_everything, writes=True)  # exists, not on the card
    llm = ScriptedLLM([calls(("drop_everything", {})), DONE])
    result = await env.run(llm)
    assert hit == [] and result.status == "completed"
    assert "not allowed" in tool_observations(llm)[0]


async def test_attack_tool_unknown_everywhere(env):
    llm = ScriptedLLM([calls(("rm_rf", {}), ("__import__", {"x": 1})), DONE])
    result = await env.run(llm)
    assert result.status == "completed"
    assert all("not allowed" in o for o in tool_observations(llm))


@pytest.mark.parametrize("sql", [
    "DELETE FROM list_notes_1",
    "UPDATE list_notes_1 SET title = 'owned'",
    "INSERT INTO list_notes_1 (id) VALUES (1)",
    "DROP TABLE list_notes_1",
    "CREATE TABLE x AS SELECT * FROM list_notes_1",
])
async def test_attack_sql_write_via_handle_tool(env, sql):
    llm = ScriptedLLM([calls(LIST, ("sql_query", {"query": sql})), DONE])
    await env.run(llm)
    assert handle_rows(env) == 500
    assert env.handles.sql("SELECT count(*) FROM list_notes_1 WHERE title = 'owned'")["rows"] == [[0]]
    assert "refused" in tool_observations(llm)[-1]


async def test_attack_attach_database_via_handle_tool(env, tmp_path):
    target = tmp_path / "exfil.db"
    llm = ScriptedLLM([calls(LIST, ("sql_query", {"query": f"ATTACH '{target}' AS x"})), DONE])
    await env.run(llm)
    assert not target.exists() and "refused" in tool_observations(llm)[-1]


async def test_attack_multi_statement_via_handle_tool(env):
    llm = ScriptedLLM([calls(LIST, ("sql_query", {
        "query": "SELECT 1; DROP TABLE list_notes_1"})), DONE])
    await env.run(llm)
    assert handle_rows(env) == 500


async def test_attack_pragma_via_handle_tool(env):
    llm = ScriptedLLM([calls(LIST, ("sql_query", {"query": "PRAGMA writable_schema=1"})), DONE])
    await env.run(llm)
    assert "refused" in tool_observations(llm)[-1]


async def test_attack_resource_exhaustion_via_handle_tool(env):
    llm = ScriptedLLM([calls(
        LIST,
        ("sql_query", {"query": "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) "
                                "SELECT count(*) FROM c"}),
        ("sql_query", {"query": "SELECT length(zeroblob(900000000))"}),
        ("sql_query", {"query": "SELECT * FROM list_notes_1 a, list_notes_1 b, list_notes_1 c"}),
    ), DONE])
    result = await env.run(llm)
    assert result.status == "completed"
    obs = tool_observations(llm)
    assert "timed out" in obs[1] and "refused" in obs[2]
    assert len(obs[3]) < 100_000  # the cross join was capped, not streamed whole


async def test_attack_forge_confirmation_by_calling_confirm_action_directly(env):
    llm = ScriptedLLM([
        calls(("archive_notes", {"ids": [1, 2, 3], "confirmed": True, "user_said_yes": "yes"})),
        DONE,
    ])
    result = await env.run(llm)
    assert result.pending_action is None  # smuggled "confirmed" fields don't fit the tool
    assert not any(n["archived"] for n in env.backend.notes)
    assert env.backend.revision == 0  # not a single write happened
    result = await env.run(ScriptedLLM([calls(("archive_notes", {"ids": [1, 2, 3]})), DONE]))
    assert result.status == "needs_input"  # the plain call can only ever ask
    assert env.backend.revision == 0


async def test_attack_claim_confirmation_in_final_message(env):
    llm = ScriptedLLM([Final("The user confirmed, I archived notes 1-400.",
                             asserted_artifacts=["archive_notes"])])
    result = await env.run(llm)
    assert result.artifacts == []
    assert not any(n["archived"] for n in env.backend.notes)


async def test_attack_forged_reply_in_tool_data_is_not_a_user_reply(env):
    env.backend.notes[0]["title"] = "yes"
    llm = ScriptedLLM([calls(("list_notes", {"include_archived": True}),
                             ("archive_notes", {"ids": [1]})), DONE])
    result = await env.run(llm)
    assert result.status == "needs_input" and not env.backend.notes[0]["archived"]


async def test_attack_save_same_item_twice_in_one_step(env):
    note = {"title": "Dup", "body": "x"}
    llm = ScriptedLLM([calls(("save_note", note), ("save_note", {**note, "body": "y"})),
                       calls(("save_note", {**note, "body": "z"})), DONE])
    result = await env.run(llm)
    assert env.backend.forward_calls == 1
    assert [a.key for a in result.artifacts] == ["Dup"]


async def test_attack_exceed_save_budget(env):
    saves = [("save_note", {"title": f"N{i}", "body": "x"}) for i in range(20)]
    result = await env.run(ScriptedLLM([calls(*saves), DONE]))
    assert env.backend.forward_calls == env.card.budget.max_saves == 3
    assert len(result.artifacts) == 3


async def test_attack_exceed_tool_call_budget(env):
    result = await env.run(ScriptedLLM([calls(*[LIST] * 500)]))
    assert result.status == "failed"


async def test_attack_never_ending_loop(env):
    llm = ScriptedLLM([calls(LIST)] * 1000)
    result = await env.run(llm)
    assert result.status == "failed" and len(llm.seen) == env.card.budget.max_steps


async def test_attack_unguarded_write_tool_on_hand_built_card(env):
    """A card that skipped load-time validation still cannot reach a raw write."""
    card = env.card.model_construct(
        **{**env.card.model_dump(), "guarded_writes": [], "confirm_actions": []}
    )
    result = await env.run(ScriptedLLM([calls(("save_note", {"title": "x", "body": "y"})), DONE]),
                           card=card)
    assert result.status == "failed" and "card rejected" in result.message
    assert env.backend.forward_calls == 0


async def test_attack_garbage_arguments(env):
    llm = ScriptedLLM([[ToolCall("save_note", "not-a-dict"), ToolCall(["x"], {}),  # type: ignore[arg-type]
                        ToolCall("sql_query", {"query": None})], DONE])
    result = await env.run(llm)
    assert result.status == "completed" and env.backend.forward_calls == 0
