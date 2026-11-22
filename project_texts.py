"""AI Project Center pages: the words, symbols and small rules the Projects board, the Phases view,
the project page and the message box share (Maprisma's project_board.dart, project_page_parts.dart,
project_messages.dart and ai_project_center.dart, tasks 082, 088-096). GTK-free, so the rules can be
tested without a display; the pages (projects_page.py, project_detail_page.py, project_phases_page.py,
project_messages_view.py) build their widgets from these."""

from __future__ import annotations

import datetime as dt
import re

from i18n import _

# The board's filters, in the core's bucket order (project_stage.BUCKETS): code, label, symbol.
FILTERS = (
    ("running", "Running", "media-playback-start-symbolic"),
    ("attention", "Attention", "dialog-error-symbolic"),
    ("needs_you", "Needs you", "dialog-question-symbolic"),
    ("ready", "Ready", "object-select-symbolic"),
    ("drafts", "Drafts", "document-edit-symbolic"),
    ("finished", "Agents finished", "flag-symbolic"),
)
ALL_ICON = "view-app-grid-symbolic"

# The board's two views (task 090).
VIEWS = (("cards", "Cards", "view-grid-symbolic"), ("phases", "Phases", "view-columns-symbolic"))

# A project's importance (task 095): a mark; it never moves a card.
IMPORTANCES = (("high", "High", "go-up-symbolic"), ("medium", "Medium", "value-decrease-symbolic"),
               ("low", "Low", "go-down-symbolic"))
IMPORTANCE_ICON = "view-sort-descending-symbolic"

# The stage rail's stops, as the core names them (project_stage.STAGES).
STAGES = (("goal", "Goal"), ("plan", "Plan"), ("approval", "Approval"), ("ready", "Ready"), ("run", "Run"),
          ("finished", "Agents finished"))

# The project page's tabs, in Maprisma's order (index = the tab number the page keeps).
TABS = ("Goal", "Plan", "Team", "Activity", "Setup")
TAB_GOAL, TAB_PLAN, TAB_TEAM, TAB_ACTIVITY, TAB_SETUP = range(5)

ACTIVE_STATUSES = frozenset({"planning", "running", "reviewing", "pausing"})
# The statuses in which a message is accepted (Send now not while pausing; task 096).
MESSAGE_STATUSES = ACTIVE_STATUSES

_ACTIVITIES = {
    "stopping": "Stopping", "setting_up_team": "Setting up team", "planning": "Planning",
    "reviewing_plan": "Reviewing plan", "reviewing": "Reviewing", "running": "Running", "paused": "Paused",
    "interrupted": "Interrupted", "failed": "Failed", "needs_review": "Needs your review",
    "approve_again": "Approve again", "approved_blocked": "Approved · blocked",
    "ready_to_run": "Approved, ready to run", "agents_finished": "Agents finished",
}

_ISSUES = {
    "folder_missing": "The folder is missing", "folder_changed": "The folder has changed",
    "no_lead": "No lead agent", "empty_goal": "No goal yet", "agent_unavailable": "An agent is not available",
    "profile_invalid": "An agent profile cannot start", "runtime_stopped": "A local runtime is stopped",
    "model_unavailable": "A model is not available", "provider_unavailable": "A provider is not available",
    "runtime_required": "A model needs its local runtime",
}

_STATUSES = {
    "planning": "Planning", "running": "Running", "reviewing": "Reviewing", "pausing": "Pausing",
    "awaiting_review": "Needs review", "paused": "Paused", "interrupted": "Interrupted",
    "completed": "Completed", "failed": "Failed", "ready": "Ready",
}

