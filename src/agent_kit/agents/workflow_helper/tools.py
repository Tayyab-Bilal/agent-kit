"""Demo tools for workflow_helper, backed by an in-memory fake Acme Workspace workflow store.

The fake has more tools than the agent may use: `delete_workflow` and `convert_to_template` exist
here, as they would on a real backend, but they are not on the card, so the agent cannot reach them.
"""

from __future__ import annotations

import copy
from typing import Any

from agent_kit.agents.workflow_helper.diagnose import diagnose, step_order
from agent_kit.registry import ToolRegistry


class WorkflowLocked(Exception):
    """The workflow's state does not allow this action (e.g. editing a published workflow)."""


def _step(step_id: str, kind: str, nxt: str | None, settings: dict[str, Any],
          required: list[str]) -> dict[str, Any]:
    return {"id": step_id, "kind": kind, "next": nxt, "settings": settings, "required": required}


class WorkflowBackend:
    def __init__(self) -> None:
        self.workflows: dict[str, dict[str, Any]] = {}
        self.runs: dict[str, list[dict[str, Any]]] = {}
        self.forward_calls = 0
        self._counter = 0
        self._add("Onboarding", "draft", [
            _step("s1", "on_new_member", "s2", {}, []),
            _step("s2", "create_task", "s3", {"project": "Welcome"}, ["project"]),
            _step("s3", "send_notification", None, {"channel": ""}, ["channel"]),  # left empty
            _step("s9", "archive_tasks", None, {}, []),  # nothing leads here
        ])
        self.runs["wf-1"] = [{"id": "r1", "status": "failed", "executed": ["s1", "s2"],
                              "failed_step": "s3", "error": "channel is empty"}]
        self._add("Weekly report", "draft", [_step("s1", "on_schedule", None, {}, [])])
        self._add("Weekly report (old)", "published", [_step("s1", "on_schedule", None, {}, [])])
        self._add("Invoice approval", "published", [
            _step("s1", "on_document_added", "s2", {}, []),
            _step("s2", "request_approval", None, {"approver": "finance"}, ["approver"]),
        ])

    def _add(self, name: str, status: str, steps: list[dict[str, Any]]) -> str:
        self._counter += 1
        wid = f"wf-{self._counter}"
        self.workflows[wid] = {"id": wid, "name": name, "status": status, "version": 1,
                               "trigger": steps[0]["id"], "steps": steps}
        self.runs.setdefault(wid, [])
        return wid

    def _get(self, workflow_id: str) -> dict[str, Any]:
        if workflow_id not in self.workflows:
            raise KeyError(f"no workflow {workflow_id!r}")
        return self.workflows[workflow_id]

    def _check(self, action: str, wf: dict[str, Any]) -> None:
        """Rules about state. Run when an action is proposed AND again when it executes."""
        if action == "edit_workflow" and wf["status"] == "published":
            raise WorkflowLocked("a published workflow cannot be edited; copy it or revert it first")
        if action == "publish_workflow" and wf["status"] == "published":
            raise WorkflowLocked("already published")
        if action == "revert_workflow" and wf["status"] != "published":
            raise WorkflowLocked("only a published workflow can be reverted to draft")

    @staticmethod
    def _apply(steps: list[dict[str, Any]], edits: dict[str, Any]) -> None:
        by_id = {s["id"]: s for s in steps}
        for path, value in edits.items():
            step_id, _, key = path.partition(".")
            if step_id not in by_id or not key:
                raise ValueError(f"unknown edit target {path!r}; use '<step id>.<setting>'")
            by_id[step_id]["settings"][key] = value

    # --- read tools -------------------------------------------------------------------------
    async def find_workflows(self, name: str) -> dict[str, Any]:
        needle = name.strip().casefold()
        hits = [w for w in self.workflows.values() if needle and needle in w["name"].casefold()]
        exact = [w for w in hits if w["name"].casefold() == needle]
        if len(exact) == 1:
            hits = exact  # an exact name beats partial matches
        matches = [{"id": w["id"], "name": w["name"], "status": w["status"]} for w in hits]
        outcome = {0: "none", 1: "one"}.get(len(matches), "several")
        hint = {"none": "Tell the user no workflow has that name.",
                "one": "Proceed with this workflow.",
                "several": "Call ask_user: list the names and ask which one they mean."}[outcome]
        return {"outcome": outcome, "matches": matches, "next": hint}

    async def diagnose_workflow(self, workflow_id: str) -> dict[str, Any]:
        return diagnose(self._get(workflow_id), self.runs.get(workflow_id, []))  # the pure function

    # --- guarded write ----------------------------------------------------------------------
    async def copy_workflow(
        self, source_id: str, new_name: str, edits: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """New workflow, new ids, links rewired, and the edits applied in the same save."""
        source = self._get(source_id)
        self.forward_calls += 1
        steps = copy.deepcopy(source["steps"])
        self._apply(steps, edits or {})
        self._counter += 1
        new_id = f"wf-{self._counter}"
        ids = {s["id"]: f"{new_id}.{s['id']}" for s in steps}
        for s in steps:
            s["id"], s["next"] = ids[s["id"]], ids.get(s["next"])
        self.workflows[new_id] = {"id": new_id, "name": new_name, "status": "draft", "version": 1,
                                  "trigger": ids[source["trigger"]], "steps": steps}
        self.runs[new_id] = []
        return {"id": new_id, "name": new_name, "status": "draft"}

    async def workflow_exists(self, tool: str, name: str) -> bool:
        return any(w["name"] == name for w in self.workflows.values())

    # --- confirm actions (only ever run after the user's own yes) ---------------------------
    async def edit_workflow(self, workflow_id: str, edits: dict[str, Any]) -> dict[str, Any]:
        wf = self._get(workflow_id)
        self._check("edit_workflow", wf)
        self._apply(wf["steps"], edits)
        wf["version"] += 1
        return {"id": workflow_id, "version": wf["version"]}

    async def publish_workflow(self, workflow_id: str) -> dict[str, Any]:
        wf = self._get(workflow_id)
        self._check("publish_workflow", wf)
        wf["status"], wf["version"] = "published", wf["version"] + 1
        return {"id": workflow_id, "status": "published"}

    async def revert_workflow(self, workflow_id: str) -> dict[str, Any]:
        wf = self._get(workflow_id)
        self._check("revert_workflow", wf)
        wf["status"], wf["version"] = "draft", wf["version"] + 1
        return {"id": workflow_id, "status": "draft"}

    async def run_workflow(self, workflow_id: str) -> dict[str, Any]:
        wf = self._get(workflow_id)
        by_id = {s["id"]: s for s in wf["steps"]}
        executed: list[str] = []
        run = {"id": f"r{len(self.runs[workflow_id]) + 1}", "status": "succeeded", "executed": executed,
               "failed_step": None, "error": ""}
        for step_id in step_order(wf):
            step = by_id[step_id]
            empty = [k for k in step["required"] if step["settings"].get(k) in ("", None)]
            if empty:
                run.update(status="failed", failed_step=step_id, error=f"{empty[0]} is empty")
                break
            executed.append(step_id)
        self.runs[workflow_id].append(run)
        return {"run_id": run["id"], "status": run["status"]}

    # --- tools the card does NOT list -------------------------------------------------------
    async def delete_workflow(self, workflow_id: str) -> dict[str, Any]:
        del self.workflows[workflow_id]
        return {"deleted": workflow_id}

    async def convert_to_template(self, workflow_id: str) -> dict[str, Any]:
        self._get(workflow_id)["status"] = "template"
        return {"id": workflow_id, "status": "template"}

    async def version_of(self, action: str, args: dict[str, Any]) -> str:
        """Version stamp, and the precheck: a refused action never becomes a question."""
        wf = self._get(args["workflow_id"])
        self._check(action, wf)
        return f"{wf['id']}@{wf['version']}"


def build_workflow_tools(backend: WorkflowBackend, registry: ToolRegistry | None = None) -> ToolRegistry:
    reg = registry or ToolRegistry()
    reg.register("find_workflows", backend.find_workflows, description="Find workflows by name")
    reg.register("diagnose_workflow", backend.diagnose_workflow,
                 description="Check a workflow and its last run; returns findings and risk")
    reg.register("copy_workflow", backend.copy_workflow, writes=True,
                 description="Copy a workflow under a new name, with optional setting edits")
    reg.register("edit_workflow", backend.edit_workflow, writes=True,
                 description="Change settings of a draft workflow")
    reg.register("publish_workflow", backend.publish_workflow, writes=True,
                 description="Publish a draft workflow")
    reg.register("revert_workflow", backend.revert_workflow, writes=True,
                 description="Move a published workflow back to draft")
    reg.register("run_workflow", backend.run_workflow, writes=True, description="Run a workflow now")
    reg.register("delete_workflow", backend.delete_workflow, writes=True,
                 description="Delete a workflow (not on any card)")
    reg.register("convert_to_template", backend.convert_to_template, writes=True,
                 description="Turn a workflow into a template (not on any card)")
    return reg
