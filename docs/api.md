# API reference

The public classes and functions, with signatures. Module paths are under `agent_kit`. For
walkthroughs see [usage.md](usage.md) and [writing-an-agent.md](writing-an-agent.md).

```python
import inspect

from agent_kit import runner
from agent_kit.bridge import make_delegate_tool
from agent_kit.card import AgentCard, Budget, GuardSpec
from agent_kit.confirm import ConfirmGate
from agent_kit.consent import LLMConsentJudge, RuleConsentJudge
from agent_kit.fakes import FakeBackendOpener, ScriptedLLM
from agent_kit.handles import HandleStore
from agent_kit.opener import OpenRefused, ToolOpener
from agent_kit.registry import AgentRegistry, ToolRegistry, discover, load_agent
from agent_kit.result import Artifact, PendingAction, TaskResult
from agent_kit.write_guard import TransportError, WriteGuard, WriteRefused

# The signatures below are the real ones; this block fails if a name here stops existing.
print(inspect.signature(runner.run))
```

## Cards (`card.py`)

All card models are strict: unknown keys raise a validation error.

| Name | Fields |
| --- | --- |
| `GuardSpec` | `tool: str`, `item_key: str` |
| `Budget` | `max_steps=25`, `max_saves=5`, `max_tool_calls=60`, `max_backend_calls=40`, `deadline_s=120` |
| `AgentCard` | `name`, `description`, `skill`, `tools`, `guarded_writes=[]`, `confirm_actions=[]`, `budget=Budget()` |
| `CardError(ValueError)` | A card broke a load-time rule. |

`AgentCard.validate_against(registry: ToolRegistry) -> None` raises `CardError` if a write tool on
the card is neither guarded nor confirm-gated.

## Tools and discovery (`registry.py`)

```text
ToolRegistry()
  .register(name, fn, *, writes=False, description="")   # fn is async, keyword arguments
  .schema(name) -> {"name", "description", "args": {param: {"type", "required"}}}
  .call(name, args: dict) -> Any                          # await it
  .check_args(name, args) -> None                         # TypeError if args do not fit fn
  .is_write(name) -> bool
  .spec(name) -> ToolSpec                                 # name, fn, writes, description
  .put(spec) / .copy() / .subset(names)                   # building blocks for guard and opener
  name in registry                                        # __contains__

discover(root: Path, tools: ToolRegistry) -> AgentRegistry
load_agent(folder: Path, tools: ToolRegistry) -> LoadedAgent     # raises CardError
AgentRegistry
  .agents: dict[str, LoadedAgent]                         # LoadedAgent(card, skill_text)
  .errors: dict[str, str]                                 # folder name -> why it was excluded
  .names() -> list[str]
  .enabled_names(flags: bool | Mapping[str, bool]) -> list[str]
BUILTIN_AGENTS: Path                                      # the folders shipped in agent_kit/agents
```

## Runner (`runner.py`)

```text
Task(id: str, text: str)

await run(card, task, llm, tools, handles, guard, gate, *,
          skill_text="", clock=time.monotonic, version_of=..., opener=None, user=None) -> TaskResult
await resume(card, pending_id, user_reply, judge, tools, gate, guard, task_id, *,
             current_version, opener=None, user=None) -> TaskResult
data_block(source: str, text: str) -> str                 # escapes text so it cannot pose as prompt
```

- `tools` may be `None` if you pass `opener` and `user`.
- `version_of(action, args) -> str` (async) gives the version stamp of a confirm action and is
  also its precheck: raising there means no question is asked.
- `run` and `resume` never raise. Failures are `TaskResult(status="failed")`.
- Built-in model tools, always available: `sql_query(query)`, `jmes_query(name, expression)` and
  `ask_user(question)`.

## Results (`result.py`)

