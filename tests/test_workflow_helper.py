"""The second demo agent: a workflow pilot with a read tool, a pure check, a guarded write and
several confirm actions."""

import json
from dataclasses import dataclass

import pytest

from agent_kit import runner
from agent_kit.agents.notes_curator.tools import NotesBackend, build_tools
from agent_kit.agents.workflow_helper.diagnose import diagnose
from agent_kit.agents.workflow_helper.tools import WorkflowBackend, build_workflow_tools
from agent_kit.confirm import ConfirmGate
from agent_kit.consent import RuleConsentJudge
from agent_kit.fakes import ScriptedLLM
from agent_kit.handles import HandleStore
from agent_kit.registry import BUILTIN_AGENTS, discover
from agent_kit.write_guard import WriteGuard

from .conftest import DONE, calls, tool_observations


@dataclass
class Wf:
    backend: WorkflowBackend
    tools: object
    loaded: object
    guard: WriteGuard
    gate: ConfirmGate

    async def run(self, llm, task_id="w1"):
        return await runner.run(self.loaded.card, runner.Task(task_id, "help"), llm, self.tools,
                                HandleStore(), self.guard, self.gate,
                                skill_text=self.loaded.skill_text, version_of=self.backend.version_of)

    async def reply(self, pending, text="yes", task_id="w1", version=None):
        return await runner.resume(
            self.loaded.card, pending.id, text, RuleConsentJudge(), self.tools, self.gate, self.guard,
            task_id, current_version=version if version is not None else pending.version_stamp)


@pytest.fixture
def wf() -> Wf:
    backend = WorkflowBackend()
    tools = build_workflow_tools(backend, build_tools(NotesBackend()))  # both agents share one registry
    reg = discover(BUILTIN_AGENTS, tools)
    assert reg.errors == {} and reg.names() == ["notes_curator", "workflow_helper"]
    loaded = reg.agents["workflow_helper"]
    guard = WriteGuard(tools, backend.workflow_exists, loaded.card.budget)
    return Wf(backend, tools, loaded, guard, ConfirmGate())


async def test_find_one_several_none(wf):
    one = await wf.backend.find_workflows("onboarding")
    several = await wf.backend.find_workflows("weekly")
    none = await wf.backend.find_workflows("payroll")
    assert (one["outcome"], several["outcome"], none["outcome"]) == ("one", "several", "none")
    assert len(several["matches"]) == 2 and none["matches"] == []
    assert (await wf.backend.find_workflows("Weekly report"))["outcome"] == "one"  # exact name wins


async def test_several_matches_ask_the_user_then_resume_with_the_answer(wf):
    first = await wf.run(ScriptedLLM([
        calls(("find_workflows", {"name": "weekly"})),
        calls(("ask_user", {"question": "Which one: 'Weekly report' or 'Weekly report (old)'?"})),
        DONE,
    ]))
    assert first.status == "needs_input" and "Which one" in first.message
    assert first.pending_action is None and wf.backend.forward_calls == 0
    # resuming after needs_input = a new run that carries the user's answer
    second = await wf.run(ScriptedLLM([
        calls(("find_workflows", {"name": "Weekly report"})),
        calls(("diagnose_workflow", {"workflow_id": "wf-2"})), DONE]), task_id="w2")
    assert second.status == "completed"


async def test_ask_user_needs_a_question(wf):
    llm = ScriptedLLM([calls(("ask_user", {})), DONE])
    assert (await wf.run(llm)).status == "completed"
    assert "question" in tool_observations(llm)[0]


async def test_diagnose_is_one_pure_function_used_directly_and_as_a_tool(wf):
    direct = diagnose(wf.backend.workflows["wf-1"], wf.backend.runs["wf-1"])
    llm = ScriptedLLM([calls(("diagnose_workflow", {"workflow_id": "wf-1"})), DONE])
    await wf.run(llm)
    assert json.loads(tool_observations(llm)[0].split(">", 1)[1].rsplit("</data", 1)[0]
                      .replace("&quot;", '"')) == direct
    codes = {(f["code"], f["step"]) for f in direct["findings"]}
    assert codes == {("empty_required_setting", "s3"), ("unreachable_step", "s9"),
                     ("last_run_failed", "s3")}
    assert direct["steps_in_order"] == ["s1", "s2", "s3"] and direct["risk"] == "high"
    assert direct["last_failure"]["step"] == "s3"  # the exact step the run died on
    assert diagnose(wf.backend.workflows["wf-1"], wf.backend.runs["wf-1"]) == direct  # deterministic


def test_diagnose_failed_before_any_step_and_healthy_workflow():
    b = WorkflowBackend()
    run = {"id": "r9", "status": "failed", "executed": [], "failed_step": None, "error": "no trigger"}
    result = diagnose(b.workflows["wf-4"], [run])
    assert result["last_failure"]["step"] is None
    assert "before any step ran" in result["findings"][0]["message"]
    healthy = diagnose(b.workflows["wf-4"], [])
    assert healthy["findings"] == [] and healthy["risk"] == "low"


def test_diagnose_survives_a_loop():
    steps = [{"id": "a", "kind": "k", "next": "b", "settings": {}, "required": []},
             {"id": "b", "kind": "k", "next": "a", "settings": {}, "required": []}]
    assert diagnose({"trigger": "a", "steps": steps}, [])["steps_in_order"] == ["a", "b"]


