"""Where a project is and what it does now, derived from its stored record (Maprisma's
project_stage.dart, docs/plan-main-layout.md "Status, stages and filters"). Pure: the header
counts, the board and the filters all use this one function. GTK-free.
"""

from __future__ import annotations

from dataclasses import dataclass

# The stage on the rail.
STAGES = ("goal", "plan", "approval", "ready", "run", "finished")
# The filter a project counts in; mutually exclusive, checked in this order (wire codes).
BUCKETS = ("running", "attention", "needs_you", "ready", "drafts", "finished")

_STOPPED = {"reviewing", "pausing", "paused", "interrupted", "failed"}


@dataclass(frozen=True)
class ProjectFacts:
    """What the core knows besides the record itself."""
    approval_valid: bool = False      # the approval matches the plan, the teams and the launch facts now
    processes_stopping: bool = False  # a supervisor still owns processes of this project
    launch_blocked: bool = False      # a valid approval whose launch would fail now (fix needs no new approval)


@dataclass(frozen=True)
class ProjectState:
    """The derived view of one project; all values are stable machine codes."""
    stage: str
    activity: str
    second: str | None
    bucket: str

    def to_json(self) -> dict:
        return {"stage": self.stage, "activity": self.activity, "second": self.second, "bucket": self.bucket}


def _stage(status: str, phase: str, record: dict, approval_valid: bool) -> str:
    if phase == "setup":
        # The stage the team setup started from; without one: Goal when there is no plan yet, else Approval.
        back = record.get("setup_return")
        came = back.get("status") if isinstance(back, dict) else None
        came_phase = back.get("phase") if isinstance(back, dict) else None
        if isinstance(came, str) and isinstance(came_phase, str) and came_phase != "setup":
            return _stage(came, came_phase, {"plan": record.get("plan")}, approval_valid)
        plan = record.get("plan")
        return "approval" if isinstance(plan, str) and plan.strip() else "goal"
    if status == "draft":
        return "goal"
    if status == "planning":
        return "plan"
    if status == "awaiting_review":
        return "approval"
    if status == "ready":
        return "ready" if approval_valid else "approval"
    if status == "running":
        return "run"
    if status == "completed":
        return "finished"
    if status in _STOPPED:
        return "run" if phase == "running" else "plan"
    return "goal"


def project_state(record: dict, facts: ProjectFacts = ProjectFacts()) -> ProjectState:
    """The stage, activity label, second line and filter bucket of `record`."""
    status = str(record.get("status") or "draft")
    phase = str(record.get("phase") or "planning")
    reconcile = record.get("needs_reconciliation") is True
    stage = _stage(status, phase, record, facts.approval_valid)
    stopping = status == "pausing" or facts.processes_stopping
    active_setup = phase == "setup" and status == "planning"
    stopped_run = phase == "running" and status in ("paused", "interrupted", "failed")
    approve_again = not facts.approval_valid and (status == "ready" or stopped_run)
    blocked = status == "ready" and facts.approval_valid and facts.launch_blocked

    if stopping:
        activity = "stopping"
    elif active_setup:
        activity = "setting_up_team"
    elif status == "reviewing":
        activity = "reviewing" if phase == "running" else "reviewing_plan"
    elif status == "ready":
        activity = "approve_again" if not facts.approval_valid else "approved_blocked" if blocked \
            else "ready_to_run"
    else:
        activity = {"planning": "planning", "running": "running", "paused": "paused",
                    "interrupted": "interrupted", "failed": "failed", "awaiting_review": "needs_review",
                    "completed": "agents_finished"}.get(status, "draft")

    # The second line: what is required next, without hiding the state.
    second = "check_changes_first" if reconcile else \
        "approve_again" if approve_again and activity != "approve_again" else None

    if stopping or active_setup or status in ("planning", "running", "reviewing"):
        bucket = "running"
    elif status in ("interrupted", "failed"):
        bucket = "attention"
    elif status in ("awaiting_review", "paused") or (reconcile and status in ("ready", "completed")) \
            or approve_again or blocked:
        bucket = "needs_you"
    elif status == "ready":
        bucket = "ready"
    elif status == "completed":
        bucket = "finished"
    else:
        bucket = "drafts"
    return ProjectState(stage, activity, second, bucket)
