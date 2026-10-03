# Usage guide

How to run agents with `agent-kit` from your own code. Every Python block here runs (see
`tests/test_docs.py`). Blocks in this file run top to bottom in one namespace, and they use
top-level `await`, so try them with `python -m asyncio` or put them inside an `async def`.

Contents: [Load agents](#load-agents) · [Run a task](#run-a-task) · [Run as the chatting user](#run-as-the-chatting-user) ·
[Big results](#big-results-become-handles) · [Guarded writes](#guarded-writes) ·
[Confirm-first actions](#confirm-first-actions-and-resume) · [Asking the user](#asking-the-user) ·
[Budgets](#budgets) · [Orchestrator bridge and flags](#orchestrator-bridge-and-per-agent-flags) ·
[Consent judges](#consent-judges) · [Clean up](#clean-up)

## Load agents

An agent is a folder. `discover` loads every `*/agent.yaml` under a root and checks each card
against your tools. A broken card excludes only that agent and its reason is kept.

```python
from agent_kit import runner
from agent_kit.agents.notes_curator.tools import NotesBackend, build_tools
from agent_kit.confirm import ConfirmGate
from agent_kit.consent import RuleConsentJudge
from agent_kit.fakes import ScriptedLLM
from agent_kit.handles import HandleStore
from agent_kit.llm import Final, ToolCall
from agent_kit.registry import BUILTIN_AGENTS, discover
from agent_kit.write_guard import WriteGuard

backend = NotesBackend(50)           # a fake Acme Workspace with 50 notes
tools = build_tools(backend)         # list_notes, save_note, archive_notes
registry = discover(BUILTIN_AGENTS, tools)
print(registry.names(), registry.errors)
```

`registry.errors` maps a folder name to why it was left out. Here `workflow_helper` is excluded
because its tools are not registered in this small registry. That is the fail-closed behaviour.

## Run a task

`runner.run` is the ReAct loop. It never raises: every failure is a `TaskResult` with
`status="failed"`. A real LLM adapter implements `AgentLLM`; the scripted fake plays back steps.

```python
agent = registry.agents["notes_curator"]
card = agent.card
gate = ConfirmGate()
guard = WriteGuard(tools, lambda tool, key: backend.note_exists(key), card.budget)

llm = ScriptedLLM([
    [ToolCall("save_note", {"title": "Weekly summary", "body": "Nothing urgent."})],
    Final("Saved a summary."),
])
result = await runner.run(
    card, runner.Task("task-1", "Summarise my notes"), llm, tools, HandleStore(), guard, gate,
    skill_text=agent.skill_text, version_of=backend.version_of,
)
print(result.status, result.message, [a.key for a in result.artifacts])
```

`artifacts` come from the write ledger: what the guard really saved, not what the model claims.

## Run as the chatting user

In a real product the chat user must only reach what they may reach. Pass a `ToolOpener` and a
`user` instead of ready-made tools. The runner opens the card's allow-list **once**, as that user,
and refuses to start if any tool is missing or forbidden. The model is never called in that case.

```python
from agent_kit.fakes import FakeBackendOpener

backends = {"alice": NotesBackend(3), "bob": NotesBackend(2)}
opener = FakeBackendOpener(
    lambda user: build_tools(backends[user]),                       # tools bound to that user's data
    {"alice": set(card.tools), "bob": {"list_notes", "save_note"}},  # bob may not archive
)

async def run_as(user, steps):
    return await runner.run(
        card, runner.Task(f"as-{user}", "tidy"), ScriptedLLM(steps), None, HandleStore(), guard, gate,
        opener=opener, user=user,
    )

ok = await run_as("alice", [[ToolCall("list_notes")], Final("done")])
refused = await run_as("bob", [Final("never reached")])
print(ok.status, "|", refused.status, refused.message)
```

`FakeBackendOpener` is the test double for a backend that enforces permissions. A real opener
would call your backend with the user's own credentials.

## Big results become handles

A tool that returns more than 20 rows is stored in a per-task SQLite table. The model sees a
summary (columns, row count, 3 sample rows) and queries with `sql_query` (one read-only SELECT)
or `jmes_query`.

```python
llm = ScriptedLLM([
    [ToolCall("list_notes")],
    [ToolCall("sql_query", {"query": "SELECT id, words FROM list_notes_1 ORDER BY words DESC LIMIT 2"})],
    Final("Found the two longest notes."),
])
await runner.run(card, runner.Task("task-2", "longest notes"), llm, tools, HandleStore(), guard, gate)
first_observation = [m["content"] for m in llm.seen[-1] if m["role"] == "tool"][0]
print(first_observation[:150])
```

The handle is named `<tool>_<call number>`. You can also use the store directly:

```python
store = HandleStore()
summary = store.put("rows", [{"n": i} for i in range(100)])
print(summary.row_count, store.sql("SELECT sum(n) AS total FROM rows")["rows"])
```

## Guarded writes

Tools listed under `guarded_writes` in the card keep their own name and schema, but a call goes
through `WriteGuard.save`: same-item check, save budget, validation, forward, existence check
after an unclear transport error, ledger. The second save of the same item is a no-op.

```python
llm = ScriptedLLM([
    [ToolCall("save_note", {"title": "Plan", "body": "v1"}),
     ToolCall("save_note", {"title": "Plan", "body": "v2"})],   # same item again
    Final("saved"),
])
before = backend.forward_calls
result = await runner.run(card, runner.Task("task-3", "save"), llm, tools, HandleStore(), guard, gate)
print("backend writes:", backend.forward_calls - before, "artifacts:", [a.key for a in result.artifacts])
```

The commit runs under `asyncio.shield`. If the turn is cancelled (for example by the deadline),
the commit is not abandoned: it finishes and is recorded. See [Limits](../README.md#limits-and-known-trade-offs)
for what this does not cover.

## Confirm-first actions and resume

Tools listed under `confirm_actions` never run in the model's turn. The runner stores the exact
arguments and returns `needs_input` with a question **written by code**. On the next turn you
call `runner.resume` with only the pending id and the user's reply. A judge reads the reply,
the version stamp is re-checked, and the stored arguments run.

```python
llm = ScriptedLLM([[ToolCall("archive_notes", {"ids": [1, 2]})], Final("waiting")])
asked = await runner.run(
    card, runner.Task("task-4", "archive"), llm, tools, HandleStore(), guard, gate,
    version_of=backend.version_of,
)
print(asked.status, "|", asked.message)

version = await backend.version_of("archive_notes", {})
answer = await runner.resume(
    card, asked.pending_action.id, "yes", RuleConsentJudge(), tools, gate, guard, "task-4",
    current_version=version,
)
print(answer.status, answer.message, [n["id"] for n in backend.notes if n["archived"]])
```

Other replies: "no" cancels; "yes but only one" is not a yes and cancels; if the data changed
since the question (`current_version` differs) you get `needs_input` with a fresh question.

## Asking the user

Every agent has a built-in `ask_user(question)` tool. It ends the run with `status="needs_input"`
and no pending action. To resume, start a new run whose task text carries the user's answer.

```python
llm = ScriptedLLM([[ToolCall("ask_user", {"question": "Which project?"})]])
asked = await runner.run(card, runner.Task("task-5", "tidy"), llm, tools, HandleStore(), guard, gate)
print(asked.status, "|", asked.message, "|", asked.pending_action)

llm = ScriptedLLM([Final("Tidied project Apollo.")])
followup = await runner.run(
    card, runner.Task("task-5b", "tidy. The user answered: project Apollo"), llm, tools,
    HandleStore(), guard, gate,
)
print(followup.status)
```

## Budgets

Every card has a budget. When one runs out the result is `failed` with the reason.

| Field | Counts | Default |
| --- | --- | --- |
| `max_steps` | model turns | 25 |
| `max_saves` | distinct guarded saves per task | 5 |
| `max_tool_calls` | every tool call the model makes, handle queries included | 60 |
| `max_backend_calls` | calls that reach the backend (registry tools, not handle queries, `ask_user` or confirm proposals) | 40 |
| `deadline_s` | wall-clock seconds for the whole run | 120 |

```python
from agent_kit.card import Budget

tight = card.model_copy(update={"budget": Budget(max_backend_calls=2)})
llm = ScriptedLLM([[ToolCall("list_notes")] * 5])
result = await runner.run(tight, runner.Task("task-6", "loop"), llm, tools, HandleStore(), guard, gate)
print(result.status, result.message)
```

## Orchestrator bridge and per-agent flags

`make_delegate_tool` builds one `delegate_to_specialist` tool for your orchestrator, with the
agent names and descriptions generated from the registry. The flag is one boolean per agent.
An agent with no flag is off. If nothing is enabled you get `None`, so the orchestrator's tools
are byte-for-byte what they were.

```python
from agent_kit.bridge import make_delegate_tool

async def dispatch(agent: str, task: str) -> str:
    return f"would run {agent}: {task}"

print(make_delegate_tool(registry, {"notes_curator": False}, dispatch))   # None: all off
tool = make_delegate_tool(registry, {"notes_curator": True}, dispatch)
print(tool.name, "|", await tool.ainvoke({"agent": "notes_curator", "task": "tidy"}))
```

## Consent judges

`ConsentJudge` is a Protocol: `judge(question, reply) -> "yes" | "no" | "unclear"`. Only a clear
yes confirms; an error, `unclear` or garbage cancels.

- `RuleConsentJudge`: deterministic word lists (English and Arabic). It ships here as a stand-in.
- `LLMConsentJudge(llm)`: asks a `TextLLM` at temperature 0 with the reply escaped as data.
  Here it is only exercised with a scripted LLM.

```python
from agent_kit.consent import LLMConsentJudge, RuleConsentJudge

class AlwaysYes:                       # a fake TextLLM; a real one calls a small, fast model
    async def complete(self, prompt: str, temperature: float = 0.0) -> str:
        return "Yes"

print(await RuleConsentJudge().judge("q", "نعم"), await RuleConsentJudge().judge("q", "yes but wait"))
print(await LLMConsentJudge(AlwaysYes()).judge("q", "yes"))
```

To grade a real judge, run it over `tests/evals/consent_cases.jsonl` (96 replies) and
`tests/evals/same_item_cases.jsonl` (14). A single false confirm is a blocker.

## Clean up

Call `end_task` once a task's result has been consumed, to free its ledger, locks and unanswered
pending actions.

```python
for task_id in ("task-1", "task-3", "task-4"):
    guard.end_task(task_id)
    gate.end_task(task_id)
print(guard.ledger("task-3"))
```
