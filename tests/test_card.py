import pytest
import yaml
from pydantic import ValidationError

from agent_kit.card import AgentCard, Budget, CardError
from agent_kit.fakes import ScriptedLLM
from agent_kit.registry import BUILTIN_AGENTS, ToolRegistry, discover

BASE = {"name": "a", "description": "d", "skill": "SKILL.md", "tools": ["t"]}


def test_extra_key_rejected():
    with pytest.raises(ValidationError):
        AgentCard.model_validate({**BASE, "gaurded_writes": []})
    with pytest.raises(ValidationError):
        AgentCard.model_validate({**BASE, "budget": {"max_stepz": 3}})


def test_unguarded_write_tool_rejects_card():
    async def noop(**_): ...

    reg = ToolRegistry()
    reg.register("t", noop, writes=True)
    card = AgentCard.model_validate(BASE)
    with pytest.raises(CardError):
        card.validate_against(reg)
    guarded = AgentCard.model_validate({**BASE, "guarded_writes": [{"tool": "t", "item_key": "k"}]})
    guarded.validate_against(reg)  # the same tool passes once guarded
    gated = AgentCard.model_validate({**BASE, "confirm_actions": ["t"]})
    gated.validate_against(reg)


def test_guard_tool_must_be_in_allow_list():
    with pytest.raises(ValidationError, match="not in tools"):
        AgentCard.model_validate({**BASE, "guarded_writes": [{"tool": "other", "item_key": "k"}]})
    with pytest.raises(ValidationError, match="not in tools"):
        AgentCard.model_validate({**BASE, "confirm_actions": ["other"]})


def _agent(root, name, card_extra=None, skill=True):
    folder = root / name
    folder.mkdir()
    (folder / "agent.yaml").write_text(yaml.safe_dump({**BASE, "name": name, **(card_extra or {})}))
    if skill:
        (folder / "SKILL.md").write_text("skill")


def test_broken_card_excluded_others_load(tmp_path):
    async def noop(**_): ...

    reg = ToolRegistry()
    reg.register("t", noop, writes=True)
    ok = {"guarded_writes": [{"tool": "t", "item_key": "k"}]}
    _agent(tmp_path, "good", ok)
    _agent(tmp_path, "unknown_key", {**ok, "bogus": 1})
    _agent(tmp_path, "unguarded")  # write tool, no guard
    _agent(tmp_path, "no_skill", ok, skill=False)
    (tmp_path / "garbage").mkdir()
    (tmp_path / "garbage" / "agent.yaml").write_text("- just\n- a list\n")
    found = discover(tmp_path, reg)
    assert found.names() == ["good"]
    assert set(found.errors) == {"unknown_key", "unguarded", "no_skill", "garbage"}


def test_builtin_notes_curator_loads():
    from agent_kit.agents.notes_curator.tools import NotesBackend, build_tools

    found = discover(BUILTIN_AGENTS, build_tools(NotesBackend()))
    # workflow_helper's tools are not registered here, so only it is excluded (fail closed)
    assert found.names() == ["notes_curator"] and list(found.errors) == ["workflow_helper"]


async def test_missing_tool_refuses_to_start(env):
    card = env.card.model_copy(update={"tools": [*env.card.tools, "ghost_tool"]})
    llm = ScriptedLLM([])
    result = await env.run(llm, card=card)
    assert result.status == "failed" and "ghost_tool" in result.message
    assert llm.seen == []  # the model was never even consulted


def test_missing_tool_excluded_at_load_so_bridge_never_offers_it(tmp_path):
    """S7: an agent that could only fail at run time is not discoverable."""
    _agent(tmp_path, "ghosty", {"tools": ["t", "not_registered"]})

    async def noop(**_): ...

    reg = ToolRegistry()
    reg.register("t", noop)
    found = discover(tmp_path, reg)
    assert found.agents == {} and "not_registered" in found.errors["ghosty"]


def test_skill_path_must_stay_inside_agent_folder(tmp_path):
    (tmp_path / "secret.txt").write_text("outside")
    _agent(tmp_path, "sneaky", {"skill": "../secret.txt"})
    _agent(tmp_path, "abs", {"skill": str(tmp_path / "secret.txt")})

    async def noop(**_): ...

    reg = ToolRegistry()
    reg.register("t", noop)
    found = discover(tmp_path, reg)
    assert found.agents == {} and set(found.errors) == {"sneaky", "abs"}


@pytest.mark.parametrize("bad", [
    {"deadline_s": float("inf")}, {"deadline_s": float("nan")}, {"max_steps": 2.5},
    {"max_steps": True}, {"max_tool_calls": "5"}, {"max_steps": 0},
])
def test_budget_is_strict(bad):
    with pytest.raises(ValidationError):
        Budget(**bad)
