"""AI Agents Map: the Projects page, the AI Project Center's board (a mixin of ai_popup.AgentsMap;
Maprisma's ai_project_center.dart and project_board.dart, tasks 082, 086, 088, 090, 094-096).

The board shows the projects as cards in the user's order: the summary row filters them (Running,
Attention, Needs you, Ready, Drafts, Agents finished, All, Importance, the agent chip from the map),
switches Cards | Phases and starts a New project; each card shows its stage rail, activity, team,
problems and last event with the quick actions Open, Review plan, Pause / Retry pause and the trash
symbol, its importance mark and its Move handle (drag onto another card, the Move menu, or
Ctrl+Up / Down / Home / End). A card opens the project page (project_detail_page.py) over the board,
which stays where it was. The state and every action live in projects_controller.py; this module
draws it and wires GTK to it. ProjectCenter calls run on worker threads (they can block for about
10 s) and come back with GLib.idle_add.

Entry points for the shell (WP7 wires the header tabs): open_projects(agent=None), close_projects(),
projects_prepare_leave(done), shutdown_projects(); projects_map_fix / projects_show_on_map are the
links to the AI Map (None: the links are not offered)."""

from __future__ import annotations

import json
import threading

from gi.repository import Adw, GLib, GObject, Gdk, Gtk

import ai_core
import project_parts as parts
import project_working
import project_texts as texts
from i18n import _
from project_detail_page import ProjectDetail
from project_phases_page import PhasesView
from project_supervisor import ProjectError
from projects_controller import POLL_SECONDS, ProjectsController

COLUMNS_3, COLUMNS_2 = 1100, 700       # the cards grid: 3 columns from 1100 px, 2 from 700 px, else 1
SUMMARY_NARROW, VIEW_NAMED = 600, 1000  # New project as a symbol below 600 px; Cards | Phases named from 1000 px
PHASES_WIDE = 6 * 170 + 7 * 8          # six readable columns


def worker_runner(work, done):
    """work() on a worker thread, done(result, error) on the GTK thread."""
    def run():
        try:
            result, error = work(), None
        except Exception as caught:      # a refusal (ProjectError) or a broken backend
            result, error = None, caught
        GLib.idle_add(lambda: done(result, error) and False)
    threading.Thread(target=run, daemon=True).start()