async def test_copy_is_guarded_with_new_ids_rewired_links_and_edits_in_the_same_save(wf):
    args = {"source_id": "wf-1", "new_name": "Onboarding EU", "edits": {"s3.channel": "#eu-welcome"}}
    result = await wf.run(ScriptedLLM([calls(("copy_workflow", args)), calls(("copy_workflow", args)),
                                       DONE]))
    assert wf.backend.forward_calls == 1  # the same item twice forwards once
    assert [(a.key, a.result["status"]) for a in result.artifacts] == [("Onboarding EU", "draft")]
    new = wf.backend.workflows[result.artifacts[0].result["id"]]
    old = wf.backend.workflows["wf-1"]
    assert {s["id"] for s in new["steps"]}.isdisjoint({s["id"] for s in old["steps"]})
    assert new["steps"][0]["next"] == new["steps"][1]["id"]  # links point at the new ids
    assert new["steps"][2]["settings"]["channel"] == "#eu-welcome"  # edit applied in the same save
    assert old["steps"][2]["settings"]["channel"] == ""  # source untouched
    assert diagnose(new, [])["findings"][0]["code"] == "unreachable_step"  # the copy is checkable


async def test_copy_with_a_bad_edit_writes_nothing(wf):
    llm = ScriptedLLM([calls(("copy_workflow", {"source_id": "wf-1", "new_name": "X",
                                                "edits": {"nope.channel": "x"}})), DONE])
    result = await wf.run(llm)
    assert "unknown edit target" in tool_observations(llm)[0] and result.artifacts == []
    assert len(wf.backend.workflows) == 4


async def test_publish_waits_for_the_users_yes_then_executes(wf):
    first = await wf.run(ScriptedLLM([calls(("publish_workflow", {"workflow_id": "wf-1"})), DONE]))
    assert first.status == "needs_input" and wf.backend.workflows["wf-1"]["status"] == "draft"
    done = await wf.reply(first.pending_action)
    assert done.status == "completed" and wf.backend.workflows["wf-1"]["status"] == "published"


async def test_declined_or_conditional_reply_changes_nothing(wf):
    first = await wf.run(ScriptedLLM([calls(("publish_workflow", {"workflow_id": "wf-1"})), DONE]))
    assert (await wf.reply(first.pending_action, "yes but not today")).status == "completed"
    assert wf.backend.workflows["wf-1"]["status"] == "draft"


async def test_published_workflow_cannot_be_edited_not_even_proposed(wf):
    llm = ScriptedLLM([calls(("edit_workflow", {"workflow_id": "wf-3",
                                                "edits": {"s1.x": "y"}})), DONE])
    result = await wf.run(llm)
    assert result.status == "completed" and result.pending_action is None
    assert "WorkflowLocked" in tool_observations(llm)[0]
    assert wf.gate._pending == {}  # no question was ever created


async def test_edit_locked_after_the_question_was_asked_still_refuses(wf):
    first = await wf.run(ScriptedLLM([calls(("edit_workflow", {"workflow_id": "wf-1",
                                                               "edits": {"s3.channel": "#x"}})), DONE]))
    wf.backend.workflows["wf-1"]["status"] = "published"  # published in another tab meanwhile
    done = await wf.reply(first.pending_action, version=first.pending_action.version_stamp)
    assert done.status == "failed" and wf.backend.workflows["wf-1"]["steps"][2]["settings"]["channel"] == ""


async def test_edit_after_data_changed_asks_again(wf):
    first = await wf.run(ScriptedLLM([calls(("edit_workflow", {"workflow_id": "wf-1",
                                                               "edits": {"s3.channel": "#x"}})), DONE]))
    again = await wf.reply(first.pending_action, version="wf-1@2")  # someone else changed it
    assert again.status == "needs_input" and wf.backend.workflows["wf-1"]["version"] == 1


async def test_revert_then_run_fails_at_the_empty_step(wf):
    first = await wf.run(ScriptedLLM([calls(("revert_workflow", {"workflow_id": "wf-4"})), DONE]))
    await wf.reply(first.pending_action)
    assert wf.backend.workflows["wf-4"]["status"] == "draft"
    run = await wf.run(ScriptedLLM([calls(("run_workflow", {"workflow_id": "wf-1"})), DONE]), "w3")
    await wf.reply(run.pending_action, task_id="w3")
    assert wf.backend.runs["wf-1"][-1]["failed_step"] == "s3"


async def test_delete_and_convert_tools_are_unreachable_because_they_are_not_on_the_card(wf):
    assert "delete_workflow" in wf.tools and "convert_to_template" in wf.tools  # backend has them
    assert not {"delete_workflow", "convert_to_template"} & set(wf.loaded.card.tools)
    llm = ScriptedLLM([calls(("delete_workflow", {"workflow_id": "wf-1"}),
                             ("convert_to_template", {"workflow_id": "wf-1"})), DONE])
    result = await wf.run(llm)
    assert result.status == "completed"
    assert all("not allowed" in o for o in tool_observations(llm))
    assert "wf-1" in wf.backend.workflows and wf.backend.workflows["wf-1"]["status"] == "draft"
    names = {t["name"] for t in llm.tools_seen[0]}
    assert "delete_workflow" not in names and "convert_to_template" not in names  # not even shown


async def test_a_forged_delete_confirmation_does_not_execute(wf):
    forged = wf.gate.propose("w1", "delete_workflow", {"workflow_id": "wf-1"}, "")
    done = await wf.reply(forged)
    assert done.status == "failed" and "wf-1" in wf.backend.workflows