_FAILURES = (
    (("project_changed",), "This project changed. Reload it before saving."),
    (("project_unknown",), "The project could not be found."),
    (("project_already_running",), "This project is already running."),
    (("project_plan_not_approved",), "Review and approve this plan before running."),
    (("project_folder_unavailable", "project_folder_changed"), "The working folder is unavailable or changed."),
    (("project_folder_busy",), "Another project is working in this folder."),
    (("project_lead_required",), "Choose exactly one lead agent."),
    (("project_brief_required",), "Enter a title, goal and existing folder."),
    (("project_agent_unavailable",), "A selected agent is unavailable."),
    (("project_setup_agent_omitted", "project_setup_runtime_omitted", "project_setup_exclusion_unexplained"),
     "The proposed setup omitted a requested agent or runtime. Revise the request or review the exclusions."),
    (("project_setup_invalid", "project_setup_proposal_unknown"),
     "The AI setup proposal could not be validated. Review the helper output and try again."),
    (("project_setup_model_unknown",), "A selected model or runtime is unavailable."),
    (("project_setup_helper_unavailable",), "No setup helper is available."),
    (("project_model_unavailable", "project_runtime_unavailable", "project_runtime_invalid",
      "project_model_unsupported", "project_runtime_model_unavailable", "project_provider_unsupported",
      "project_runtime_required"), "A selected model or runtime is unavailable."),
    (("project_access_unsupported",), "Choose a supported access mode before saving."),
    (("project_review_changes_first",), "Review existing changes before changing this setup."),
    (("project_still_pausing", "project_processes_still_running"),
     "Some agent processes are still stopping. Try again shortly."),
    (("project_setup_agent_failed",), "Team setup ended without a proposal. The project is unchanged."),
    (("project_not_running",), "The project is not running now. Your message was not sent."),
    (("project_message_empty",), "Write a message first."),
    (("project_message_too_long",),
     "The message is too long (at most 4096 bytes). Shorten it or send it in parts."),
    (("project_message_invalid",),
     "The message could not be sent: it contains characters that are not allowed."),
    (("project_messages_full",),
     "Too many messages are waiting. Remove one or wait until the next assignment takes them."),
    (("project_message_unknown",), "This message is no longer there."),
    (("project_message_delivered",), "This message was already given to an agent."),
    (("project_launch_withdrawn",), "Another action on this project came first."),
    (("project_not_started",), "The new agent did not start."),
    (("project_message_pending",),
     "This message is already in the prompt of an agent that is starting. It will be delivered there."),
    (("project_prompt_too_large",),
     "The prompt for the next agent would be too large. Shorten the goal, the plan or the waiting messages."),
    (("project_not_paused",), "The agent at work could not be paused."),
    (("project_capacity",), "Too many projects are running now. Continue when one has finished."),
    (("project_center_closed",), "The Project Center is closing."),
    (("project_message_not_continued",), "The project could not continue at once."),
    (("project_limit",), "There are 64 projects already. Delete one before you create another."),
    (("project_center_busy",), "Another AI Agents Map already holds the project list."),
    (("project_store_unavailable", "project_store_full", "project_store_corrupt"),
     "The saved project store is unavailable. Check the saved state before continuing."),
)
_FAILURE_BY_CODE = {code: text for codes, text in _FAILURES for code in codes}
FAILED = "The project operation failed."

CONTINUE_QUESTION = ("Continue from saved handoff?",
                     "Review existing changes and output first. This starts a new agent session with the saved "
                     "handoff; incomplete actions may already have changed files.",
                     "I reviewed the existing changes")
DELETE_QUESTION = ("Delete saved project?", "The saved project will be removed. Its working folder is kept.",
                   "Delete")
APPROVE_QUESTION = ("Approve this plan?",
                    "Review the plan, team, working folder and permissions before approving. Approval applies "
                    "to this saved plan version.", "Approve plan")


def activity(code) -> str:
    return _(_ACTIVITIES.get(code, "Draft"))


def second_line(code) -> str | None:
    """The card's second line (what is required next), or None."""
    if code == "check_changes_first":
        return _("Check the changes first")
    if code == "approve_again":
        return _("Approve again")
    return None


def issue(code) -> str:
    return _(_ISSUES.get(code, "Cannot start as it is"))


def status(code) -> str:
    return _(_STATUSES.get(code, "Draft"))


def stage_index(code) -> int:
    return next((i for i, (stage, _label) in enumerate(STAGES) if stage == code), -1)


def stage_label(code) -> str:
    index = stage_index(code)
    return _(STAGES[index][1]) if index >= 0 else ""


def bucket_icon(code) -> str:
    return next((icon for bucket, _label, icon in FILTERS if bucket == code), FILTERS[4][2])