class ProjectsBoard:
    """The board's widgets for one controller; `render()` draws its state again."""

    def __init__(self, ctrl: ProjectsController, colors, host):
        self.ctrl, self.colors, self.host = ctrl, colors, host
        self.width = 1000
        self.cards: dict = {}           # id -> (FlowBoxChild, signature)
        self.order: list = []
        self.dragged = None
        self.popovers: set = set()      # cards with an open menu: not rebuilt meanwhile
        self.shown_summary = None
        self.root = parts.SizeBox(lambda width, _height: self.on_width(width), orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP_TIGHT,
                             hexpand=True, vexpand=True)
        self.banner = Gtk.Box(spacing=ai_core.GAP_INLINE)
        self.banner.add_css_class("am-project-banner")
        self.banner.append(parts.icon("dialog-warning-symbolic"))
        self.banner.append(parts.label(_("The project list has a problem"), wrap=True))
        self.root.append(self.banner)
        self.unavailable = Gtk.Box(spacing=ai_core.GAP_INLINE)
        self.unavailable.append(parts.icon("network-offline-symbolic"))
        self.unavailable.append(parts.label(_("Project status unavailable")))
        retry = Gtk.Button(label=_("Retry"))
        retry.add_css_class("am-small-btn")
        retry.connect("clicked", lambda *_a: ctrl.refresh(profiles=True, force=True))
        self.unavailable.append(retry)
        self.root.append(self.unavailable)
        self.message = parts.label("", ("am-project-error",), wrap=True)
        self.root.append(self.message)
        self.summary = Gtk.Box(spacing=ai_core.GAP_INLINE)
        self.chips = Gtk.Box(spacing=4)
        chips_scroll = Gtk.ScrolledWindow(hexpand=True)
        chips_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        chips_scroll.set_child(self.chips)
        self.summary.append(chips_scroll)
        self.view_switch = Gtk.Box()
        self.view_switch.add_css_class("am-segmented")
        self.view_buttons = {}
        for code, name, icon_name in texts.VIEWS:
            button = Gtk.ToggleButton(valign=Gtk.Align.CENTER)
            button.add_css_class("am-segment")
            button.add_css_class("flat")
            button.connect("toggled", lambda b, code=code: b.get_active() and ctrl.set_view(code))
            if self.view_buttons:
                button.set_group(next(iter(self.view_buttons.values())))
            self.view_buttons[code] = button
            self.view_switch.append(button)
        self.summary.append(self.view_switch)
        self.new_button = Gtk.Button(valign=Gtk.Align.CENTER)
        self.new_button.add_css_class("am-primary")
        self.new_button.connect("clicked", lambda *_a: ctrl.open_card(None))
        self.summary.append(self.new_button)
        self.root.append(self.summary)
        self.root.append(Gtk.Separator())
        self.content = Gtk.Stack(vexpand=True)
        self.content.add_named(self._empty_card(), "empty")
        none = parts.label(_("No projects here."), xalign=0.5)
        none.set_valign(Gtk.Align.CENTER)
        self.content.add_named(none, "none")
        self.flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True, valign=Gtk.Align.START,
                                activate_on_single_click=True, column_spacing=ai_core.GAP, row_spacing=ai_core.GAP,
                                max_children_per_line=2, min_children_per_line=1)
        self.flow.set_margin_top(ai_core.GAP_TIGHT)
        self.flow.connect("child-activated", self.on_card_activated)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self.on_card_key)
        self.flow.add_controller(keys)
        self.scroller = Gtk.ScrolledWindow(vexpand=True)
        self.scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroller.set_child(self.flow)
        self.content.add_named(self.scroller, "cards")
        self.phases = PhasesView(lambda p: ctrl.open_card(p))
        self.content.add_named(self.phases.root, "phases")
        self.root.append(self.content)

    def _empty_card(self) -> Gtk.Widget:
        button = Gtk.Button(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        button.add_css_class("am-project-card")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP_TIGHT)
        box.append(parts.icon("list-add-symbolic", 40))
        box.append(parts.label(_("New project"), ("am-project-title",), xalign=0.5))
        box.append(parts.label(_("Give a team of agents a goal and a folder."), xalign=0.5))
        button.set_child(box)
        button.connect("clicked", lambda *_a: self.ctrl.open_card(None))
        self.empty_new = button
        return button

    def on_width(self, width: int):
        if self.ctrl.disposed:
            return
        changed = self._layout_key(self.width) != self._layout_key(width)
        self.width = width
        if changed:
            # Team strip and rail follow the width: every card is drawn again (its child is kept).
            self.cards = {pid: (child, None) for pid, (child, _sig) in self.cards.items()}
            self.shown_summary = None
            self.render()

    @staticmethod
    def _layout_key(width: int) -> tuple:
        return (width >= COLUMNS_3, width >= COLUMNS_2, width >= SUMMARY_NARROW, width >= VIEW_NAMED,
                width >= PHASES_WIDE)

    @property
    def narrow(self) -> bool:
        return self.width < COLUMNS_2

    # --- drawing -----------------------------------------------------------------------------------

    def render(self):
        ctrl = self.ctrl
        self.banner.set_visible(ctrl.store_error is not None)
        self.unavailable.set_visible(ctrl.unavailable)
        self.message.set_label(ctrl.error or "")
        self.message.set_visible(bool(ctrl.error) and not ctrl.unavailable and not ctrl.open)
        self.render_summary()
        board = ctrl.agent_projects()
        shown = ctrl.shown()
        if not board and ctrl.agent is None:
            self.drop_cards()
            self.content.set_visible_child_name("empty")
        elif not shown:
            self.drop_cards()
            self.content.set_visible_child_name("none")
        elif ctrl.view == "phases":
            self.phases.render(shown, self.width >= PHASES_WIDE)
            self.content.set_visible_child_name("phases")
        else:
            self.render_cards(shown)
            self.content.set_visible_child_name("cards")
        self.empty_new.set_sensitive(not ctrl.busy and not ctrl.unavailable)
        if ctrl.reveal is not None and not ctrl.open:
            seq = ctrl.reveal_seq
            GLib.idle_add(lambda: self.reveal(seq) and False)

    def drop_cards(self):
        for child, _sig in self.cards.values():
            if child.get_parent() is self.flow:
                self.flow.remove(child)
        self.cards, self.order = {}, []
        self.popovers.clear()

    def _chip(self, text: str, icon_name: str, active: bool, clicked) -> Gtk.ToggleButton:
        button = Gtk.ToggleButton(active=active, valign=Gtk.Align.CENTER)
        content = Gtk.Box(spacing=4)
        content.append(parts.icon(icon_name, 14))
        content.append(Gtk.Label(label=text))
        button.set_child(content)
        button.add_css_class("am-chip")

        def pressed(*_a):
            clicked()
            # The chip shows the filter, not its own toggle: All stays on while no filter is chosen
            # (a click on it then changes nothing, so the summary is not drawn again).
            if not self.ctrl.disposed and button.get_active() != self.chip_active(button):
                button.set_active(self.chip_active(button))
        button.filter_code = None
        button.connect("clicked", pressed)
        return button

    def chip_active(self, button) -> bool:
        return self.ctrl.filter == button.filter_code

    def render_summary(self):
        ctrl = self.ctrl
        counts = ctrl.counts()
        self.new_button.set_sensitive(not ctrl.busy and not ctrl.unavailable)
        key = json.dumps([counts, ctrl.importance_counts(), ctrl.filter, ctrl.importance, ctrl.agent, ctrl.view,
                          self._layout_key(self.width)], default=str)
        if key == self.shown_summary:
            return                        # an open menu or a focused chip stays
        self.shown_summary = key
        parts.clear(self.chips)
        if ctrl.agent is not None:
            chip = Gtk.Button(valign=Gtk.Align.CENTER, tooltip_text=_("Show all agents"))
            content = Gtk.Box(spacing=4)
            content.append(parts.icon("system-users-symbolic", 14))
            content.append(Gtk.Label(label=ctrl.agent[1]))
            content.append(parts.icon("window-close-symbolic", 12))
            chip.set_child(content)
            chip.add_css_class("am-chip")
            chip.connect("clicked", lambda *_a: ctrl.set_agent(None))
            self.chips.append(chip)

        def named(name, code):
            return _(name) if counts is None else f"{_(name)} {counts.get(code, 0)}"
        self.all_chip = self._chip(named("All", "all"), texts.ALL_ICON, ctrl.filter is None,
                                   lambda: ctrl.set_filter(None))
        self.chips.append(self.all_chip)
        marks = ctrl.importance_counts()
        if marks is not None:
            self.chips.append(self._importance_chip(marks))
        for code, name, icon_name in texts.FILTERS:
            chip = self._chip(named(name, code), icon_name, ctrl.filter == code,
                              lambda code=code: ctrl.set_filter(code))
            chip.filter_code = code
            self.chips.append(chip)
        named_views = self.width >= VIEW_NAMED
        for code, name, icon_name in texts.VIEWS:
            button = self.view_buttons[code]
            content = Gtk.Box(spacing=4)
            content.append(parts.icon(icon_name, 14))
            if named_views:
                content.append(Gtk.Label(label=_(name)))
            button.set_child(content)
            button.set_tooltip_text(None if named_views else _(name))
            parts.set_accessible(button, _(name))
            if button.get_active() != (ctrl.view == code):
                button.set_active(ctrl.view == code)
        if self.width < SUMMARY_NARROW:
            self.new_button.set_child(parts.icon("list-add-symbolic"))
            self.new_button.set_tooltip_text(_("New project"))
        else:
            content = Gtk.Box(spacing=4)
            content.append(parts.icon("list-add-symbolic"))
            content.append(Gtk.Label(label=_("New project")))
            self.new_button.set_child(content)
            self.new_button.set_tooltip_text(None)
        parts.set_accessible(self.new_button, _("New project"))

    def _importance_chip(self, marks: dict) -> Gtk.Widget:
        """One chip for the importance filter: its menu lists the three with their counts; chosen again
        (or ×) clears it."""
        ctrl = self.ctrl
        chosen = texts.importance_of(ctrl.importance)

        def named(entry):
            count = marks.get(entry[0])
            return _(entry[1]) if count is None else f"{_(entry[1])} {count}"
        box = Gtk.Box(spacing=0)
        button = Gtk.MenuButton(valign=Gtk.Align.CENTER, tooltip_text=_("Importance"))
        content = Gtk.Box(spacing=4)
        content.append(parts.icon(chosen[2] if chosen else texts.IMPORTANCE_ICON, 14))
        content.append(Gtk.Label(label=named(chosen) if chosen else _("Importance")))
        button.set_child(content)
        button.add_css_class("am-chip")
        popover = Gtk.Popover()
        menu = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        for entry in texts.IMPORTANCES:
            item = Gtk.CheckButton(label=named(entry), active=ctrl.importance == entry[0])
            item.connect("toggled", lambda _b, code=entry[0]: (popover.popdown(), ctrl.set_importance_filter(code)))
            menu.append(item)
        popover.set_child(menu)
        button.set_popover(popover)
        box.append(button)
        if chosen is not None:
            clear = Gtk.Button(icon_name="window-close-symbolic", valign=Gtk.Align.CENTER, tooltip_text=_("All"))
            clear.add_css_class("flat")
            clear.add_css_class("am-small-btn")
            clear.connect("clicked", lambda *_a: ctrl.set_importance_filter(None))
            box.append(clear)
        return box

    def render_cards(self, shown: list):
        columns = 3 if self.width >= COLUMNS_3 else 2 if self.width >= COLUMNS_2 else 1
        self.flow.set_max_children_per_line(columns)
        self.flow.set_min_children_per_line(columns)
        focused = self.focused_id()
        ids = [str(p.get("id")) for p in shown]
        busy_state = (self.ctrl.unavailable, self.ctrl.moving, self.ctrl.busy, columns,
                      self.host_map_fix() is not None)
        for project in shown:
            pid = str(project.get("id"))
            signature = json.dumps([project, self.ctrl.card_busy(pid), busy_state], sort_keys=True, default=str)
            held = self.cards.get(pid)
            if held is not None and (held[1] == signature or pid in self.popovers or pid == self.dragged):
                continue                  # unchanged, or its menu is open / it is being dragged
            child = held[0] if held is not None else self._new_child(pid)
            inside = focused == pid and self.focus_inside(child)
            child.set_child(self.card(project, columns == 1))
            self.cards[pid] = (child, signature)
            if inside:
                child.grab_focus()            # the keyboard stays with the card
        for pid in [i for i in self.cards if i not in ids]:
            self.popovers.discard(pid)
            child = self.cards.pop(pid)[0]
            if child.get_parent() is self.flow:
                self.flow.remove(child)
        if ids != self.order or any(self.cards[i][0].get_parent() is None for i in ids):
            for pid in ids:
                child = self.cards[pid][0]
                if child.get_parent() is self.flow:
                    self.flow.remove(child)
            for pid in ids:
                self.flow.append(self.cards[pid][0])
            self.order = ids
            if focused in self.cards:
                self.cards[focused][0].grab_focus()

    def focus_inside(self, child) -> bool:
        root = child.get_root()
        focus = root.get_focus() if root is not None else None
        return focus is not None and focus is not child and focus.is_ancestor(child)

    def menu_shown(self, pid: str, shown: bool):
        """A card's menu opened or closed: the card is not drawn again while it is open."""
        if shown:
            self.popovers.add(pid)
        else:
            self.popovers.discard(pid)
            GLib.idle_add(lambda: self.ctrl.notify() and False)

    def _new_child(self, pid: str) -> Gtk.FlowBoxChild:
        child = Gtk.FlowBoxChild()
        child.project_id = pid
        drop = Gtk.DropTarget.new(GObject.TYPE_STRING, Gdk.DragAction.MOVE)
        drop.connect("enter", lambda _t, _x, _y, pid=pid: self.drop_hint(pid, True))
        drop.connect("motion", lambda _t, _x, _y, pid=pid: self.drop_hint(pid, True))
        drop.connect("leave", lambda _t, pid=pid: self.drop_hint(pid, False))
        drop.connect("drop", lambda _t, value, _x, _y, pid=pid: self.dropped(value, pid))
        child.add_controller(drop)
        return child

    def host_map_fix(self):
        return getattr(self.host, "projects_map_fix", None)

    def card(self, project: dict, vertical: bool) -> Gtk.Widget:
        """One project on the board (Maprisma's ProjectCard)."""
        ctrl = self.ctrl
        pid = str(project.get("id"))
        busy = ctrl.card_busy(pid)
        narrow = self.narrow
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP_TIGHT)
        box.add_css_class("am-project-card")
        if vertical:
            box.add_css_class("am-project-vertical")
        if busy:
            box.add_css_class("am-project-busy")
        head = Gtk.Box(spacing=ai_core.GAP_TIGHT)
        names = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        names.append(parts.label(str(project.get("title") or ""), ("am-project-title",)))
        names.append(parts.label(texts.short_path(str(project.get("path") or "")), ("am-project-small", "am-project-dim")))
        head.append(names)
        if texts.importance_of(project.get("importance")):
            on_mark = None if ctrl.unavailable or busy else (lambda code: ctrl.set_importance(pid, code))
            mark = parts.importance_mark(project.get("importance"), on_mark)
            if isinstance(mark, Gtk.MenuButton):
                mark.get_popover().connect("show", lambda *_a: self.menu_shown(pid, True))
                mark.get_popover().connect("closed", lambda *_a: self.menu_shown(pid, False))
            head.append(mark)
        head.append(self.move_handle(project, busy))
        box.append(head)
        line = Gtk.Box(spacing=ai_core.GAP_TIGHT)
        line.append(parts.icon(texts.bucket_icon(project.get("bucket")), 14))
        line.append(parts.label(texts.activity(project.get("activity")), ()))
        line.get_last_child().set_hexpand(True)
        queued = project.get("messages_queued")
        if type(queued) is int and queued > 0:
            line.append(parts.messages_badge(queued))
        box.append(line)
        second = texts.second_line(project.get("second"))
        if second:
            box.append(parts.label(second, ("am-project-small",)))
        box.append(parts.stage_rail(project.get("stage"), self.colors, compact=narrow))
        box.append(parts.team_strip(project, 3 if narrow else 4))
        issues = [i for i in project.get("issues") or [] if isinstance(i, dict)]
        if issues:
            row = Gtk.Box(spacing=ai_core.GAP_TIGHT)
            row.append(parts.label(" · ".join(texts.issue(i.get("code")) for i in issues),
                                   ("am-project-small", "am-project-error")))
            row.get_first_child().set_hexpand(True)
            map_fix = self.host_map_fix()
            fix = Gtk.Button(label=texts.issue_fix_label(texts.issue_fix(issues[0], map_fix is not None)))
            fix.add_css_class("flat")
            fix.add_css_class("am-small-btn")
            fix.connect("clicked", lambda *_a: ctrl.fix_issue(project, issues[0], map_fix))
            row.append(fix)
            box.append(row)
        if isinstance(project.get("last_event"), dict):
            box.append(parts.label(texts.event_line(project["last_event"]), ("am-project-small", "am-project-dim")))
        actions = Gtk.Box(spacing=4)
        quick = Gtk.Box(spacing=4, hexpand=True)

        def action(text, clicked, sensitive=True):
            button = Gtk.Button(label=text)
            button.add_css_class("am-small-btn")
            button.set_sensitive(sensitive)
            button.connect("clicked", lambda *_a: clicked())
            quick.append(button)
            return button
        action(_("Open"), lambda: ctrl.open_card(project))
        if project.get("activity") == "needs_review":
            action(_("Review plan"), lambda: ctrl.open_card(project, tab=texts.TAB_PLAN))
        pause = texts.pause_label(project)
        if pause:
            action(pause, lambda: ctrl.pause_from_board(project), sensitive=not busy)
        actions.append(quick)
        trash = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text=_("Delete project"))
        trash.add_css_class("flat")
        parts.set_accessible(trash, _("Delete project"))
        trash.set_sensitive(not busy and not ctrl.unavailable and texts.deletable(project))
        trash.connect("clicked", lambda *_a: ctrl.delete_from_board(project))
        actions.append(trash)
        box.append(actions)
        if project_working.working_card(project):
            box.append(project_working.WorkingBar())     # an agent works here (task 098)
        return box

    def move_handle(self, project: dict, busy: bool) -> Gtk.Widget:
        """The drag handle (task 095): dragged onto another card it moves this one there; clicked it
        offers Move up / Move down / Move to top / Move to end."""
        ctrl = self.ctrl
        pid = str(project.get("id"))
        handle = Gtk.MenuButton(child=parts.icon("list-drag-handle-symbolic"), valign=Gtk.Align.START,
                                tooltip_text=_("Move"))
        handle.add_css_class("flat")
        handle.set_sensitive(not busy and not ctrl.unavailable and not ctrl.moving)
        popover = Gtk.Popover()
        menu = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        popover.set_child(menu)

        def fill(*_a):
            # Built when it opens, from the board as it is shown now (a filter or a new card since).
            parts.clear(menu)
            for key, text, place in ctrl.move_options(pid):
                item = Gtk.Button(label=text)
                item.add_css_class("flat")
                item.set_sensitive(place is not None)
                item.connect("clicked", lambda *_a, key=key: (popover.popdown(), ctrl.move_by(pid, key)))
                menu.append(item)
            self.menu_shown(pid, True)
        popover.connect("show", fill)
        popover.connect("closed", lambda *_a: self.menu_shown(pid, False))
        handle.set_popover(popover)
        source = Gtk.DragSource(actions=Gdk.DragAction.MOVE)
        source.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)   # before the button's own click
        source.connect("prepare", lambda *_a: self.drag_start(pid))
        source.connect("drag-end", lambda *_a: self.drag_end())
        handle.add_controller(source)
        return handle

    # --- dragging ----------------------------------------------------------------------------------

    def drag_start(self, pid: str):
        if self.ctrl.unavailable or self.ctrl.moving or self.ctrl.card_busy(pid):
            return None
        self.dragged = pid
        return Gdk.ContentProvider.new_for_value(pid)

    def drag_end(self):
        if self.dragged is not None and not self.ctrl.disposed:
            GLib.idle_add(lambda: self.ctrl.notify() and False)    # a card held while dragged is drawn now
        self.dragged = None
        for child, _sig in self.cards.values():
            card = child.get_child()
            if card is not None:
                card.remove_css_class("am-project-drop-before")
                card.remove_css_class("am-project-drop-after")

    def drop_place(self, source: str, target: str):
        """(target, after) for a card dropped on `target`: after it when it comes from before, before it
        when it comes from behind; None for a card no longer shown."""
        shown = [str(p.get("id")) for p in self.ctrl.shown()]
        if source == target or source not in shown or target not in shown:
            return None
        return target, shown.index(source) < shown.index(target)

    def drop_hint(self, pid: str, on: bool):
        """The drop place as a bar before or after the card; no move for the card itself, a card no
        longer shown or anything not dragged from this board."""
        held = self.cards.get(pid)
        card = held[0].get_child() if held else None
        place = self.drop_place(self.dragged, pid) if on and self.dragged else None
        if card is not None:
            card.remove_css_class("am-project-drop-before")
            card.remove_css_class("am-project-drop-after")
            if place is not None:
                card.add_css_class("am-project-drop-after" if place[1] else "am-project-drop-before")
        return Gdk.DragAction.MOVE if place is not None else Gdk.DragAction(0)

    def dropped(self, value, target: str) -> bool:
        if self.dragged is None or str(value) != self.dragged:
            self.drag_end()
            return False                  # not a card of this board
        place = self.drop_place(str(value), target)
        self.drag_end()
        if place is None:
            return False
        self.ctrl.move_from_board(str(value), *place)
        return True

    # --- keys and focus ----------------------------------------------------------------------------

    def focused_id(self):
        root = self.root.get_root()
        focus = root.get_focus() if root is not None else None
        while focus is not None:
            if isinstance(focus, Gtk.FlowBoxChild) and getattr(focus, "project_id", None):
                return focus.project_id
            focus = focus.get_parent()
        return None

    def on_card_activated(self, _flow, child):
        project = next((p for p in self.ctrl.projects if str(p.get("id")) == getattr(child, "project_id", None)), None)
        if project is not None:
            self.ctrl.open_card(project)

    def on_card_key(self, _controller, keyval, _keycode, state):
        if state & MODIFIERS != Gdk.ModifierType.CONTROL_MASK:
            return False
        key = {Gdk.KEY_Up: "up", Gdk.KEY_Down: "down", Gdk.KEY_Home: "top", Gdk.KEY_End: "end"}.get(keyval)
        pid = self.focused_id()
        if key is None or pid is None:
            return False
        self.ctrl.move_by(pid, key)
        return True

    def reveal(self, seq: int):
        """Brings the card asked for into view and gives it the keyboard ("": the first card shown, else
        New project); a later ask wins."""
        ctrl = self.ctrl
        pid = ctrl.reveal
        if ctrl.disposed or pid is None or not ctrl.revealed(seq):
            return
        if self.root.get_root() is None:
            return                        # the page is gone
        visible = self.content.get_visible_child_name()
        if visible == "phases":
            if self.phases.focus(pid) or self.phases.focus_first():
                return
        elif visible == "cards":
            target = self.cards.get(pid) if pid else None
            if target is None and self.order:
                target = self.cards.get(self.order[0])
            if target is not None:
                target[0].grab_focus()
                return
        (self.empty_new if self.content.get_visible_child_name() == "empty" else self.new_button).grab_focus()


