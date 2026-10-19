import json

import yaml
from langchain_core.utils.function_calling import convert_to_openai_tool

from agent_kit.agents.notes_curator.tools import NotesBackend, build_tools
from agent_kit.bridge import make_delegate_tool
from agent_kit.registry import BUILTIN_AGENTS, discover


async def _dispatch(agent: str, task: str) -> str:
    return f"{agent}:{task}"


def serialise(tools) -> bytes:
    return json.dumps([convert_to_openai_tool(t) for t in tools], sort_keys=True).encode()


def agent_names(schema) -> list[str]:
    prop = schema["parameters"]["properties"]["agent"]
    return prop.get("enum") or [prop["const"]]  # pydantic emits const for a one-value Literal


def registry():
    return discover(BUILTIN_AGENTS, build_tools(NotesBackend()))


def test_flag_off_orchestrator_tools_unchanged():
    from langchain_core.tools import tool

    @tool
    def search(q: str) -> str:
        """Search things."""
        return q

    before = serialise([search])
    extra = make_delegate_tool(registry(), False, _dispatch)
    assert extra is None
    after = serialise([search, *([extra] if extra else [])])
    assert after == before  # byte-for-byte
    assert make_delegate_tool(registry(), True, _dispatch) is not None  # and the flag matters


async def test_delegate_tool_generated_from_registry(tmp_path):
    tool = make_delegate_tool(registry(), True, _dispatch)
    schema = convert_to_openai_tool(tool)["function"]
    assert agent_names(schema) == ["notes_curator"]
    assert "Summarises and tidies" in schema["description"]
    assert await tool.ainvoke({"agent": "notes_curator", "task": "go"}) == "notes_curator:go"

    # adding an agent is adding a folder: it shows up in the enum with its description
    folder = tmp_path / "echo"
    folder.mkdir()
    (folder / "agent.yaml").write_text(yaml.safe_dump(
        {"name": "echo", "description": "Echoes things back", "skill": "SKILL.md", "tools": []}))
    (folder / "SKILL.md").write_text("echo")
    reg = discover(tmp_path, build_tools(NotesBackend()))
    schema = convert_to_openai_tool(make_delegate_tool(reg, True, _dispatch))["function"]
    assert agent_names(schema) == ["echo"]
    assert "Echoes things back" in schema["description"]


def _two_agent_registry(tmp_path):
    for name in ("alpha", "beta"):
        folder = tmp_path / name
        folder.mkdir()
        (folder / "agent.yaml").write_text(yaml.safe_dump(
            {"name": name, "description": f"{name} helper", "skill": "SKILL.md", "tools": []}))
        (folder / "SKILL.md").write_text(name)
    return discover(tmp_path, build_tools(NotesBackend()))


def test_per_agent_flag_only_lists_enabled_agents(tmp_path):
    reg = _two_agent_registry(tmp_path)
    tool = make_delegate_tool(reg, {"alpha": True, "beta": False}, _dispatch)
    schema = convert_to_openai_tool(tool)["function"]
    assert agent_names(schema) == ["alpha"]
    assert "beta" not in schema["description"]
    both = convert_to_openai_tool(make_delegate_tool(reg, {"alpha": True, "beta": True}, _dispatch))
    assert agent_names(both["function"]) == ["alpha", "beta"]


def test_agent_without_a_flag_is_off_and_all_off_is_byte_identical(tmp_path):
    reg = _two_agent_registry(tmp_path)
    assert reg.enabled_names({"alpha": True}) == ["alpha"]  # beta has no flag: ships dark
    base = serialise([])
    for flags in ({}, {"alpha": False, "beta": False}, False):
        extra = make_delegate_tool(reg, flags, _dispatch)
        assert extra is None and serialise([*([extra] if extra else [])]) == base
    assert reg.enabled_names(True) == ["alpha", "beta"]
