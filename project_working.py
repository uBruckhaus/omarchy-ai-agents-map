"""AI Agents Map: show that the AI is working (Maprisma's project_working.dart, task 098).

While a project's agent drafts or checks the plan, works, reviews, sets up the team or is being
stopped, the project page shows a strip under its tabs (a moving bar, what happens, who does it, the
step, for how long, the agent's latest output line) and the board card a thin moving bar.

The project view (`get`, or a change's answer) decides whether an agent works: it is at least as
fresh as the board card, which only says who and which step."""

from __future__ import annotations

import re
from datetime import datetime, timezone

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402

import ai_core  # noqa: E402
import project_parts as parts  # noqa: E402
from i18n import _  # noqa: E402

# The moving bar pulses (tests switch it off: no timers without a main loop).
ANIMATE = True

# Events that end or start an attempt or an assignment.
_BOUNDARIES = frozenset({"started", "continued", "assignment_done", "finished", "paused", "failed",
                         "interrupted", "continue_failed", "setup_started", "setup_failed", "setup_paused",
                         "setup_interrupted", "setup_applied"})

_ROLES = {"lead": "Lead agent", "implementer": "Implementer", "contributor": "Contributor",
          "reviewer": "Reviewer"}


def _time(value) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def project_work(card, project) -> dict | None:
    """What the strip shows, or None while no agent works: headline, detail ("Agent · role · step N
    of M"), output (the stored output it belongs to), key (the current dispatch, None before the
    attempt's first one) and since (when it began)."""
    if not isinstance(project, dict):
        return None
    status = project.get("status")
    phase = project.get("phase")
    card = card if isinstance(card, dict) else None
    active = card.get("active") if card is not None and isinstance(card.get("active"), dict) else None
    stopping = status == "pausing" or project.get("processes_stopping") is True
    running = status in ("planning", "running", "reviewing")
    if not running and not stopping:
        return None
    setup = phase == "setup"
    # The current attempt's latest dispatch.
    started = boundary = None
    for event in reversed(project.get("events") or []):
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == ("setup_started" if setup else "assignment"):
            started = event
            break
        if kind in _BOUNDARIES:
            if kind in ("started", "continued"):
                boundary = _time(event.get("at"))
            break
    # Who: the card's member at work, else the event's "Agent: role". The card counts only while its
    # assignment is the one at work (it may be from before a change: "next", "stopped", another phase,
    # another member).
    name = role = None
    position, count = -1, 0
    card_phase = active.get("phase") if active else None
    current = (not setup and active is not None and active.get("state") in ("active", "stopping")
               and (phase is None or card_phase == ("execution" if phase == "running" else phase))
               and (type(project.get("next_member")) is not int or project.get("next_member") == active.get("position")))
    where = card_phase if current else None
    if card is not None and where is not None and type(active.get("position")) is int:
        position = active["position"]
        members = card.get(where) if isinstance(card.get(where), list) else []
        count = len(members)
        member = next((m for m in members if isinstance(m, dict) and m.get("position") == position), None)
        if member is not None:
            name = str(member.get("name") or member.get("agent") or "")
            role = member.get("role") if isinstance(member.get("role"), str) else None
    if setup:
        # The project has the helper's id, the card also its name (when it is the same helper).
        helper = project.get("setup_agent")
        shown = card.get("setup_agent") if card is not None else None
        if isinstance(shown, dict) and (helper is None or shown.get("agent") == helper):
            name = str(shown.get("name") or shown.get("agent") or "")
        elif isinstance(helper, str):
            name = helper
        role = None
    elif name is None and started is not None and isinstance(started.get("text"), str):
        text = started["text"]
        colon = text.rfind(": ")
        if colon > 0:
            name, role = text[:colon], text[colon + 2:]
    reviewer = role == "reviewer" or status == "reviewing"
    if stopping:
        headline = "Stopping the agent"
    elif setup:
        headline = "AI is setting up the team"
    elif (where or phase) == "planning":
        headline = "AI is checking the plan" if reviewer else "AI is drafting the plan"
    else:
        headline = "AI is reviewing the changes" if reviewer else "AI is working on the project"
    detail = []
    if name and name.strip():
        detail.append(name)
    if role is not None and not setup:
        detail.append(_(_ROLES.get(role, "Agent")))
    if position >= 0 and count > 1:
        detail.append(_("step {0} of {1}").format(position + 1, count))
    return {"headline": _(headline), "detail": " · ".join(detail) or None,
            "output": str(project.get("setup_output" if setup else "output") or ""),
            "key": f"{started.get('type')}@{started.get('at')}" if started is not None else None,
            "since": _time(started.get("at")) if started is not None and _time(started.get("at")) else boundary}


_ANSI = re.compile(r"\x1b(?:\[[0-9;?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")


