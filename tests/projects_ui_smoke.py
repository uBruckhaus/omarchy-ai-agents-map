"""A real GTK build of the Projects pages (run by test_projects_ui.GtkBuildTest under a headless
Broadway display with an isolated HOME): the board with cards in several states, the Phases view,
the project page with each tab, narrow layouts and the message box. Prints SMOKE OK."""

import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import gi  # noqa: E402
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Graphene, Gtk  # noqa: E402

import palette  # noqa: E402
import project_texts as texts  # noqa: E402
from page_parts import PageParts  # noqa: E402
from text_guard import TextGuard  # noqa: E402
from projects_page import ProjectsPage  # noqa: E402
from test_projects_ui import RUNNING, FakeCenter, card  # noqa: E402


class Host(PageParts, TextGuard, ProjectsPage):
    def __init__(self, window):
        self.window = window
        self.theme = SimpleNamespace(colors=dict(palette.DEFAULT_COLORS))
        self.toasts_shown = []
        self.view = "main"

    def toast(self, message, seconds, *_a, **_k):
        self.toasts_shown.append(message)

    def page_name(self):
        return "projects"

    def show_page(self, name):
        pass

    def show_view(self, name, save=True):
        self.view = name

    def editing(self):
        focus = self.window.get_focus()
        if isinstance(focus, Gtk.TextView):
            return focus.get_editable()
        return isinstance(focus, Gtk.Editable) and focus.get_editable()


def spin(until, seconds=5.0):
    context = GLib.MainContext.default()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        if until():
            return True
        time.sleep(0.01)
    raise AssertionError("timed out")


def spin_for(seconds):
    context = GLib.MainContext.default()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        time.sleep(0.01)


def walk(widget):
    stack, found = [widget], []
    while stack:
        item = stack.pop()
        found.append(item)
        child = item.get_first_child()
        while child is not None:
            stack.append(child)
            child = child.get_next_sibling()
    if isinstance(widget, Gtk.Popover) and widget.get_child() is not None and widget.get_child() not in found:
        found += walk(widget.get_child())
    return found


def children(flow):
    count, child = 0, flow.get_first_child()
    while child is not None:
        count += 1
        child = child.get_next_sibling()
    return count


def center_moves(center):
    return [c for c in center.calls if c[0] == "move"]


def labels(widget):
    found = []
    stack = [widget]
    while stack:
        item = stack.pop()
        if isinstance(item, Gtk.Label):
            found.append(item.get_label())
        if isinstance(item, Gtk.Button) and item.get_label():
            found.append(item.get_label())
        child = item.get_first_child()
        while child is not None:
            stack.append(child)
            child = child.get_next_sibling()
    return found


def buttons(widget, label):
    found = []
    stack = [widget]
    while stack:
        item = stack.pop()
        if isinstance(item, Gtk.Button) and (item.get_label() == label or item.get_tooltip_text() == label):
            found.append(item)
        child = item.get_first_child()
        while child is not None:
            stack.append(child)
            child = child.get_next_sibling()
    return found


def shot(window, name):
    """With PROJECTS_SMOKE_SHOTS=<folder>: the window as a PNG (to compare the look with Maprisma's)."""
    folder = os.environ.get("PROJECTS_SMOKE_SHOTS")
    if not folder:
        return
    spin_for(0.4)
    child = window.get_child()
    width, height = child.get_width(), child.get_height()
    snapshot = Gtk.Snapshot()
    rect = Graphene.Rect().init(0, 0, width, height)
    snapshot.append_color(Gdk.RGBA(red=0.12, green=0.12, blue=0.18, alpha=1), rect)
    window.snapshot_child(child, snapshot)
    node = snapshot.to_node()
    if node is not None:
        window.get_renderer().render_texture(node, None).save_to_png(os.path.join(folder, f"{name}.png"))


def detail_ready(host):
    return host.projects_detail.ctrl.project is not None


