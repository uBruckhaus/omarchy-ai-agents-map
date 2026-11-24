"""AI Agents Map: an open project over the Projects board (Maprisma's project page in
ai_project_center.dart and project_page_parts.dart, tasks 082, 088, 089, 092, 094-096).

The breadcrumb "Projects › <title>" leads back to the board (unsaved changes ask first); under it the
large stage rail, the tabs Goal, Plan, Team (with the team graph), Activity (the message box, the
agents' output, the saved handoff and the events) and Setup (a team in plain language), and a footer
with the actions for the project's state (narrow: the main one, the others under More). The state
lives in projects_controller.ProjectsController; this module draws it."""

from __future__ import annotations

import json

from gi.repository import Gio, GLib, Gtk, Pango

import ai_core
import folder_picker
import project_parts as parts
import project_texts as texts
from i18n import _
from project_messages_view import MessagesView
from project_working import ProjectWorking

MAPRISMA_URL = "https://github.com/maprisma/maprisma"     # scheduled runs live there (a hint, a link)

NARROW = 600          # the tabs become a list, the footer keeps one action
GRAPH_BESIDE = 900
SHORT = 460           # compact also in a window lower than this    # the Team graph next to the form from this width, else below it


def text_view(lines: int, on_change) -> Gtk.TextView:
    view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, accepts_tab=False, top_margin=6, bottom_margin=6,
                        left_margin=8, right_margin=8)
    view.add_css_class("am-prompt")
    view.set_size_request(-1, 22 * lines)
    view.get_buffer().connect("changed", lambda buffer: on_change(
        buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)))
    return view


def view_text(view: Gtk.TextView) -> str:
    buffer = view.get_buffer()
    return buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)


def segmented(choices, selected, on_choose, sensitive: bool = True) -> Gtk.Widget:
    """Two or three choices side by side (Planning | Execution, High | Medium | Low)."""
    box = Gtk.Box()
    box.add_css_class("am-segmented")
    first = None
    for value, text in choices:
        button = Gtk.ToggleButton(label=text, active=value == selected)
        button.add_css_class("am-segment")
        button.add_css_class("flat")
        if first is None:
            first = button
        else:
            button.set_group(first)
        button.set_sensitive(sensitive)
        button.connect("toggled", lambda b, value=value: b.get_active() and value != selected and on_choose(value))
        box.append(button)
    return box


def dropdown(items, selected, on_choose, sensitive: bool = True, missing: str | None = None) -> Gtk.DropDown:
    """items: [(value, text, enabled)]. A disabled item is shown dimmed and cannot be chosen (the
    choice goes back); a selected value that is not among the items (a missing agent, a model not
    chosen yet) is shown as its own disabled entry (`missing`: its text), never as the first item."""
    items = list(items)
    values = [value for value, _text, _on in items]
    if selected not in values:
        items.insert(0, (selected, missing if missing is not None else str(selected or "—"), False))
        values.insert(0, selected)
    names = Gtk.StringList.new([text for _value, text, _on in items])
    factory = Gtk.SignalListItemFactory()

    def setup(_factory, item):
        text = Gtk.Label(xalign=0)
        text.set_ellipsize(Pango.EllipsizeMode.END)
        text.set_max_width_chars(28)
        item.set_child(text)

    def bind(_factory, item):
        index = item.get_position()
        item.get_child().set_label(item.get_item().get_string())
        enabled = 0 <= index < len(items) and items[index][2]
        item.get_child().set_sensitive(enabled)
        if hasattr(item, "set_selectable"):
            item.set_selectable(enabled)
            item.set_activatable(enabled)
    factory.connect("setup", setup)
    factory.connect("bind", bind)
    drop = Gtk.DropDown(model=names, factory=factory)
    drop.add_css_class("am-project-drop")       # the size of the page's other controls, not bold
    current = values.index(selected)
    drop.set_selected(current)
    drop.set_sensitive(sensitive)

    def chosen(*_a):
        index = drop.get_selected()
        if index == current or not 0 <= index < len(items):
            return
        if not items[index][2]:
            GLib.idle_add(lambda: drop.set_selected(current) and False)    # not applied: shown back
            return
        on_choose(values[index])
    drop.connect("notify::selected", chosen)
    return drop


