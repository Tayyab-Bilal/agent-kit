"""End-to-end walkthrough of notes_curator with a scripted fake LLM. No network, no keys."""

from __future__ import annotations

import asyncio

from agent_kit import runner
from agent_kit.agents.notes_curator.tools import NotesBackend, build_tools
from agent_kit.agents.workflow_helper.diagnose import diagnose
from agent_kit.agents.workflow_helper.tools import WorkflowBackend, build_workflow_tools
from agent_kit.confirm import ConfirmGate
from agent_kit.consent import RuleConsentJudge
from agent_kit.fakes import FakeBackendOpener, ScriptedLLM
from agent_kit.handles import HandleStore
from agent_kit.llm import Final, ToolCall
from agent_kit.registry import BUILTIN_AGENTS, discover
from agent_kit.write_guard import WriteGuard


def say(title: str) -> None:
    print(f"\n=== {title} ===")


async def workflow_helper_story() -> None:
    """Second agent, opened as the chatting user: find, diagnose, guarded copy, confirm publish."""
    store = WorkflowBackend()
    tools = build_workflow_tools(store)
    registry = discover(BUILTIN_AGENTS, tools)
    agent = registry.agents["workflow_helper"]
    card, gate, guard = agent.card, ConfirmGate(), WriteGuard(tools, store.workflow_exists, agent.card.budget)
    # The fake backend decides what each user may use. Dana has every tool this agent needs.
    opener = FakeBackendOpener(lambda user: tools, {"dana": set(card.tools)})

    async def go(task_id: str, script: list) -> runner.TaskResult:
        return await runner.run(
            card, runner.Task(task_id, "help with my workflows"), ScriptedLLM(script), None,
            HandleStore(), guard, gate, skill_text=agent.skill_text, version_of=store.version_of,
            opener=opener, user="dana",
        )

    say("workflow_helper: two workflows match 'weekly', so it asks")
    asked = await go("wf-1", [
        [ToolCall("find_workflows", {"name": "weekly"})],
        [ToolCall("ask_user", {"question": "Which one: 'Weekly report' or 'Weekly report (old)'?"})],
    ])
    print(f"{asked.status}: {asked.message}")

    say("workflow_helper: diagnose, as a tool and as a plain function call")
    await go("wf-2", [[ToolCall("diagnose_workflow", {"workflow_id": "wf-1"})], Final("see findings")])
    report = diagnose(store.workflows["wf-1"], store.runs["wf-1"])  # same function, no agent
    print(f"risk: {report['risk']}; last run died on step {report['last_failure']['step']}")
    for finding in report["findings"]:
        print(f"  - {finding['message']}")

    say("workflow_helper: guarded copy with an edit in the same save, then confirm-first publish")
    copied = await go("wf-3", [[ToolCall("copy_workflow", {
        "source_id": "wf-1", "new_name": "Onboarding (fixed)", "edits": {"s3.channel": "#welcome"}})],
        [ToolCall("publish_workflow", {"workflow_id": "wf-5"})], Final("waiting")])
    print(f"artifacts: {[(a.key, a.result['id']) for a in copied.artifacts]}")
    print(f"question: {copied.message}")
    published = await runner.resume(card, copied.pending_action.id, "yes", RuleConsentJudge(), None,
                                    gate, guard, "wf-3", current_version=copied.pending_action.version_stamp,
                                    opener=opener, user="dana")
    print(f"{published.status}: wf-5 is now {store.workflows['wf-5']['status']}")

    say("workflow_helper: delete is not on the card, so it is refused")
    llm = ScriptedLLM([[ToolCall("delete_workflow", {"workflow_id": "wf-1"})], Final("done")])
    await runner.run(card, runner.Task("wf-4", "delete it"), llm, None, HandleStore(), guard, gate,
                     opener=opener, user="dana")
    print(f"model was told: {llm.seen[-1][-1]['content']}")
    print(f"wf-1 still exists: {'wf-1' in store.workflows}")


async def main() -> None:
    backend = NotesBackend(500)
    tools = build_tools(backend)
    registry = discover(BUILTIN_AGENTS, tools)
    agent = registry.agents["notes_curator"]
    card, gate, judge = agent.card, ConfirmGate(), RuleConsentJudge()
    guard = WriteGuard(tools, lambda tool, key: backend.note_exists(key), card.budget)

    async def go(task_id: str, text: str, script: list) -> runner.TaskResult:
        return await runner.run(
            card, runner.Task(task_id, text), ScriptedLLM(script), tools, HandleStore(), guard,
            gate, skill_text=agent.skill_text, version_of=backend.version_of,
        )

    say("Task 1: summarise, then propose archiving")
    first = await go("task-1", "Summarise my longest notes and archive three old ones", [
        [ToolCall("list_notes")],  # 500 rows: becomes handle list_notes_1, model sees a summary
        [ToolCall("sql_query", {"query": (
            "SELECT id, title, words FROM list_notes_1 ORDER BY words DESC LIMIT 5")})],
        [ToolCall("save_note", {"title": "Top 5 longest notes", "body": "Summary of the 5 longest."})],
        [ToolCall("archive_notes", {"ids": [3, 4, 5]})],  # risky: stored, not executed
        Final("Archiving is waiting for the user."),
    ])
    print(f"status: {first.status}")
    print(f"code-written question:\n  {first.message}")
    print(f"archived so far: {[n['id'] for n in backend.notes if n['archived']]}")

    say('User replies: "yes but only two of them"')
    version = await backend.version_of("", {})
    cancelled = await runner.resume(card, first.pending_action.id, "yes but only two of them", judge,
                                    tools, gate, guard, "task-1", current_version=version)
    print(f"{cancelled.status}: {cancelled.message}")
    print(f"archived so far: {[n['id'] for n in backend.notes if n['archived']]}")

    say("Task 2: propose again, user replies plain yes")
    second = await go("task-2", "Archive notes 3, 4 and 5", [
        [ToolCall("archive_notes", {"ids": [3, 4, 5]})], Final("waiting")])
    print(f"question: {second.message}")
    done = await runner.resume(card, second.pending_action.id, "yes", judge, tools, gate, guard,
                               "task-2", current_version=await backend.version_of("", {}))
    print(f"archived with the STORED args: {[n['id'] for n in backend.notes if n['archived']]}")

    await workflow_helper_story()

    say("Write ledger (what really happened)")
    for task_id in ("task-1", "task-2"):
        for entry in guard.ledger(task_id):
            print(f"  {entry.task_id} [{entry.kind}] {entry.tool}: {entry.item_key[:24]!r} -> {entry.result}")

    say("Final TaskResult (task 2, after the user's yes)")
    print(done.model_dump_json(indent=2))


if __name__ == "__main__":
    asyncio.run(main())