def main():
    Adw.init()
    window = Gtk.Window(default_width=1200, default_height=800)
    host = Host(window)
    messages = [{"id": "m1", "at": "2026-11-22T10:00:00Z", "mode": "queue", "text": "Check the tests", "state": "queued",
                 "run_id": "run-1"},
                {"id": "m0", "at": "2026-11-22T09:00:00Z", "mode": "queue", "text": "Earlier", "state": "delivered",
                 "run_id": "run-1", "delivered_at": "2026-11-22T09:05:00Z",
                 "delivered_to": {"phase": "execution", "position": 0, "agent": "claude", "role": "lead"}}]
    import datetime as dt
    next_five = (dt.datetime.now().astimezone() + dt.timedelta(days=1)).replace(hour=5, minute=0, second=0,
                                                                               microsecond=0)
    center = FakeCenter([
        card("run", title="Running one", messages_queued=1, messages=messages, **RUNNING),
        card("blocked", title="Blocked one", status="ready", stage="ready", activity="approved_blocked",
             bucket="needs_you", importance="high", approval_valid=True,
             issues=[{"code": "runtime_stopped", "target": "runtime", "node": "runtime:llama", "fix": "map"}]),
        card("draft", title="A draft", last_event={"at": "2026-11-22T08:00:00Z", "type": "saved",
                                                   "text": "Project created."}),
        card("done", title="Finished", status="completed", stage="finished", activity="agents_finished",
             bucket="finished", importance="low", schedule={"kind": "daily", "time": "05:00"},
             next_run=next_five.isoformat(timespec="minutes")),
    ])
    page = host.build_projects_page()
    host._project_center = center            # no real center: the fake answers
    window.set_child(page)
    window.present()
    host.open_projects()                     # the pages' style sheet
    ctrl = host.projects_ctrl
    spin(lambda: ctrl.loaded and host.projects_stack.get_visible_child_name() == "board")
    board = host.projects_board
    # The breakpoints follow the real allocation (Broadway's screen is 1024 px wide).
    spin(lambda: board.width == board.root.get_width() and board.width > 900)
    assert board.flow.get_max_children_per_line() == 2
    board = host.projects_board
    spin(lambda: len(board.order) == 4)
    shown = labels(board.root)
    for text in ("Running one", "Blocked one", "Approved · blocked", "A local runtime is stopped", "Go to Team",
                 "All 4", "Running 1", "Needs you 1", "Agents finished 1", "Pause", "Open", "High", "Low"):
        assert text in shown, (text, shown)
    shot(window, "board")
    # A scheduled project's card: a small clock with its next run (none on the others).
    assert next_five.strftime("%a 05:00") in labels(board.cards["done"][0]), labels(board.cards["done"][0])
    assert next_five.strftime("%a 05:00") not in labels(board.cards["draft"][0])
    # The trash symbol is off while agents work, on for a draft.
    running_card = board.cards["run"][0]
    assert not buttons(running_card, "Delete project")[0].get_sensitive()
    assert buttons(board.cards["draft"][0], "Delete project")[0].get_sensitive()
    assert "1" in labels(running_card)                       # the messages badge
    import project_working

    def bars(widget):
        found, stack = 0, [widget]
        while stack:
            item = stack.pop()
            found += isinstance(item, project_working.WorkingBar)
            child = item.get_first_child()
            while child is not None:
                stack.append(child)
                child = child.get_next_sibling()
        return found
    assert bars(running_card) == 1 and bars(board.cards["draft"][0]) == 0    # task 098
    # Fix on map appears once the shell links to the map.
    host.projects_map_fix = lambda node: None
    board.cards = {pid: (child, None) for pid, (child, _sig) in board.cards.items()}
    ctrl.notify()
    spin(lambda: "Fix on map" in labels(board.root))
    # Filters narrow the cards; All clears.
    ctrl.set_filter("needs_you")
    spin(lambda: board.order == ["blocked"])
    ctrl.set_filter(None)
    spin(lambda: len(board.order) == 4)
    # All stays on when it is clicked again with no filter chosen.
    assert board.all_chip.get_active()
    board.all_chip.emit("clicked")
    spin(lambda: True, 0.1)
    assert board.all_chip.get_active() and ctrl.filter is None
    spin(lambda: len(board.order) == 4)
    # Resizing across every breakpoint keeps one child per card (no duplicates left behind).
    for width in (500, 800, 1200, 650, 1200):
        board.on_width(width)
        spin(lambda: True, 0.05)
        assert children(board.flow) == 4, (width, children(board.flow))
    # Cards are moved by dragging the whole card: no Move menu any more, the grip only shows it.
    ctrl.set_filter("drafts")
    spin(lambda: board.order == ["draft"])
    ctrl.set_filter(None)
    spin(lambda: len(board.order) == 4)
    ctrl.set_filter("needs_you")
    spin(lambda: board.order == ["blocked"])
    card_child = board.cards["blocked"][0]
    assert not [w for w in walk(card_child) if isinstance(w, Gtk.MenuButton) and w.get_tooltip_text() == "Move"]
    sources = [c for c in card_child.observe_controllers() if isinstance(c, Gtk.DragSource)]
    assert len(sources) == 1, sources                  # the whole card is the drag source
    # A drag shows separators on the board and fades the dragged card; dropping or cancelling ends both.
    assert board.drag_start("blocked", 20, 10) is not None
    board.drag_begin(sources[0], "blocked")
    assert board.flow.has_css_class("am-board-dragging")
    assert card_child.get_child().has_css_class("am-project-dragged")
    board.drag_end()
    assert not board.flow.has_css_class("am-board-dragging")
    assert not card_child.get_child().has_css_class("am-project-dragged")
    # By touch a drag starts only on the grip (a swipe on the card scrolls the board).
    spin(lambda: card_child.get_height() > 0)
    ok, bounds = board.grips["blocked"].compute_bounds(card_child)
    assert ok and card_child.get_height() > 0, (card_child.get_width(), card_child.get_height())
    assert board.on_grip("blocked", bounds.get_x() + bounds.get_width() / 2, bounds.get_y() + bounds.get_height() / 2)
    assert not board.on_grip("blocked", 4, card_child.get_height() - 4), (
        bounds.get_x(), bounds.get_y(), bounds.get_width(), bounds.get_height())
    # A card whose importance menu is open is not dragged.
    board.popovers.add("blocked")
    assert board.drag_start("blocked") is None
    board.popovers.discard("blocked")
    # Dragging: no drop onto the card itself, nothing from outside the board.
    assert board.drop_hint("blocked", True) == Gdk.DragAction(0)
    assert board.dropped("blocked", "draft") is False and not center_moves(center)
    ctrl.set_filter(None)
    spin(lambda: len(board.order) == 4)
    # Phases: navigation only.
    ctrl.set_view("phases")
    spin(lambda: board.content.get_visible_child_name() == "phases")
    assert set(board.phases.cards) == {"run", "blocked", "draft", "done"}
    assert "Pause" not in labels(board.phases.root)
    shot(window, "phases")
    board.on_width(500)
    spin(lambda: board.phases.cards and "Goal" in labels(board.phases.root))
    assert next_five.strftime("%a 05:00") not in labels(board.phases.root)     # the Phases view is unchanged
    ctrl.set_view("cards")
    board.on_width(1200)
    # The scheduled project's Goal tab: its line, the form, Save schedule only once the form differs.
    ctrl.open_card(next(p for p in ctrl.projects if p["id"] == "done"))
    spin(lambda: host.projects_stack.get_visible_child_name() == "project" and detail_ready(host))
    detail = host.projects_detail
    ctrl.set_tab(texts.TAB_GOAL)
    spin(lambda: "Schedule" in labels(detail.schedule_box))
    goal = labels(detail.schedule_box)
    assert f"Every day at 05:00 · next run {next_five.strftime('%a 05:00')}" in goal, goal
    for text in ("Repeat", "At", "Ends", "Save schedule"):
        assert text in goal, (text, goal)
    save = buttons(detail.schedule_box, "Save schedule")[0]
    assert not save.get_sensitive()
    shot(window, "schedule")
    detail.schedule_spins[0].set_value(6)                     # typing a time keeps the form (and its focus)
    spin(lambda: save.get_sensitive())
    assert buttons(detail.schedule_box, "Save schedule")[0] is save
    detail.schedule_edit(kind="weekdays", rebuild=True)
    spin(lambda: "Mon" in labels(detail.schedule_box) and "On" in labels(detail.schedule_box))
    days = [w for w in walk(detail.schedule_box) if isinstance(w, Gtk.ToggleButton) and w.get_label() in ("Mon", "Tue")]
    assert len(days) == 2 and all(d.get_active() for d in days)
    for button in [w for w in walk(detail.schedule_box) if isinstance(w, Gtk.ToggleButton)
                   and w.get_label() in ("Mon", "Tue", "Wed", "Thu", "Fri")]:
        button.set_active(False)
    spin(lambda: "Choose at least one day." in labels(detail.schedule_box))
    assert not buttons(detail.schedule_box, "Save schedule")[0].get_sensitive()
    next(d for d in days if d.get_label() == "Tue").set_active(True)
    spin(lambda: buttons(detail.schedule_box, "Save schedule")[0].get_sensitive())
    shot(window, "schedule-weekdays")
    buttons(detail.schedule_box, "Save schedule")[0].emit("clicked")
    spin(lambda: any(c[0] == "set_schedule" for c in center.calls) and not ctrl.scheduling)
    sent = [c for c in center.calls if c[0] == "set_schedule"][-1][1]
    assert sent == {"id": "done", "schedule": {"kind": "weekdays", "time": "06:00", "days": ["tue"]}}, sent
    spin(lambda: detail.schedule_draft is None)
    for kind, text in (("hours", "How often"), ("monthly", "On day"), ("weekly", "On")):
        detail.schedule_edit(kind=kind, rebuild=True)
        spin(lambda text=text: text in labels(detail.schedule_box))
    detail.schedule_edit(until="2031-01-31", rebuild=True)
    spin(lambda: "31 Jan 2031" in labels(detail.schedule_box))
    detail.on_width(500)                                      # narrow: the rows stack
    spin(lambda: "Repeat" in labels(detail.schedule_box))
    detail.on_width(1200)
    detail.schedule_edit(kind="off", rebuild=True)
    spin(lambda: "At" not in labels(detail.schedule_box))
    buttons(detail.schedule_box, "Save schedule")[0].emit("clicked")
    spin(lambda: [c for c in center.calls if c[0] == "set_schedule"][-1][1]["schedule"] is None and not ctrl.scheduling)
    ctrl.back_to_board()
    spin(lambda: not ctrl.open)
    # The running project's page: rail, tabs, message box.
    ctrl.open_card(next(p for p in ctrl.projects if p["id"] == "run"))
    spin(lambda: host.projects_stack.get_visible_child_name() == "project")
    detail = host.projects_detail
    for tab in range(len(texts.TABS)):
        ctrl.set_tab(tab)
        spin(lambda tab=tab: detail.pages.get_visible_child_name() == str(tab))
    ctrl.set_tab(texts.TAB_ACTIVITY)
    spin(lambda: detail.messages.root.get_visible())
    page_text = labels(detail.root)
    for text in ("Running one", "Message to the team", "Check the tests", "Send now", "Send", "Agent output",
                 "Pause project", "Plan", "Agents finished"):
        assert text in page_text, (text, page_text)
    assert any(t.startswith("Delivered to Claude Code") for t in page_text), page_text
    # The working strip under the tabs (task 098), the bar on the running card.
    assert detail.working.root.get_visible()
    assert "AI is working on the project…" in page_text, page_text
    assert not detail.messages.send_button.get_sensitive()   # empty box
    detail.messages.text.get_buffer().set_text("More tests please")
    assert detail.messages.send_button.get_sensitive()
    assert ctrl.message_drafts["run"] == "More tests please"
    shot(window, "activity")
    ctrl.set_tab(texts.TAB_TEAM)
    spin(lambda: "No agents in this phase yet." in labels(detail.root))
    shot(window, "team")
    # Keys: Backspace leaves even from a read-only text box (the keys come first, CAPTURE); nothing
    # happens under the page's own questions; Ctrl+Shift+Enter does not send.
    assert host._projects_keys.get_propagation_phase() == Gtk.PropagationPhase.CAPTURE
    ctrl.confirming = True
    assert host._projects_key(None, Gdk.KEY_F5, 0, 0) is False
    ctrl.confirming = False
    shift_ctrl = Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK
    assert detail.messages.on_key(None, Gdk.KEY_Return, 0, shift_ctrl) is False
    assert board.on_card_key(None, Gdk.KEY_Up, 0, shift_ctrl) is False
    sent = len([c for c in center.calls if c[0] == "message"])
    assert detail.messages.on_key(None, Gdk.KEY_Return, 0, Gdk.ModifierType.CONTROL_MASK) is True
    spin(lambda: len([c for c in center.calls if c[0] == "message"]) == sent + 1 and not ctrl.busy)
    # Narrow: tabs as a list, the footer keeps one action and More.
    detail.on_width(500)
    spin(lambda: isinstance(detail.tabs.get_first_child(), Gtk.DropDown))
    assert any(isinstance(w, Gtk.MenuButton) for w in [detail.footer.get_last_child()])
    detail.on_width(1200)
    ctrl.set_tab(texts.TAB_PLAN)
    spin(lambda: detail.pages.get_visible_child_name() == str(texts.TAB_PLAN))
    assert not detail.plan_view.get_editable()                # the project runs
    detail.plan_view.grab_focus()
    assert host._projects_key(None, Gdk.KEY_BackSpace, 0, 0) is True
    spin(lambda: host.projects_stack.get_visible_child_name() == "board")
    ctrl.open_card(next(p for p in ctrl.projects if p["id"] == "run"))
    spin(lambda: host.projects_stack.get_visible_child_name() == "project")
    # Back to the board: the card gets the keyboard.
    ctrl.back_to_board()
    spin(lambda: host.projects_stack.get_visible_child_name() == "board")
    spin(lambda: ctrl.reveal is None)
    # A new project's page.
    ctrl.open_card(None)
    spin(lambda: host.projects_stack.get_visible_child_name() == "project")
    assert "New project" in labels(detail.root)
    detail.title_entry.set_text("Typed")
    assert ctrl.dirty
    # A plan typed into a draft that has changes already: Approve plan follows at once.
    ctrl.set_tab(texts.TAB_PLAN)
    spin(lambda: detail.pages.get_visible_child_name() == str(texts.TAB_PLAN))
    approve = lambda: next(b for b in buttons(detail.footer, "Approve plan"))   # noqa: E731
    assert not approve().get_sensitive()
    detail.plan_view.get_buffer().set_text("1. Do it.")
    spin(lambda: approve().get_sensitive())
    # Team: the graph leaves the width to a new project's form; new profile facts redraw the form,
    # the typed text and the keyboard stay; the fields are guarded.
    ctrl.set_tab(texts.TAB_TEAM)
    spin(lambda: detail.pages.get_visible_child_name() == str(texts.TAB_TEAM))
    assert not detail.team_graph.get_visible()
    entry = next(w for w in walk(detail.team_form) if getattr(w, "field_key", None) == ("run", 0, "instructions"))
    assert entry.get_delegate()._am_guarded
    entry.grab_focus()
    entry.set_text("Be brief")
    ctrl.profiles = [dict(ctrl.profiles[0], permissions="Edits files in the project only")]
    ctrl.refresh()
    ctrl.profiles = [dict(ctrl.profiles[0], permissions="Edits files in the project only")]
    ctrl.notify()
    spin(lambda: "Edits files in the project only" in labels(detail.team_form))
    again = next(w for w in walk(detail.team_form) if getattr(w, "field_key", None) == ("run", 0, "instructions"))
    assert again is not entry and again.get_text() == "Be brief"
    assert window.get_focus() is again.get_delegate() or window.get_focus() is again, window.get_focus()
    ctrl.set_tab(texts.TAB_SETUP)
    spin(lambda: detail.pages.get_visible_child_name() == str(texts.TAB_SETUP))
    assert detail.request_view._am_guarded
    # Dropdowns: a missing value is shown as itself, a disabled choice goes back and is not applied.
    from project_detail_page import dropdown
    picked = []
    drop = dropdown([("a", "A", True), ("b", "B (stopped)", False)], "gone", picked.append, missing="Gone (missing)")
    assert drop.get_selected() == 0 and drop.get_model().get_string(0) == "Gone (missing)"
    drop.set_selected(2)
    spin(lambda: drop.get_selected() == 0)
    assert picked == []
    drop.set_selected(1)
    assert picked == ["a"]
    # Deleting the last card: the keyboard goes to New project, not to a hidden old card.
    ctrl.ask = lambda kind, question, answer: answer("discard" if kind == "leave" else True)
    ctrl.back_to_board()
    spin(lambda: not ctrl.open)
    center.projects = []
    ctrl.refresh()
    spin(lambda: board.content.get_visible_child_name() == "empty" and children(board.flow) == 0)
    ctrl.reveal_card("run")
    ctrl.notify()
    spin(lambda: ctrl.reveal is None)
    assert window.get_focus() is board.empty_new, window.get_focus()
    # A narrow, low window: the real allocation makes the board one column and the page compact.
    narrow = Gtk.Window(default_width=520, default_height=420)
    window.set_child(None)
    narrow.set_child(page)
    host.window = narrow
    narrow.present()
    spin(lambda: 0 < board.width < 600)
    center.projects = [card("x")]
    ctrl.refresh()
    spin(lambda: board.order == ["x"])
    assert board.flow.get_max_children_per_line() == 1
    ctrl.open_card(ctrl.projects[0])
    spin(lambda: host.projects_stack.get_visible_child_name() == "project")
    spin(lambda: detail.narrow and isinstance(detail.tabs.get_first_child(), Gtk.DropDown))
    ctrl.back_to_board()
    spin(lambda: not ctrl.open)
    # Shutdown: the poll stops, no center is opened again, late answers change nothing.
    host.shutdown_projects()
    assert host._projects_tick_id is None and host._schedule_tick_id is None and ctrl.disposed
    host._projects_release.join(5)
    assert not host._projects_release.is_alive() and host._project_center is None
    try:
        host._project_call("list", {})
        raise AssertionError("a center after shutdown")
    except Exception as error:
        assert getattr(error, "code", None) == "project_center_closed", error
    # The folder dialog (task 098): one at a time; it closes without an answer when its window hides.
    import folder_picker
    chosen = []
    owner = Gtk.Window()
    owner.present()
    first = folder_picker.choose_folder(owner, "Working folder", "/", chosen.append)
    assert folder_picker.choose_folder(owner, "Working folder", "/", chosen.append) is first
    owner.set_visible(False)
    spin(lambda: folder_picker._open is None)
    assert chosen == []
    owner.present()
    second = folder_picker.choose_folder(owner, "Working folder", "/", chosen.append)
    assert second is not first
    owner.destroy()                                   # destroyed without hiding first
    spin(lambda: folder_picker._open is None)
    third = folder_picker.choose_folder(None, "Working folder", "/", chosen.append)
    third.destroy()                                   # gone without an answer: the next one opens
    assert folder_picker._open is None and chosen == []
    import gc
    finalized = []
    for dialog in (first, second, third):
        dialog.weak_ref(lambda: finalized.append(1))   # GObject level: the GTK object itself is freed
    del first, second, third, dialog
    gc.collect()
    spin(lambda: len(finalized) == 3)                 # nothing of a closed dialog stays (no cycle)
    print("SMOKE OK")


if __name__ == "__main__":
    main()