class ProjectDetail:
    def __init__(self, ctrl, colors, host):
        self.ctrl, self.colors, self.host = ctrl, colors, host
        self.syncing = False
        self.width = 1000
        self.shown_adopt = None
        self.shown_team = None
        self.shown_card = None
        self.shown_tabs = None
        self.height = 1000
        self.shown_footer = self.shown_importance = self.shown_graph = None
        self.root = parts.SizeBox(self.on_size, orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP_TIGHT,
                              hexpand=True, vexpand=True)
        crumb = Gtk.Box(spacing=ai_core.GAP_TIGHT)
        self.breadcrumb = Gtk.Button(label=_("Projects"), tooltip_text=_("Back to the board (Esc)"))
        self.breadcrumb.add_css_class("flat")
        self.breadcrumb.connect("clicked", lambda *_a: self.back())
        crumb.append(self.breadcrumb)
        crumb.append(parts.icon("go-next-symbolic", 14))
        self.title = parts.label("", ("am-project-title",))
        self.title.set_hexpand(True)
        crumb.append(self.title)
        self.status_line = parts.label("", ("am-project-dim",))
        crumb.append(self.status_line)
        self.root.append(crumb)
        self.rail = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.root.append(self.rail)
        self.tabs = Gtk.Box(spacing=4)
        self.root.append(self.tabs)
        self.root.append(Gtk.Separator())
        # While an agent works: what happens, on every tab (Maprisma task 098).
        self.working = ProjectWorking()
        self.root.append(self.working.root)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
        for side in ("start", "end", "top", "bottom"):
            getattr(body, f"set_margin_{side}")(ai_core.GAP_TIGHT)
        self.issues = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        body.append(self.issues)
        # Task 104: the steps this team can't do (before approval), and the steps a run left undone.
        self.access = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP_TIGHT)
        body.append(self.access)
        self.shown_access = None
        self.stopping = parts.label(_("Some agent processes are still stopping. Try again shortly."), wrap=True)
        body.append(self.stopping)
        self.error = parts.label("", ("am-project-error",), wrap=True)
        body.append(self.error)
        self.pages = Gtk.Stack(vhomogeneous=False, hhomogeneous=False)
        self.pages.add_named(self.build_goal(), "0")
        self.pages.add_named(self.build_plan(), "1")
        self.team_holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.pages.add_named(self.team_holder, "2")
        self.pages.add_named(self.build_activity(), "3")
        self.setup_holder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP_TIGHT)
        self.pages.add_named(self.setup_holder, "4")
        body.append(self.pages)
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(body)
        self.root.append(scroller)
        self.root.append(Gtk.Separator())
        self.footer = Gtk.Box(spacing=ai_core.GAP_INLINE)
        self.root.append(self.footer)

    def on_size(self, width: int, height: int):
        if self.ctrl.disposed:
            return
        before = (self.narrow, self.width >= GRAPH_BESIDE)
        self.width, self.height = width, height
        if before != (self.narrow, self.width >= GRAPH_BESIDE):
            self.shown_tabs = self.shown_card = self.shown_team = self.shown_graph = None
            parts.clear(self.team_holder)
            self.shown_footer = self.shown_importance = None
            if self.ctrl.open:
                self.render()

    def on_width(self, width: int):
        self.on_size(width, self.height)

    @property
    def narrow(self) -> bool:
        """Compact when narrow, and also when low (a wide but short window keeps room for the body)."""
        return self.width < NARROW or self.height < SHORT

    def back(self):
        if self.ctrl.busy:
            self.host.toast(_("Wait for the current step"), 4)
            return
        self.ctrl.back_to_board()

    # --- tabs ----------------------------------------------------------------------------------------

    def build_goal(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP_TIGHT)
        box.append(parts.label(_("Project title"), ("am-section-heading",)))
        self.title_entry = Gtk.Entry()
        self.title_entry.connect("changed", lambda e: self.edit("title", e.get_text()))
        box.append(self.title_entry)
        box.append(parts.label(_("Project goal"), ("am-section-heading",)))
        self.brief_view = text_view(5, lambda text: self.edit("brief", text))
        box.append(self.brief_view)
        box.append(parts.label(_("Working folder"), ("am-section-heading",)))
        self.path_entry = Gtk.Entry()
        self.path_entry.connect("changed", lambda e: self.edit("path", e.get_text()))
        box.append(self.path_entry)
        self.folder_button = Gtk.Button(halign=Gtk.Align.END)
        content = Gtk.Box(spacing=4)
        content.append(parts.icon("folder-open-symbolic"))
        content.append(Gtk.Label(label=_("Choose folder")))
        self.folder_button.set_child(content)
        self.folder_button.add_css_class("am-small-btn")
        self.folder_button.connect("clicked", lambda *_a: self.choose_folder())
        box.append(self.folder_button)
        self.importance_row = Gtk.Box(spacing=ai_core.GAP_INLINE)
        box.append(self.importance_row)
        # Scheduled runs are not part of the plugin (they need systemd user timers); Maprisma, the
        # desktop app it is based on, has them. Only a hint and a link: nothing is installed from here.
        box.append(parts.label(_("Want this project to run by itself, for example every day at 05:00? "
                                 "Scheduled runs are in Maprisma, the desktop app AI Agents Map is based on."),
                               ("am-project-dim",), wrap=True))
        box.append(Gtk.LinkButton(uri=MAPRISMA_URL, label=_("Maprisma on GitHub"), halign=Gtk.Align.START))
        return box

    def build_plan(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP_TIGHT)
        box.append(parts.label(_("Project plan"), ("am-section-heading",)))
        self.plan_view = text_view(10, lambda text: self.edit("plan", text))
        box.append(self.plan_view)
        # The plan box is empty while the lead drafts: it says why.
        self.plan_hint = parts.label(_("The AI is drafting the plan. It appears here when the agent is done."),
                                     ("am-project-dim",), wrap=True)
        box.append(self.plan_hint)
        self.approved_note = parts.label("", wrap=True)
        box.append(self.approved_note)
        return box

    def build_activity(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP_TIGHT)
        self.messages = MessagesView(self.ctrl)
        box.append(self.messages.root)
        box.append(parts.label(_("Agent output"), ("heading",)))
        self.output = parts.label("", ("monospace",), wrap=True, selectable=True)
        box.append(self.output)
        self.handoff_heading = parts.label(_("Saved handoff"), ("heading",))
        box.append(self.handoff_heading)
        self.handoff = parts.label("", wrap=True, selectable=True)
        box.append(self.handoff)
        box.append(Gtk.Separator())
        self.events = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.append(self.events)
        return box

    def edit(self, field: str, value: str):
        if not self.syncing:
            self.ctrl.set_text(field, value)

    def choose_folder(self):
        ctrl = self.ctrl
        asked = (ctrl.selection, (ctrl.project or {}).get("id"))

        def done(path):
            # Only into the project it was chosen for, while its fields can still be edited.
            still = (not ctrl.disposed and ctrl.open and not ctrl.busy and not ctrl.running
                     and (ctrl.selection, (ctrl.project or {}).get("id")) == asked)
            if path and still and self.root.get_root() is not None:
                self.path_entry.set_text(path)
        # The mounted disks and pinned folders in the sidebar (Maprisma task 098).
        folder_picker.choose_folder(self.host.window, _("Working folder"), ctrl.texts.get("path") or "", done)

    # --- drawing ---------------------------------------------------------------------------------------

    def render(self):
        ctrl = self.ctrl
        project = ctrl.project
        card = ctrl.card_of_open
        self.title.set_label(str(project.get("title") or "") if project else _("New project"))
        # A completed run that left steps to the user says so (its status stays completed).
        self.status_line.set_label(" · ".join([texts.status_line(project or {})]
                                              + ([_("Unsaved changes")] if ctrl.dirty else [])))
        if texts.is_partial(project):
            self.status_line.add_css_class("am-project-warn")
        else:
            self.status_line.remove_css_class("am-project-warn")
        card_key = json.dumps([card, self.narrow], sort_keys=True, default=str)
        if card_key != self.shown_card:
            self.shown_card = card_key
            parts.clear(self.rail)
            if card is not None:
                self.rail.append(parts.large_stage_rail(card, self.colors, compact=self.narrow))
            parts.clear(self.issues)
            found = parts.issues_list(card, lambda item: ctrl.fix_issue(None, item, self.host_map_fix()),
                                      self.host_map_fix() is not None) if card is not None else None
            if found is not None:
                self.issues.append(found)
        self.render_access()
        self.render_tabs()
        self.working.render(card, project, compact=self.narrow)
        self.plan_hint.set_visible(not ctrl.texts["plan"].strip() and (project or {}).get("status") == "planning"
                                   and (project or {}).get("phase") == "planning")
        self.stopping.set_visible((project or {}).get("processes_stopping") is True)
        self.error.set_label(ctrl.error or "")
        self.error.set_visible(bool(ctrl.error))
        self.sync_fields()
        editable = not ctrl.busy and not ctrl.running
        for widget in (self.title_entry, self.path_entry, self.folder_button):
            widget.set_sensitive(editable)
        for view in (self.brief_view, self.plan_view):
            view.set_editable(editable)
        self.render_importance()
        self.approved_note.set_label(_("This plan version is approved.") if ctrl.approved
                                     else _("Review this plan and the team before execution."))
        self.render_team()
        self.render_activity()
        self.render_setup()
        self.pages.set_visible_child_name(str(ctrl.tab))
        self.render_footer()

    def host_map_fix(self):
        return getattr(self.host, "projects_map_fix", None)

    def render_access(self):
        """The amber gaps box (a saved, unchanged, not approved project whose team can't do every
        planned step) and the steps a completed run left to the user (task 104)."""
        ctrl = self.ctrl
        project = ctrl.project
        show_gaps = project is not None and not ctrl.dirty and not ctrl.approved
        idle = not ctrl.busy and not ctrl.running
        label = ctrl.suggest_label() if show_gaps else None
        names = {str(e.get("agent")): ctrl.agent_name(str(e.get("agent")))
                 for e in (project or {}).get("not_done") or [] if isinstance(e, dict) and e.get("agent")}
        key = json.dumps([show_gaps and texts.gaps_of(project), (project or {}).get("needs_source"), label, idle,
                          bool(ctrl.texts["plan"].strip()), (project or {}).get("partial"),
                          (project or {}).get("not_done"), names], sort_keys=True, default=str)
        if key == self.shown_access:
            return
        self.shown_access = key
        parts.clear(self.access)
        if show_gaps:
            box = parts.access_gaps_box(project, label, ctrl.use_suggested if idle else None,
                                        ctrl.leave_to_me if idle and ctrl.texts["plan"].strip() else None)
            if box is not None:
                self.access.append(box)
        if project is not None:
            left = parts.steps_left_box(project, ctrl.agent_name)
            if left is not None:
                self.access.append(left)
        self.access.set_visible(self.access.get_first_child() is not None)

    def sync_fields(self):
        """The fields take the adopted texts (a project opened, saved, discarded or read again)
        without counting as edits; a field shows its text only when it differs (the cursor stays)."""
        ctrl = self.ctrl
        if self.shown_adopt == ctrl.adopt_version:
            return
        self.shown_adopt = ctrl.adopt_version
        self.syncing = True
        try:
            for entry, field in ((self.title_entry, "title"), (self.path_entry, "path")):
                if entry.get_text() != ctrl.texts[field]:
                    entry.set_text(ctrl.texts[field])
            for view, field in ((self.brief_view, "brief"), (self.plan_view, "plan")):
                if view_text(view) != ctrl.texts[field]:
                    view.get_buffer().set_text(ctrl.texts[field])
        finally:
            self.syncing = False

    def render_tabs(self):
        ctrl = self.ctrl
        key = (ctrl.tab, self.narrow)
        if key == self.shown_tabs:
            return
        self.shown_tabs = key
        parts.clear(self.tabs)
        if self.narrow:
            self.tabs.append(dropdown([(i, _(name), True) for i, name in enumerate(texts.TABS)], ctrl.tab,
                                      ctrl.set_tab))
            self.tabs.get_first_child().set_hexpand(True)
            return
        first = None
        for i, name in enumerate(texts.TABS):
            button = Gtk.ToggleButton(label=_(name), active=i == ctrl.tab)
            button.add_css_class("am-chip")
            if first is None:
                first = button
            else:
                button.set_group(first)
            button.connect("toggled", lambda b, i=i: b.get_active() and ctrl.set_tab(i))
            self.tabs.append(button)

    def render_importance(self):
        """The Goal tab's importance (task 095): set at once, never part of the draft; only for a saved
        project."""
        ctrl = self.ctrl
        pid = (ctrl.project or {}).get("id")
        value = (ctrl.project or {}).get("importance")
        sensitive = not (ctrl.busy or pid in ctrl.marking or pid in ctrl.quick or ctrl.unavailable)
        key = (pid, value, sensitive, self.narrow)
        if key == self.shown_importance:
            return                        # its buttons stay while the project runs
        self.shown_importance = key
        parts.clear(self.importance_row)
        if not isinstance(pid, str) or texts.importance_of(value) is None:
            self.importance_row.set_visible(False)
            return
        self.importance_row.set_visible(True)
        compact = self.narrow
        self.importance_row.set_orientation(Gtk.Orientation.VERTICAL if compact else Gtk.Orientation.HORIZONTAL)
        title = parts.label(_("Importance"))
        title.set_hexpand(not compact)
        self.importance_row.append(title)
        self.importance_row.append(segmented([(code, _(name)) for code, name, _icon in texts.IMPORTANCES], value,
                                             lambda code: ctrl.set_importance(pid, code), sensitive))

    # --- team ----------------------------------------------------------------------------------------

    def phase_selector(self) -> Gtk.Widget:
        ctrl = self.ctrl
        return segmented([("plan", _("Planning")), ("run", _("Execution"))], ctrl.phase, ctrl.change_phase,
                         not ctrl.busy)

    def render_team(self):
        ctrl = self.ctrl
        card = ctrl.card_of_open
        beside = self.width >= GRAPH_BESIDE
        show_on_map = getattr(self.host, "projects_show_on_map", None)
        if self.team_holder.get_first_child() is None or self.shown_team is None:
            parts.clear(self.team_holder)
            self.team_layout = Gtk.Box(spacing=ai_core.GAP)
            self.team_form = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
            self.team_graph = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
            self.team_layout.append(self.team_form)
            self.team_layout.append(self.team_graph)
            self.team_holder.append(self.team_layout)
            self.shown_graph = None
        self.team_layout.set_orientation(Gtk.Orientation.HORIZONTAL if beside else Gtk.Orientation.VERTICAL)
        # The graph follows the card (a run moves on); the form only its own inputs, so typing in it
        # is never interrupted by a running project's updates.
        graph_key = json.dumps([card and {k: card.get(k) for k in ("planning", "execution", "active")},
                                show_on_map is not None], sort_keys=True, default=str)
        if graph_key != self.shown_graph:
            self.shown_graph = graph_key
            parts.clear(self.team_graph)
            if card is not None:
                self.team_graph.append(parts.team_graph(card, show_on_map))
        self.team_graph.set_visible(card is not None)   # a new project: the form takes the width
        # Every profile fact the form shows (commands, permissions, options) is part of its key.
        key = json.dumps([ctrl.team_version, ctrl.phase, ctrl.busy, ctrl.running, ctrl.profiles, ctrl.runtimes,
                          ctrl.allow_push, (ctrl.project or {}).get("effective_capabilities")],
                         sort_keys=True, default=str)
        if key == self.shown_team:
            return
        self.shown_team = key
        refocus = self.focused_field()
        parts.clear(self.team_form)
        form = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP_TIGHT, hexpand=True)
        form.append(self.phase_selector())
        locked = ctrl.team_locked
        if ctrl.phase == "plan":
            if ctrl.plan_team is None:
                form.append(parts.label(_("Planning follows the execution team until customized."), wrap=True))
            toggle = Gtk.Button(label=_("Customize planning team") if ctrl.plan_team is None
                                else _("Use execution team for planning"), halign=Gtk.Align.START)
            toggle.add_css_class("am-small-btn")
            toggle.set_sensitive(not ctrl.busy and not ctrl.running)
            toggle.connect("clicked", lambda *_a: ctrl.toggle_plan_team())
            form.append(toggle)
        form.append(parts.label(_("Choose the lead agent and optional cooperating agents."), wrap=True))
        # Task 104: push stays off unless the user allows it for this project.
        if ctrl.phase != "plan":
            line = Gtk.Box(spacing=ai_core.GAP_INLINE)
            words = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
            words.append(parts.label(_(texts.ALLOW_PUSH[0])))
            words.append(parts.label(_(texts.ALLOW_PUSH[1]), ("am-project-small", "am-project-dim"), wrap=True))
            line.append(words)
            push = Gtk.Switch(active=ctrl.allow_push, sensitive=not locked, valign=Gtk.Align.CENTER)
            push.set_name("allow-push")
            parts.set_accessible(push, _(texts.ALLOW_PUSH[0]))
            push.connect("notify::active", lambda switch, _p: ctrl.set_allow_push(switch.get_active()))
            line.append(push)
            form.append(line)
        if not ctrl.profiles:
            form.append(parts.label(_("No project agent profiles are available."), ("am-project-dim",), wrap=True))
        for index in range(len(ctrl.team)):
            form.append(self.member(index, locked))
        add = Gtk.Button(halign=Gtk.Align.START)
        content = Gtk.Box(spacing=4)
        content.append(parts.icon("list-add-symbolic"))
        content.append(Gtk.Label(label=_("Add cooperating agent")))
        add.set_child(content)
        add.add_css_class("am-small-btn")
        add.set_sensitive(not locked and bool(ctrl.profiles))
        add.connect("clicked", lambda *_a: ctrl.add_member())
        form.append(add)
        form.append(Gtk.Separator())
        form.append(parts.label(_("Cooperating agents run as separate managed sessions."), ("am-project-dim",), wrap=True))
        self.team_form.append(form)
        self.guard(form)
        if refocus is not None:
            self.restore_field(form, *refocus)

    def focused_field(self):
        """(key, cursor) of the Team form's entry that has the keyboard, else None."""
        root = self.root.get_root()
        focus = root.get_focus() if root is not None else None
        while focus is not None and not hasattr(focus, "field_key"):
            focus = focus.get_parent()
        if focus is None or not focus.is_ancestor(self.team_form):
            return None
        return focus.field_key, focus.get_position()

    @staticmethod
    def restore_field(form, key, cursor):
        stack = [form]
        while stack:
            widget = stack.pop()
            if getattr(widget, "field_key", None) == key:
                widget.grab_focus()
                widget.set_position(min(cursor, len(widget.get_text())))
                return
            child = widget.get_first_child()
            while child is not None:
                stack.append(child)
                child = child.get_next_sibling()

    def guard(self, widget):
        """Fields built after the window was: paste only through the app's bounded read (TextGuard)."""
        guard = getattr(self.host, "guard_text_fields_in", None)
        if guard is not None:
            guard(widget)

    def member(self, index: int, locked: bool) -> Gtk.Widget:
        """One member's form (Maprisma's _member): profile, role, access, instructions, local runtime and
        model, native subagents, effective permissions and commands, Remove."""
        ctrl = self.ctrl
        member = ctrl.team[index]
        profile = ctrl.profile(member.get("agent"))
        effective_caps = ((ctrl.project or {}).get("effective_capabilities") or {}).get(member.get("agent"))
        effective_profile = ({**profile, "capabilities": effective_caps}
                             if profile is not None and isinstance(effective_caps, dict) else profile)
        modes = list((profile or {}).get("access_modes") or ["read-only", "project-edit"])
        plan = ctrl.phase == "plan"
        access = "read-only" if plan or member.get("role") == "reviewer" \
            else member.get("access") or texts.default_access(modes)
        access_label = texts.access_label
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP_TIGHT)
        card.add_css_class("am-model-card")

        def row(title, widget):
            line = Gtk.Box(spacing=ai_core.GAP_INLINE)
            name = parts.label(title)
            name.set_hexpand(True)
            line.append(name)
            line.append(widget)
            card.append(line)
        agent = Gtk.Box(spacing=ai_core.GAP_INLINE)
        agent.append(dropdown([(p.get("id"), str(p.get("name") or p.get("id")), True) for p in ctrl.profiles],
                              member.get("agent"), lambda v: ctrl.set_member(index, "agent", v), not locked))
        # What the chosen agent can and cannot do here (at Edit + commands with the project's Allow push).
        agent.append(parts.ability_hint(effective_profile, access, ctrl.allow_push))
        row(_("Agent profile"), agent)
        row(_("Role"), dropdown([(r, texts.role(r, planning=plan), True) for r in ("lead", "implementer", "reviewer")],
                                member.get("role"), lambda v: ctrl.set_member(index, "role", v), not locked))
        choices = ([(access, _("{0} (unavailable for this profile)").format(access_label(access)), False)]
                   if access not in modes else []) + [(m, access_label(m), True) for m in modes]
        row(_("Execution access"), dropdown(choices, access, lambda v: ctrl.set_member(index, "access", v),
                                            not locked and not plan and member.get("role") != "reviewer"))
        if access not in modes:
            card.append(parts.label(_("Choose a supported access mode before saving."), ("am-project-error",), wrap=True))
        else:
            # What this level allows for this agent (task 104), and the higher risk of Edit + commands
            # (agent-neutral: the shield names this agent's own limits).
            facts = texts.capabilities_at(effective_profile, access, ctrl.allow_push)
            if facts is not None and access != "read-only":
                line = parts.label(texts.capability_line(facts), ("am-project-small",), wrap=True)
                line.set_name(f"access-facts-{index}")
                card.append(line)
            if access == "project-commands":
                card.append(parts.warn_line(_(texts.COMMANDS_RISK)))
        card.append(parts.label(_("Role instructions"), ("am-project-small", "am-project-dim")))
        instructions = Gtk.Entry(text=str(member.get("instructions") or ""), sensitive=not locked)
        instructions.field_key = (ctrl.phase, index, "instructions")
        instructions.connect("changed", lambda e: ctrl.set_member(index, "instructions", e.get_text()))
        card.append(instructions)
        if profile is not None:
            self.runtime_fields(card, member, profile, index, locked)
            if profile.get("model_option") is not None and member.get("runtime") is None:
                card.append(parts.label(_("Model (optional)"), ("am-project-small", "am-project-dim")))
                model = Gtk.Entry(text=str(member.get("model") or ""), sensitive=not locked)
                model.field_key = (ctrl.phase, index, "model")
                model.connect("changed", lambda e: ctrl.set_member(index, "model", e.get_text()))
                card.append(model)
            definitions = profile.get("subagent_definitions")
            if profile.get("native_subagents") is True and isinstance(definitions, dict) and definitions:
                card.append(parts.label(_("Native subagents")))
                chosen = list(member.get("subagents") or [])
                for preset in definitions:
                    check = Gtk.CheckButton(label=_({"researcher": "Researcher", "reviewer": "Reviewer"}.get(preset, preset)),
                                            active=preset in chosen, sensitive=not locked)

                    def toggled(button, preset=preset):
                        now = [p for p in member.get("subagents") or [] if p != preset]
                        if button.get_active():
                            now.append(preset)
                        ctrl.set_member(index, "subagents", now)
                    check.connect("toggled", toggled)
                    card.append(check)
            else:
                card.append(parts.label(_("Native subagents are unavailable for this profile."),
                                        ("am-project-small", "am-project-dim"), wrap=True))
            read_only = plan or member.get("access") == "read-only" or member.get("role") == "reviewer"
            card.append(parts.label(_("Effective permissions"), ("am-project-small", "am-project-dim")))
            card.append(parts.label(_("Read-only") if read_only else str(profile.get("permissions") or ""), wrap=True))
            card.append(parts.label(_("Planning command"), ("am-project-small", "am-project-dim")))
            card.append(parts.label(" ".join(profile.get("plan_argv") or []), ("monospace",), wrap=True, selectable=True))
            card.append(parts.label(_("Execution command"), ("am-project-small", "am-project-dim")))
            # The argv this member launches with (the core's choice per access level and Allow push).
            argv = texts.execution_argv(profile, member, plan, ctrl.allow_push)
            command = parts.label(" ".join(str(word) for word in argv), ("monospace",), wrap=True, selectable=True)
            command.set_name(f"execution-command-{index}")
            card.append(command)
        remove = Gtk.Button(label=_("Remove"), halign=Gtk.Align.END, sensitive=not locked)
        remove.add_css_class("am-small-btn")
        remove.connect("clicked", lambda *_a: ctrl.remove_member(index))
        card.append(remove)
        return card

    def runtime_fields(self, card, member: dict, profile: dict, index: int, locked: bool):
        ctrl = self.ctrl
        agent = member.get("agent")

        def binding(runtime):
            return ((runtime.get("bindings") or {}).get(agent)) if isinstance(runtime.get("bindings"), dict) else None
        candidates = [r for r in ctrl.runtimes if binding(r) is not None and profile.get("provider_option") is not None]
        chosen = str(member.get("runtime") or "")
        items = [("", _("Agent default"), True)]
        if chosen and not any(r.get("key") == chosen for r in candidates):
            items.append((chosen, chosen, False))
        items += [(r.get("key"), str(r.get("name") or r.get("key")), r.get("installed") is True and r.get("running") is True)
                  for r in candidates]
        line = Gtk.Box(spacing=ai_core.GAP_INLINE)
        name = parts.label(_("Local runtime"))
        name.set_hexpand(True)
        line.append(name)
        line.append(dropdown(items, chosen, lambda v: ctrl.set_member(index, "runtime", v), not locked))
        card.append(line)
        card.append(parts.label(_("Connect and start local runtimes on the map before selecting them here."),
                                ("am-project-small", "am-project-dim"), wrap=True))
        if chosen:
            selected = next((r for r in ctrl.runtimes if r.get("key") == chosen), None)
            models = [str(m) for m in (binding(selected) or {}).get("models") or []] if selected else []
            card.append(parts.label(_("Agent provider") + ": " + str(member.get("provider") or "")))
            model = member.get("model")
            choices = ([(model, str(model), False)] if model is not None and model not in models else []) + \
                [(m, m, True) for m in models]
            line = Gtk.Box(spacing=ai_core.GAP_INLINE)
            name = parts.label(_("Available local model"))
            name.set_hexpand(True)
            line.append(name)
            line.append(dropdown(choices, model, lambda v: ctrl.set_member(index, "bound_model", v), not locked))
            card.append(line)

    # --- activity ------------------------------------------------------------------------------------

    def render_activity(self):
        project = self.ctrl.project or {}
        self.messages.render(project if self.ctrl.project else None)
        output = str(project.get("output") or "")
        if self.output.get_label() != output:
            self.output.set_label(output)
        handoff = str(project.get("handoff") or "")
        self.handoff_heading.set_visible(bool(handoff))
        self.handoff.set_visible(bool(handoff))
        if self.handoff.get_label() != handoff:
            self.handoff.set_label(handoff)
        events = [e for e in project.get("events") or [] if isinstance(e, dict)]
        key = json.dumps(events, sort_keys=True, default=str)
        if getattr(self, "shown_events", None) != key:
            self.shown_events = key
            parts.clear(self.events)
            for event in events:
                line = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
                line.append(parts.label(texts.event_text(event), wrap=True))
                line.append(parts.label(str(event.get("at") or ""), ("am-project-small", "am-project-dim")))
                self.events.append(line)

    # --- setup ---------------------------------------------------------------------------------------

    def render_setup(self):
        ctrl = self.ctrl
        project = ctrl.project or {}
        key = json.dumps([ctrl.phase, ctrl.busy, ctrl.running, ctrl.setup_helper, (project or {}).get("id"),
                          [p.get("id") for p in ctrl.setup_helpers()],
                          project.get("setup_proposal"), project.get("setup_error"), project.get("setup_output"),
                          project.get("status"), project.get("phase")], sort_keys=True, default=str)
        if key != getattr(self, "shown_setup", None):
            self.shown_setup = key
            self.build_setup(project)
        # The request keeps its cursor while typed; it takes the text when the project or phase changed.
        if view_text(self.request_view) != ctrl.setup_request:
            self.syncing = True
            self.request_view.get_buffer().set_text(ctrl.setup_request)
            self.syncing = False
        if self.use_button is not None:
            protected = project.get("phase") == "running" and project.get("status") in ("paused", "interrupted",
                                                                                         "failed")
            self.use_button.set_sensitive(not ctrl.busy and not ctrl.running and not ctrl.dirty and not protected)

    def build_setup(self, project: dict):
        ctrl = self.ctrl
        box = self.setup_holder
        parts.clear(box)
        box.append(self.phase_selector())
        protected = project.get("phase") == "running" and project.get("status") in ("paused", "interrupted", "failed")
        box.append(parts.label(_("AI setup request"), ("am-section-heading",)))
        request = text_view(4, lambda text: None if self.syncing else ctrl.set_setup_request(text))
        self.request_view = request
        self.use_button = None
        request.set_editable(not ctrl.busy and not ctrl.running)
        request.set_tooltip_text(_("Example: use Claude with agy and Pi using llama and the optimal model for this project."))
        box.append(request)
        box.append(parts.label(_("Example: use Claude with agy and Pi using llama and the optimal model for this project."),
                               ("am-project-small", "am-project-dim"), wrap=True))
        helpers = ctrl.setup_helpers()
        if not helpers:
            box.append(parts.label(_("No setup helper is available.")))
        else:
            line = Gtk.Box(spacing=ai_core.GAP_INLINE)
            name = parts.label(_("Setup helper"))
            name.set_hexpand(True)
            line.append(name)
            line.append(dropdown([(h.get("id"), str(h.get("name") or h.get("id")), True) for h in helpers],
                                 ctrl.setup_helper, ctrl.set_setup_agent, not ctrl.busy and not ctrl.running))
            box.append(line)
        propose = Gtk.Button(label=_("Propose AI setup"), halign=Gtk.Align.START)
        propose.add_css_class("am-primary")
        propose.set_sensitive(not ctrl.busy and not ctrl.running and not protected and bool(helpers))
        propose.connect("clicked", lambda *_a: ctrl.perform("setup"))
        box.append(propose)
        box.append(Gtk.Separator())
        if protected:
            box.append(parts.label(_("Review existing changes before changing this setup."), wrap=True))
        proposal = project.get("setup_proposal")
        if project.get("setup_error") is not None and not isinstance(proposal, dict):
            box.append(parts.label(_("Team setup was stopped. The project is unchanged.") if project["setup_error"] == "paused"
                                   else _("Team setup ended without a proposal. The project is unchanged."), wrap=True))
        if not isinstance(proposal, dict):
            box.append(parts.label(_("No AI setup proposal yet."), ("am-project-dim",)))
        else:
            target_plan = proposal.get("target") == "plan"
            box.append(parts.label(_("Proposed AI setup"), ("heading",)))
            box.append(parts.label(_("Proposal target") + ": " + (_("Planning") if target_plan else _("Execution"))))
            for member in [m for m in proposal.get("team") or [] if isinstance(m, dict)]:
                card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
                card.add_css_class("am-model-card")
                card.append(parts.label(f"{ctrl.agent_name(member.get('agent'))} · "
                                        f"{texts.role(member.get('role'), planning=target_plan)}", ("heading",)))
                for title, key_ in ((_("Model (optional)"), "model"), (_("Agent provider"), "provider"),
                                    (_("Local runtime"), "runtime")):
                    card.append(parts.label(f"{title}: {member.get(key_) or _('Agent default')}", wrap=True))
                effective = ("read-only" if target_plan or member.get("role") == "reviewer"
                             else member.get("access") or "project-edit")
                card.append(parts.label(_("Effective permissions") + ": " + texts.access_label(effective)))
                if effective == "project-commands":
                    card.append(parts.warn_line(_(texts.COMMANDS_RISK)))
                if member.get("instructions"):
                    card.append(parts.label(str(member["instructions"]), wrap=True))
                box.append(card)
            box.append(parts.label(_("Rationale"), ("heading",)))
            box.append(parts.label(str(proposal.get("rationale") or ""), wrap=True, selectable=True))
            warnings = [w for w in proposal.get("warnings") or [] if isinstance(w, str)]
            if warnings:
                box.append(parts.label(_("Warnings"), ("heading",)))
                for warning in warnings:
                    box.append(parts.label("• " + warning, wrap=True))
            excluded = proposal.get("excluded_agents") or []
            if excluded:
                box.append(parts.label(_("Excluded agents") + ": " + ", ".join(ctrl.agent_name(a) for a in excluded),
                                       wrap=True))
            runtimes = proposal.get("excluded_runtimes") or []
            if runtimes:
                names = {r.get("key"): r.get("name") for r in ctrl.runtimes}
                box.append(parts.label(_("Excluded runtimes") + ": " +
                                       ", ".join(str(names.get(r) or r) for r in runtimes), wrap=True))
            use = Gtk.Button(label=_("Use proposed team"), halign=Gtk.Align.START)
            use.add_css_class("am-primary")
            self.use_button = use
            use.connect("clicked", lambda *_a: ctrl.perform("setup_apply"))
            box.append(use)
        if project.get("setup_output"):
            box.append(Gtk.Separator())
            box.append(parts.label(_("Setup helper output"), ("heading",)))
            box.append(parts.label(str(project["setup_output"]), ("monospace",), wrap=True, selectable=True))
        self.guard(box)

    # --- footer --------------------------------------------------------------------------------------

    def render_footer(self):
        ctrl = self.ctrl
        actions = texts.footer_actions(ctrl.project, ctrl.texts["plan"], ctrl.dirty, ctrl.running, ctrl.approved,
                                       bool(ctrl.plan_team if ctrl.plan_team is not None else ctrl.run_team),
                                       has_run_team=bool(ctrl.run_team))
        key = (tuple(actions), ctrl.busy, ctrl.dirty, self.narrow)
        if key == self.shown_footer:
            return                        # its buttons and its More menu stay while output comes in
        self.shown_footer = key
        parts.clear(self.footer)

        def button(text, operation, enabled=True):
            widget = Gtk.Button(label=text)
            widget.add_css_class("am-small-btn")
            if operation in ("run", "continue", "review"):
                widget.add_css_class("am-primary")
            if operation == "delete":
                widget.add_css_class("am-danger")
            widget.set_sensitive(enabled and not ctrl.busy)
            widget.connect("clicked", lambda *_a: ctrl.perform(operation))
            return widget
        if not self.narrow:
            flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=8, hexpand=True,
                               column_spacing=ai_core.GAP_INLINE, row_spacing=ai_core.GAP_TIGHT)
            for text, operation, enabled in actions:
                child = Gtk.FlowBoxChild(focusable=False)
                child.set_child(button(text, operation, enabled))
                flow.append(child)
            self.footer.append(flow)
            return
        primary = texts.primary_action(actions, ctrl.dirty)
        if primary is not None:
            main = button(*primary)
            main.set_hexpand(True)
            self.footer.append(main)
        else:
            self.footer.append(Gtk.Box(hexpand=True))
        more = Gtk.MenuButton(icon_name="view-more-symbolic", tooltip_text=_("More"), sensitive=not ctrl.busy)
        popover = Gtk.Popover()
        menu = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        for text, operation, enabled in actions:
            if primary is not None and operation == primary[1]:
                continue
            item = Gtk.Button(label=text, sensitive=enabled)
            item.add_css_class("flat")
            item.connect("clicked", lambda *_a, operation=operation: (popover.popdown(), ctrl.perform(operation)))
            menu.append(item)
        popover.set_child(menu)
        more.set_popover(popover)
        self.footer.append(more)