def importance_of(code):
    """(code, label, icon) of a card's mark; None without one (an older store)."""
    return next((entry for entry in IMPORTANCES if entry[0] == code), None)


def role(code, planning: bool = False) -> str:
    """A member's role; an implementer works as a contributor while planning."""
    if code == "implementer" and planning:
        return _("Contributor")
    return _({"lead": "Lead agent", "implementer": "Implementer", "contributor": "Contributor",
              "reviewer": "Reviewer", "setup": "Setup helper"}.get(code, "Agent"))


def failure(error) -> str:
    """A refusal as the user reads it (Maprisma's _failure); a rule's refusal keeps its own words."""
    code = getattr(error, "code", None)
    if code == "rule_refused":
        message = str(error)
        return message if message and message != code else _("Settings › Rules do not allow this.")
    if code == "invalid":
        return _(FAILED)
    return _(_FAILURE_BY_CODE.get(code, FAILED)) if isinstance(code, str) else _(FAILED)


def failure_code(code: str) -> str:
    return failure(_Code(code))


class _Code(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def message_failure(error) -> str:
    """A refused message (task 096): what it means for the message, else as every other refusal."""
    code = getattr(error, "code", None)
    if code == "project_changed":
        return _("The project moved on to another run. Look at it again, then send your message.")
    if code == "project_store_full":
        return _("The project store is full: your message was not saved. The running work goes on.")
    return failure(error)


def waits(reason) -> str:
    """"Could not continue at once: why. It waits." (the answer and the message_waiting event alike)."""
    if reason in ("project_changed", "project_launch_withdrawn"):
        why = _("Another action on this project came first.")
    elif isinstance(reason, str) and reason:
        why = failure_code(reason)
    else:
        why = _("The project could not continue at once.")
    return _("The agent could not continue at once: {0} Your message waits for the next assignment.").format(why)


def not_continued(reason, message) -> str:
    """Why Send now did not continue at once, said for what became of the message."""
    if reason == "project_message_unknown" or message is None:
        return _("Your message was removed meanwhile, so the project did not continue with it.")
    if reason == "project_message_delivered" or (isinstance(message, dict) and message.get("state") == "delivered"):
        return _("Your message was already given to an agent meanwhile. The project did not continue a second time.")
    return waits(reason)


_CODE = re.compile(r"^project_[a-z_]+$")
_WAITING = re.compile(r"^The agent could not continue at once \(([a-z_]+)\)\. "
                      r"Your message waits for the next assignment\.$")


def event_text(event: dict) -> str:
    """An event as the user reads it: the core's English text, its machine codes in words."""
    text = str(event.get("text") or "")
    kind = event.get("type")
    roles = r"(lead|implementer|reviewer|setup|contributor)"
    if kind == "assignment":
        match = re.match(rf"^(.*): {roles}$", text)
        if match:
            return _("{0}: {1}").format(match.group(1), role(match.group(2)))
    if kind == "assignment_done":
        match = re.match(rf"^{roles} ended successfully\.$", text)
        if match:
            return _("{0} ended successfully.").format(role(match.group(1)))
    if kind == "message_delivered":
        match = re.match(rf"^Your message was given to (.*): {roles}\.$", text)
        if match:
            return _("Your message was given to {0}: {1}.").format(match.group(1), role(match.group(2)))
        match = re.match(rf"^(\d+) messages were given to (.*): {roles}\.$", text)
        if match:
            return _("{0} messages were given to {1}: {2}.").format(match.group(1), match.group(2),
                                                                    role(match.group(3)))
    if kind == "message_waiting":
        match = _WAITING.match(text)
        if match:
            return waits(match.group(1))
    if kind == "continue_failed" and _CODE.match(text):
        return _("The new session did not start: {0} The project is paused; check the changes, "
                 "then continue.").format(failure_code(text))
    if kind in ("setup_failed", "failed") and _CODE.match(text):
        return failure_code(text)
    return _(text)


def when(at, now: dt.datetime | None = None) -> str:
    """An ISO time as a short local time: today the time, earlier the date."""
    if not isinstance(at, str) or not at:
        return ""
    try:
        moment = dt.datetime.fromisoformat(at.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return ""
    now = now or dt.datetime.now().astimezone()
    if moment.date() == now.date():
        return moment.strftime("%H:%M")
    return moment.strftime("%d %b").lstrip("0")


def event_line(event) -> str:
    if not isinstance(event, dict):
        return ""
    text, at = event_text(event), when(event.get("at"))
    return f"{at} · {text}" if at else text


def short_path(path: str) -> str:
    parts = [p for p in str(path or "").split("/") if p]
    return path if len(parts) <= 2 else "…/" + "/".join(parts[-2:])


def initials(name: str) -> str:
    return "".join(word[0].upper() for word in str(name or "").split()[:2])


def names_agent(project: dict, command: str | None) -> bool:
    """Whether a board card's planning or execution names the agent `command` (None: every card)."""
    if command is None:
        return True
    return any(isinstance(m, dict) and m.get("agent") == command
               for phase in ("planning", "execution") for m in project.get(phase) or [])


def issue_fix(issue_: dict, map_fix: bool = True) -> str:
    """Where an issue is fixed (task 092): "map" only with a box to go to and a shell that can go
    there, else the project's "goal" or "team" tab."""
    fix, node = issue_.get("fix"), issue_.get("node")
    if fix == "map" and map_fix and isinstance(node, str) and node:
        return "map"
    return "goal" if fix == "goal" else "team"


def issue_fix_label(fix: str) -> str:
    return _({"map": "Fix on map", "goal": "Go to Goal"}.get(fix, "Go to Team"))


def collect_issues(project: dict) -> list:
    """The project's issues and every assignment's, each code and box once (the project page's list)."""
    seen, found = set(), []
    members = [m for phase in ("planning", "execution") for m in project.get(phase) or [] if isinstance(m, dict)]
    for item in list(project.get("issues") or []) + [i for m in members for i in m.get("issues") or []]:
        if isinstance(item, dict):
            key = (item.get("code"), item.get("node") or "")
            if key not in seen:
                seen.add(key)
                found.append(item)
    return found


def deletable(project: dict) -> bool:
    """The card's trash symbol (task 094): not while agents work, set up its team or stop."""
    return (project.get("status") not in ACTIVE_STATUSES and project.get("activity") != "setting_up_team"
            and project.get("processes_stopping") is not True)


def pause_label(project: dict) -> str | None:
    """The card's Pause (an assignment or the team setup runs) or Retry pause (stopping could not be
    confirmed); None when neither applies."""
    status_ = project.get("status")
    if status_ == "pausing":
        return _("Retry pause")
    active = project.get("active") if isinstance(project.get("active"), dict) else {}
    if (active.get("state") == "active" or project.get("activity") == "setting_up_team") \
            and project.get("processes_stopping") is not True:
        return _("Pause")
    return None


def team_nodes(project: dict, limit: int = 4):
    """The card's team strip: (shown [(member, state)], more). Execution (planning before a plan
    exists), at most `limit` members; the active, stopping, stopped or next one always among them; the
    team setup's helper first while a team is set up."""
    active = project.get("active") if isinstance(project.get("active"), dict) else None
    phase = (active or {}).get("phase") or ("planning" if project.get("stage") in ("goal", "plan") else "execution")
    setup = project.get("phase") == "setup" and isinstance(project.get("setup_agent"), dict)
    own = project.get(phase) or []
    members = own if own else project.get("execution" if phase == "planning" else "planning") or []
    team = []
    if setup:
        helper = project["setup_agent"]
        team.append({"agent": helper.get("agent"), "name": helper.get("name"), "role": "setup", "issues": []})
    team += [m for m in members if isinstance(m, dict)]
    if not team:
        return [], 0
    if setup:
        position = 0
        state = {"setting_up_team": "active", "stopping": "stopping", "paused": "stopped",
                 "interrupted": "stopped", "failed": "stopped"}.get(project.get("activity"))
    else:
        raw = active.get("position") if active and active.get("phase") == phase else None
        position = raw if type(raw) is int and 0 <= raw < len(team) else None
        state = active.get("state") if active else None
    indices = list(range(min(limit, len(team))))
    if position is not None and position not in indices:
        indices = indices[:limit - 1] + [position]
    shown = [(team[i], state if i == position else None) for i in indices]
    return shown, len(team) - len(shown)


def member_config(member: dict) -> str:
    """A team node's configuration: an explicit model, runtime or provider, else Agent default."""
    for key in ("model", "runtime", "provider"):
        value = member.get(key)
        if isinstance(value, str) and value:
            return value
    return _("Agent default")


def graph_config(member: dict) -> tuple[str, bool]:
    """The Team graph's target box: (text, explicit). Only an explicit runtime/provider is drawn as a
    target (never a guessed provider), with an explicit model beside it."""
    def text(value):
        return value if isinstance(value, str) and value else None
    target = " · ".join(t for t in (text(member.get("runtime")), text(member.get("provider"))) if t)
    parts = [target or _("Agent default")] + ([text(member.get("model"))] if text(member.get("model")) else [])
    return " · ".join(parts), bool(target)


def state_text(state) -> str | None:
    return {"active": _("Working"), "stopping": _("Stopping"), "stopped": _("Stopped"),
            "next": _("Next")}.get(state)


def state_icon(state) -> str | None:
    return {"active": "media-playback-start-symbolic", "stopping": "content-loading-symbolic",
            "stopped": "media-playback-stop-symbolic", "next": "media-skip-forward-symbolic"}.get(state)


def by_stage(projects: list) -> dict:
    """The projects per stage, in board order; an unknown stage counts as Goal (never lost)."""
    known = {code for code, _label in STAGES}
    groups = {code: [] for code, _label in STAGES}
    for project in projects:
        stage = project.get("stage")
        groups[stage if stage in known else "goal"].append(project)
    return groups


def messages_waiting(count: int) -> str:
    return _("1 message waiting") if count == 1 else _("{0} messages waiting").format(count)


def can_message(project) -> bool:
    """The message box is offered: the project's run is active (and not its team setup)."""
    return (isinstance(project, dict) and project.get("status") in MESSAGE_STATUSES
            and isinstance(project.get("run_id"), str) and project.get("phase") != "setup")


def message_state(message: dict, agent_name=None) -> str:
    """A message's line under its text: waiting with its time, or delivered with the agent, role and
    phase it went to."""
    if message.get("state") == "delivered":
        to = message.get("delivered_to")
        at = when(message.get("delivered_at") or message.get("at"))
        if not isinstance(to, dict):
            return " · ".join(p for p in (_("Delivered"), at) if p)
        agent = str(to.get("agent") or "")
        who = " · ".join(p for p in ((agent_name(agent) if agent_name and agent else agent),
                                     role(to.get("role"), planning=to.get("phase") == "planning")
                                     if to.get("role") is not None else "") if p)
        phase = _("Planning") if to.get("phase") == "planning" else _("Execution")
        return " · ".join(p for p in (_("Delivered to {0} ({1})").format(who, phase), at) if p)
    return " · ".join(p for p in (_("Waiting for the next assignment"), when(message.get("at"))) if p)


def footer_actions(project, plan_text: str, dirty: bool, running: bool, approved: bool, has_team: bool) -> list:
    """The project page's footer: [(label, operation, enabled)] in Maprisma's order."""
    status_ = project.get("status") if isinstance(project, dict) else None
    actions = [
        (_("Save project"), "save", not running and (dirty or project is None)),
        (_("Draft plan with AI"), "plan", not running and has_team),
        (_("Approve plan"), "review", not running and bool(plan_text.strip()) and (not approved or status_ == "completed")),
        (_("Run project"), "run", not running and approved and status_ == "ready"),
    ]
    if running:
        actions.append((_("Retry pause") if status_ == "pausing" else _("Pause project"), "pause", True))
    if status_ in ("paused", "interrupted", "failed"):
        actions.append((_("Continue project"), "continue", not dirty))
    if project is not None:
        actions.append((_("Delete project"), "delete", not running))
    return actions


def primary_action(actions: list, dirty: bool):
    """The one action a narrow footer keeps (the others go under More): an edit saves first."""
    preferred = (["save"] if dirty else []) + ["pause", "continue", "run", "review", "save", "plan"]
    for operation in preferred:
        for action in actions:
            if action[1] == operation and action[2]:
                return action
    return None
