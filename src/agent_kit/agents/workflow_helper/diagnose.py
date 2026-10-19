"""Pure workflow checks. No I/O, no clock, no randomness: same input, same answer.

Why it is its own module: the agent's `diagnose_workflow` tool and any plain caller (an HTTP
endpoint, a script, a test) call this one function, so the two cannot drift apart.

A workflow is a dict: {"id", "name", "status", "trigger", "steps": [{"id", "kind", "next",
"settings", "required"}]}. A run is {"id", "status", "executed": [step ids], "failed_step", "error"};
`runs` is oldest first.
"""

from __future__ import annotations

from typing import Any


def step_order(workflow: dict[str, Any]) -> list[str]:
    """Step ids in run order, starting at the trigger. Stops at a loop instead of spinning."""
    by_id = {s["id"]: s for s in workflow["steps"]}
    order: list[str] = []
    current = workflow["trigger"]
    while current in by_id and current not in order:
        order.append(current)
        current = by_id[current]["next"]
    return order


def diagnose(workflow: dict[str, Any], runs: list[dict[str, Any]]) -> dict[str, Any]:
    by_id = {s["id"]: s for s in workflow["steps"]}
    order = step_order(workflow)
    orphans = [s["id"] for s in workflow["steps"] if s["id"] not in order]
    findings: list[dict[str, Any]] = []

    for step_id in order:
        step = by_id[step_id]
        for key in step["required"]:
            if step["settings"].get(key) in ("", None):
                findings.append({"code": "empty_required_setting", "step": step_id,
                                 "message": f"step {step_id} needs setting {key!r} but it is empty"})
    for step_id in orphans:
        findings.append({"code": "unreachable_step", "step": step_id,
                         "message": f"step {step_id} can never run: nothing leads to it"})

    last_failure = None
    if runs and runs[-1]["status"] == "failed":
        run = runs[-1]
        if run["executed"]:
            where, text = run["failed_step"], f"the last run died on step {run['failed_step']}"
        else:
            where, text = None, "the last run failed before any step ran"
        last_failure = {"run_id": run["id"], "step": where, "error": run["error"]}
        findings.append({"code": "last_run_failed", "step": where, "message": f"{text}: {run['error']}"})

    blocking = {"empty_required_setting", "last_run_failed"}
    risk = "high" if any(f["code"] in blocking for f in findings) else "medium" if findings else "low"
    return {"steps_in_order": order, "orphans": orphans, "findings": findings, "risk": risk,
            "last_failure": last_failure}
