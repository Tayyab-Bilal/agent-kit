"""Every python block in README.md and docs/*.md runs. Blocks in one file run top to bottom in one
namespace (later blocks may use earlier names) and may use top-level `await`, as in
`python -m asyncio`. Every yaml block that looks like an agent card must validate."""

import ast
import re
from pathlib import Path

import pytest
import yaml

from agent_kit.card import AgentCard

ROOT = Path(__file__).parent.parent
DOCS = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]
FENCE = re.compile(r"^```(\w+)\n(.*?)^```", re.S | re.M)


def blocks(path: Path, lang: str) -> list[str]:
    return [body for tag, body in FENCE.findall(path.read_text()) if tag == lang]


def test_expected_docs_exist():
    names = {p.name for p in DOCS}
    assert {"README.md", "usage.md", "api.md", "writing-an-agent.md"} <= names


@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")
@pytest.mark.parametrize("path", DOCS, ids=lambda p: p.name)
async def test_python_blocks_run(path):
    code_blocks = blocks(path, "python")
    assert code_blocks, f"{path.name} has no runnable example"
    namespace: dict = {"__name__": "__doc__"}
    for i, code in enumerate(code_blocks, 1):
        compiled = compile(code, f"{path.name} block {i}", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
        result = eval(compiled, namespace)  # noqa: S307 - our own documentation
        if result is not None:
            await result


@pytest.mark.parametrize("path", DOCS, ids=lambda p: p.name)
def test_card_yaml_blocks_are_valid_cards(path):
    for text in blocks(path, "yaml"):
        data = yaml.safe_load(text)
        if isinstance(data, dict) and "tools" in data:
            AgentCard.model_validate(data)
