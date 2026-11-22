"""AI Agents Map: the Projects board's Phases view (Maprisma's project_phases.dart, task 090).

The six stages as columns (Goal, Plan, Approval, Ready, Run, Agents finished), each with its count
and a compact card per project in the board's order; narrower than six readable columns, a stacked
list per stage (empty stages left out). Navigation only: a card opens its project (click, Enter,
Space); nothing here changes a project."""

from __future__ import annotations

import json

from gi.repository import Gtk

import ai_core
import project_parts as parts
import project_texts as texts
from i18n import _


class PhasesView:
    def __init__(self, on_open):
        self.on_open = on_open
        self.cards: dict = {}            # id -> its button
        self.shown = None
        self.root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, vexpand=True)

    def render(self, projects: list, wide: bool):
        key = json.dumps([[(p.get("id"), p.get("title"), p.get("stage"), p.get("bucket"), p.get("activity"),
                            p.get("second")) for p in projects], wide], default=str)
        if key == self.shown:
            return
        self.shown = key
        focused = self.focused_id()
        parts.clear(self.root)
        self.cards = {}
        groups = texts.by_stage(projects)
        if not projects:
            self.root.append(parts.label(_("No projects here."), xalign=0.5))
            return
        if wide:
            columns = Gtk.Box(spacing=ai_core.GAP_INLINE, homogeneous=True, vexpand=True)
            for code, name in texts.STAGES:
                column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP_TIGHT)
                column.add_css_class("am-phase-column")
                column.append(self.header(code, _(name), len(groups[code])))
                cards = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP_TIGHT)
                for project in groups[code]:
                    cards.append(self.card(project, _(name)))
                scroller = Gtk.ScrolledWindow(vexpand=True)
                scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
                scroller.set_child(cards)
                column.append(scroller)
                columns.append(column)
            self.root.append(columns)
        else:
            stacked = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP_TIGHT)
            for code, name in texts.STAGES:
                if not groups[code]:
                    continue
                stacked.append(self.header(code, _(name), len(groups[code])))
                for project in groups[code]:
                    stacked.append(self.card(project, _(name)))
            scroller = Gtk.ScrolledWindow(vexpand=True)
            scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            scroller.set_child(stacked)
            self.root.append(scroller)
        if focused is not None:
            self.focus(focused)

    def header(self, code: str, name: str, count: int) -> Gtk.Widget:
        row = Gtk.Box(spacing=ai_core.GAP_TIGHT)
        title = parts.label(name, ("heading",))
        title.set_hexpand(True)
        row.append(title)
        row.append(parts.label(str(count), ("am-project-dim",)))
        row.stage_code = code
        parts.set_accessible(row, f"{name}, " + _("{0} projects").format(count))
        return row

    def card(self, project: dict, stage: str) -> Gtk.Widget:
        """A compact card: title, bucket symbol and activity, the second line. Opens its project."""
        title = str(project.get("title") or "")
        activity = texts.activity(project.get("activity"))
        second = texts.second_line(project.get("second"))
        button = Gtk.Button()
        button.add_css_class("am-phase-card")
        button.add_css_class("flat")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        name = parts.label(title, ("am-project-title",), lines=2)
        name.set_wrap(True)
        box.append(name)
        line = Gtk.Box(spacing=4)
        line.append(parts.icon(texts.bucket_icon(project.get("bucket")), 12))
        line.append(parts.label(activity, ("am-project-small",), wrap=True))
        box.append(line)
        if second:
            box.append(parts.label(second, ("am-project-small", "am-project-error"), wrap=True))
        button.set_child(box)
        parts.set_accessible(button, ", ".join(p for p in (title, stage, activity, second) if p))
        button.connect("clicked", lambda *_a: self.on_open(project))
        button.project_id = str(project.get("id"))
        self.cards[button.project_id] = button
        return button

    def focused_id(self):
        root = self.root.get_root()
        focus = root.get_focus() if root is not None else None
        while focus is not None:
            if getattr(focus, "project_id", None) and focus in self.cards.values():
                return focus.project_id
            focus = focus.get_parent()
        return None

    def focus(self, pid) -> bool:
        button = self.cards.get(pid)
        return bool(button is not None and button.grab_focus())

    def focus_first(self) -> bool:
        return any(button.grab_focus() for button in list(self.cards.values())[:1])