MODIFIERS = (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SHIFT_MASK
             | Gdk.ModifierType.SUPER_MASK | Gdk.ModifierType.META_MASK)


class ProjectsPage:
    """The Projects page in the window (mixin of ai_popup.AgentsMap)."""

    projects_map_fix = None          # WP7: Fix on map (by AI Map box id)
    projects_show_on_map = None      # WP7: Show on map (by agent command)

    def build_projects_page(self) -> Gtk.Widget:
        self._project_center = None
        self._project_center_lock = threading.Lock()
        self._projects_closed = False          # shut down: no center is opened again, answers are dropped
        self._projects_render_queued = False
        self.projects_ctrl = ProjectsController(self._project_call, self._projects_runner, self._project_ask,
                                                self._projects_changed)
        colors = lambda: self.theme.colors        # noqa: E731  (read when drawn: follows the theme)
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP)
        self.projects_title = Gtk.Label(label=_("Projects").upper())
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text=_("Refresh") + " (F5)",
                             valign=Gtk.Align.CENTER)
        refresh.add_css_class("flat")
        refresh.connect("clicked", lambda *_a: self.projects_refresh())
        self.projects_refresh_button = refresh
        self.projects_header = self.page_header(self.projects_title, self.close_projects,
                                                _("Back (Backspace, Esc)"), end=(Gtk.Box(hexpand=True), refresh))
        page.append(self.projects_header)
        self.projects_progress = Gtk.ProgressBar(visible=False)
        self.projects_progress.add_css_class("osd")
        page.append(self.projects_progress)
        self.projects_stack = Gtk.Stack(vexpand=True, hexpand=True)
        loading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=ai_core.GAP, valign=Gtk.Align.CENTER)
        self.projects_spinner = Adw.Spinner()
        loading.append(self.projects_spinner)
        self.projects_loading_note = Gtk.Box(spacing=ai_core.GAP_INLINE, halign=Gtk.Align.CENTER)
        self.projects_loading_note.append(parts.icon("network-offline-symbolic"))
        self.projects_loading_note.append(parts.label(_("Project status unavailable")))
        retry = Gtk.Button(label=_("Retry"))
        retry.connect("clicked", lambda *_a: self.projects_ctrl.refresh(profiles=True, force=True))
        self.projects_loading_note.append(retry)
        loading.append(self.projects_loading_note)
        self.projects_stack.add_named(loading, "loading")
        self.projects_board = ProjectsBoard(self.projects_ctrl, colors, self)
        self.projects_stack.add_named(self.projects_board.root, "board")
        self.projects_detail = ProjectDetail(self.projects_ctrl, colors, self)
        self.projects_stack.add_named(self.projects_detail.root, "project")
        page.append(self.projects_stack)
        page.connect("map", lambda *_a: self._projects_shown(True))
        page.connect("unmap", lambda *_a: self._projects_shown(False))
        page.connect("destroy", lambda *_a: self.projects_teardown())
        self._projects_keys = Gtk.EventControllerKey()
        # Before the fields: a read-only text box (a running project's plan) must not eat Backspace.
        self._projects_keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        self._projects_keys.connect("key-pressed", self._projects_key)
        self.window.add_controller(self._projects_keys)
        self._projects_tick_id = GLib.timeout_add_seconds(POLL_SECONDS, self._projects_tick)
        self.projects_page = page
        return page

    # --- entry points ------------------------------------------------------------------------------

    def open_projects(self, agent: str | None = None, name: str = ""):
        """Show the Projects page (WP7 makes it the Projects tab); agent: the map's "Used in N
        projects" chip for that agent command (the board is shown, an open project is left first)."""
        parts.apply_css(self.theme.colors, getattr(self, "window_rounding", 10) or 10)
        ctrl = self.projects_ctrl
        if agent is not None:
            def left(ok):
                if ok:
                    ctrl.set_agent(agent, name)
            if ctrl.open:
                ctrl.back_to_board(left)
            else:
                left(True)
        self.show_page("projects")

    def close_projects(self):
        """Back to the AI Map (unsaved changes on an open project ask first)."""
        def left(ok):
            if ok:
                self.show_view(getattr(self, "view", "main"), save=False)
        self.projects_prepare_leave(left)

    def projects_prepare_leave(self, done):
        """The shell's guard before it leaves the page: done(True) to go on."""
        ctrl = self.projects_ctrl
        if ctrl.confirming or ctrl.leaving:
            done(False)                        # its own question is open
            return
        if ctrl.busy:
            self.toast(_("Wait for the current step"), 4)
            done(False)
            return
        ctrl.before_leave(done)

    def projects_refresh(self):
        """Refresh and F5: the list again, agents and runtimes looked at again (not while a step runs)."""
        if not self.projects_ctrl.busy:
            self.projects_ctrl.refresh(profiles=True, force=True)

    def projects_teardown(self):
        """The page is gone or the app ends: the poll stops, answers on their way change nothing."""
        tick, self._projects_tick_id = getattr(self, "_projects_tick_id", None), None
        if tick is not None:
            GLib.source_remove(tick)
        keys, self._projects_keys = getattr(self, "_projects_keys", None), None
        owner = keys.get_widget() if keys is not None else None
        if owner is not None:
            owner.remove_controller(keys)
        if getattr(self, "projects_ctrl", None) is not None:
            self.projects_ctrl.dispose()

    def shutdown_projects(self):
        """Closing the app stops its project agents: the center is released without waiting on the GTK
        thread (each supervisor stops its agent's whole tree, and a later start shows the work as
        interrupted); no center is opened again afterwards."""
        if not hasattr(self, "_project_center_lock"):
            return
        # The fence first (no answer is taken, no center is opened again), the page at once; the center
        # is released on a worker: its locks may be held by a scan or a save for a while. The thread is
        # not a daemon, so the process waits for it after the window is gone.
        self._projects_closed = True
        self.projects_teardown()

        def release():
            with self._project_center_lock:
                center, self._project_center = self._project_center, None
            if center is not None:
                try:
                    center.detach()
                except Exception:
                    pass
        self._projects_release = threading.Thread(target=release, name="project-center-release")
        self._projects_release.start()

    # --- the backend -------------------------------------------------------------------------------

    def _project_call(self, operation: str, params: dict) -> dict:
        """On a worker thread: the in-process ProjectCenter, opened on first use (never after shutdown).
        A list with refresh (F5) looks at agents and runtimes again (the host's discovery generation)."""
        with self._project_center_lock:
            if self._projects_closed:
                raise ProjectError("project_center_closed")
            if self._project_center is None:
                import project_center
                self._project_center = project_center.ProjectCenter(
                    on_change=lambda _pid: GLib.idle_add(self._projects_poke))
            center = self._project_center
        refocused = operation == "list" and getattr(self, "projects_refocus", False)
        if refocused:
            self.projects_refocus = False       # consumed once (the window came back to the front)
        if operation == "list" and (params.get("refresh") is True or refocused):
            bump = getattr(center, "bump_discovery", None)
            if bump is not None:
                bump()                         # F5: under the center's own lock
            else:
                center.discovery_generation += 1
        if operation == "list":
            params = {}
        return center.call(operation, params)

    def _projects_runner(self, work, done):
        """worker_runner whose answers are dropped once the page is gone."""
        def answered(result, error):
            if not self._projects_closed and not self.projects_ctrl.disposed:
                done(result, error)
        worker_runner(work, answered)

    def _projects_poke(self):
        ctrl = self.projects_ctrl
        if not self._projects_closed and not ctrl.disposed and ctrl.active:
            ctrl.refresh()
        return False

    def _project_ask(self, kind: str, question, answer):
        def answered(value):
            if not self.projects_ctrl.disposed:
                answer(value)
        if kind == "leave":
            dialog = Adw.AlertDialog(heading=_("Save project changes?"))
            for response, text in (("cancel", _("Cancel")), ("discard", _("Discard")), ("save", _("Save"))):
                dialog.add_response(response, text)
            dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
            dialog.set_response_appearance("discard", Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_default_response("save")
            dialog.set_close_response("cancel")
            dialog.connect("response", lambda _d, response: answered(response))
        else:
            title, body, action = question
            dialog = Adw.AlertDialog(heading=_(title), body=_(body))
            dialog.add_response("cancel", _("Cancel"))
            dialog.add_response("ok", _(action))
            dialog.set_response_appearance("ok", Adw.ResponseAppearance.DESTRUCTIVE if action == "Delete"
                                           else Adw.ResponseAppearance.SUGGESTED)
            dialog.set_default_response("cancel")
            dialog.set_close_response("cancel")
            dialog.connect("response", lambda _d, response: answered(response == "ok"))
        dialog.present(self.window)

    # --- drawing -----------------------------------------------------------------------------------

    def _projects_changed(self):
        if not self._projects_render_queued and not self.projects_ctrl.disposed:
            self._projects_render_queued = True
            GLib.idle_add(self._projects_render)

    def _projects_render(self):
        self._projects_render_queued = False
        ctrl = self.projects_ctrl
        if ctrl.disposed or self.projects_page.get_root() is None:
            return False
        if not ctrl.loaded:
            self.projects_stack.set_visible_child_name("loading")
            self.projects_spinner.set_visible(not ctrl.unavailable)
            self.projects_loading_note.set_visible(ctrl.unavailable)
        else:
            self.projects_board.render()
            if ctrl.open:
                self.projects_detail.render()
            self.projects_stack.set_visible_child_name("project" if ctrl.open else "board")
        self.projects_refresh_button.set_sensitive(not ctrl.busy)
        working = ctrl.busy and not ctrl.confirming and not ctrl.leaving
        self.projects_progress.set_visible(working)
        if working:
            self.projects_progress.pulse()
        return False

    def _projects_shown(self, shown: bool):
        if self.projects_ctrl.disposed:
            return
        self.projects_ctrl.set_active(shown)
        if shown:
            self._projects_render()

    def _projects_tick(self):
        if self.projects_ctrl.disposed:
            self._projects_tick_id = None
            return False
        if self.projects_ctrl.busy and self.projects_progress.get_visible():
            self.projects_progress.pulse()
        self.projects_ctrl.tick()
        return True

    def _projects_key(self, _controller, keyval, _keycode, state):
        if self.page_name() != "projects":
            return False
        blocked = getattr(self, "dialog_open", None)
        visible = getattr(self.window, "get_visible_dialog", None)
        if (callable(blocked) and blocked()) or (blocked is None and callable(visible) and visible() is not None):
            return False                       # a dialog (or an agent's task box) keeps its keys
        ctrl = self.projects_ctrl
        if ctrl.confirming or ctrl.leaving or state & MODIFIERS:
            return False                       # keys do nothing under its questions
        if keyval == Gdk.KEY_F5:
            self.projects_refresh()
            return True
        if keyval not in (Gdk.KEY_Escape, Gdk.KEY_BackSpace):
            return False
        if self.editing():
            if keyval == Gdk.KEY_BackSpace:
                return False
            self.window.set_focus(None)        # Esc first leaves the field
            if ctrl.open:
                self.projects_detail.breadcrumb.grab_focus()
            return True
        if ctrl.busy:
            self.toast(_("Wait for the current step"), 4)
            return True
        if ctrl.open:
            ctrl.back_to_board()
            return True
        escape = getattr(self, "projects_escape", None)
        if escape is None:
            self.close_projects()              # no tabs around it (tests): back to the map
            return True
        if keyval == Gdk.KEY_BackSpace:
            return True                        # the board is a tab: nothing to go back to
        escape()                               # Esc on the board closes the window (Maprisma, task 070)
        return True