def last_output_line(output: str) -> str | None:
    """The agent's latest non-empty output line, without terminal codes."""
    for line in reversed(re.split(r"[\r\n]+", _ANSI.sub("", output))):
        text = line.strip()
        if text:
            return text[:300] + "…" if len(text) > 300 else text
    return None


def working_for(since: datetime | None, now: datetime) -> str:
    """"for 3 min" from `since` to `now`; "just started" in the first minute."""
    if since is None:
        return ""
    minutes = int((now - since).total_seconds() // 60)
    if minutes < 1:
        return _("just started")
    if minutes < 60:
        return _("for {0} min").format(minutes)
    return _("for {0} h {1} min").format(minutes // 60, minutes % 60)


def working_card(card) -> bool:
    """Whether a board card's bar moves: an agent works or stops there."""
    if not isinstance(card, dict):
        return False
    active = card.get("active") if isinstance(card.get("active"), dict) else {}
    return (active.get("state") in ("active", "stopping") or card.get("activity") in ("setting_up_team", "stopping")
            or card.get("processes_stopping") is True)


class WorkingBar(Gtk.ProgressBar):
    """A thin moving bar; full and still when the desktop asks for no animations."""

    def __init__(self):
        super().__init__(hexpand=True)
        self.add_css_class("am-working-bar")
        self._timer = 0
        self.set_fraction(1.0)
        self.connect("map", lambda *_a: self._start())
        self.connect("unmap", lambda *_a: self._stop())

    @staticmethod
    def animated() -> bool:
        settings = Gtk.Settings.get_default()
        return ANIMATE and (settings is None or settings.get_property("gtk-enable-animations"))

    def _start(self):
        if self._timer or not self.animated():
            return
        self.set_pulse_step(0.08)
        self._timer = GLib.timeout_add(120, self._pulse)

    def _pulse(self):
        self.pulse()
        return GLib.SOURCE_CONTINUE

    def _stop(self):
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0
        self.set_fraction(1.0)


class OutputBaseline:
    """Which output line the strip shows. The stored output is the previous agent's until the current
    one writes (the center keeps it on purpose, e.g. as evidence of interrupted work), so a line shows
    only once it differs from what was stored when the current dispatch was first seen (after an
    attempt's start with no dispatch: from the output seen then)."""

    def __init__(self):
        self.reset(None)

    def reset(self, project_id):
        self.project_id = project_id
        self.seen = False
        self.key = self.baseline = self.last = None
        self.last_setup = False

    def line(self, work: dict | None, project_id) -> str | None:
        if work is None or project_id != self.project_id:
            self.reset(project_id)
        if work is None:
            return None
        # Team setup writes setup_output, the agents output.
        setup = bool(work["key"] and work["key"].startswith("setup_started@"))
        if not self.seen or (work["key"] != self.key and setup != self.last_setup):
            self.seen = True
            self.key = work["key"]
            self.baseline = work["output"]
        elif work["key"] != self.key:
            # After no dispatch (an attempt's start) the output seen then is not the new agent's; after
            # another agent its final output stays stored until the new one writes.
            self.baseline = self.last if self.key is None else work["output"]
            self.key = work["key"]
        self.last = work["output"]
        self.last_setup = setup
        if work["key"] is None or work["output"] == self.baseline:
            return None
        return last_output_line(work["output"])


class ProjectWorking:
    """The project page's strip under the tabs."""

    def __init__(self):
        self.root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.root.add_css_class("am-working")
        self.root.set_visible(False)
        self.bar = WorkingBar()
        self.root.append(self.bar)
        self.headline = parts.label("", ("heading",), wrap=True)
        self.detail = parts.label("", ("am-project-small", "am-project-dim"), wrap=True)
        self.output = parts.label("", ("am-project-small", "monospace"))
        for widget in (self.headline, self.detail, self.output):
            widget.set_margin_start(ai_core.GAP_TIGHT)
            widget.set_margin_end(ai_core.GAP_TIGHT)
            self.root.append(widget)
        self.baseline = OutputBaseline()

    def render(self, card, project, compact: bool = False, now: datetime | None = None):
        work = project_work(card, project)
        line = self.baseline.line(work, project.get("id") if isinstance(project, dict) else None)
        if work is None:
            self.root.set_visible(False)
            return
        elapsed = working_for(work["since"], now or datetime.now(timezone.utc))
        detail = " · ".join(x for x in (work["detail"], elapsed) if x)
        self.root.set_visible(True)
        parts.set_accessible(self.root, work["headline"])
        self.headline.set_label(work["headline"] + "…")
        self.detail.set_label(detail)
        self.detail.set_visible(bool(detail))
        self.output.set_label(line or "")
        self.output.set_visible(bool(line) and not compact)