| Type | Fields |
| --- | --- |
| `TaskResult` | `status: "completed" \| "needs_input" \| "failed"`, `message`, `artifacts: list[Artifact]`, `pending_action: PendingAction \| None` |
| `Artifact` | `kind`, `key`, `result: dict` (taken from the write ledger) |
| `PendingAction` | `id`, `action`, `args`, `version_stamp`, `question` (frozen) |

## Run as the user (`opener.py`, `fakes.py`)

```text
ToolOpener (Protocol)
  await open(card, user) -> ToolRegistry                  # exactly card.tools, bound to user
OpenRefused(Exception)                                    # a tool is missing or forbidden
FakeBackendOpener(build: Callable[[str], ToolRegistry], grants: Mapping[str, Collection[str]])
  .opened: list[str]                                      # who each open() was for
```

## Write guard (`write_guard.py`)

```text
WriteGuard(tools, exists: Callable[[str, str], Awaitable[bool]], budget: Budget, validate=None)
  await save(task_id, tool, item_key, payload, *, tools=None) -> dict
  expose(tools, specs: Iterable[GuardSpec], task_id) -> ToolRegistry   # same names and schemas
  ledger(task_id) -> list[LedgerEntry]                    # LedgerEntry(task_id, tool, item_key, result, kind)
  record(task_id, tool, item_key, result, kind) -> None
  end_task(task_id) -> None
TransportError(Exception)   # raise from a tool: the write may or may not have landed
WriteRefused(Exception)     # budget, validation, or a save that failed and did not land
```

## Confirm gate and consent (`confirm.py`, `consent.py`)

```text
ConfirmGate()
  propose(task_id, action, args, version) -> PendingAction
  await resolve(pending_id, task_id, user_reply, judge, current_version) -> Execute | Cancel | AskAgain
  question_for(action, args) -> str                       # static
  end_task(task_id) -> None

ConsentJudge (Protocol)       await judge(question, reply) -> "yes" | "no" | "unclear"
RuleConsentJudge()            deterministic stand-in (word lists, English and Arabic)
LLMConsentJudge(llm: TextLLM) temperature 0, reply escaped as data, bad output -> "unclear"
```

## Handles (`handles.py`)

```text
HandleStore(timeout_s=0.5)
  put(name, rows: list[dict]) -> HandleSummary            # name, columns, row_count, sample
  sql(query, timeout_s=None) -> {"columns", "rows", "truncated"}   # one read-only SELECT
  jmes(name, expression, timeout_s=None) -> dict          # runs in a forked child with limits
HandleError(Exception)
```

Limits: 200 rows and 64 KB per result, 0.5 s per query.

## LLM seams (`llm.py`, `fakes.py`)

```text
AgentLLM (Protocol)   await step(transcript, tools) -> list[ToolCall] | Final
TextLLM (Protocol)    await complete(prompt, temperature=0.0) -> str
ToolCall(name, args={})        Final(message, asserted_artifacts=[])   # asserted_artifacts is ignored
ScriptedLLM(script)            .seen (transcripts)  .tools_seen (schemas shown to the model)
```

## Bridge (`bridge.py`)

```text
make_delegate_tool(registry, enabled: bool | Mapping[str, bool], dispatch) -> StructuredTool | None
```

`enabled` is a master switch or one flag per agent (no flag = off). It returns `None` when no
agent is enabled. `dispatch(agent, task)` is your async function that runs the chosen agent.

## Demo agents (`agents/`)

| Agent | Tools |
| --- | --- |
| `notes_curator` | `list_notes` (read), `save_note` (guarded by `title`), `archive_notes` (confirm) |
| `workflow_helper` | `find_workflows`, `diagnose_workflow` (read); `copy_workflow` (guarded by `new_name`); `edit_workflow`, `publish_workflow`, `revert_workflow`, `run_workflow` (confirm) |

`agents/workflow_helper/diagnose.py` exposes the pure `diagnose(workflow, runs) -> dict` and
`step_order(workflow) -> list[str]`. The agent's `diagnose_workflow` tool calls the same function.
