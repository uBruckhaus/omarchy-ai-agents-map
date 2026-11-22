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
from projects_page import ProjectsPage  # noqa: E402
from test_projects_ui import RUNNING, FakeCenter, card  # noqa: E402


class Host(PageParts, ProjectsPage):
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
        return False


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


def main():
    Adw.init()
    window = Gtk.Window(default_width=1200, default_height=800)
    host = Host(window)
    messages = [{"id": "m1", "at": "2026-11-22T10:00:00Z", "mode": "queue", "text": "Check the tests", "state": "queued",
                 "run_id": "run-1"},
                {"id": "m0", "at": "2026-11-22T09:00:00Z", "mode": "queue", "text": "Earlier", "state": "delivered",
                 "run_id": "run-1", "delivered_at": "2026-11-22T09:05:00Z",
                 "delivered_to": {"phase": "execution", "position": 0, "agent": "claude", "role": "lead"}}]
    center = FakeCenter([
        card("run", title="Running one", messages_queued=1, messages=messages, **RUNNING),
        card("blocked", title="Blocked one", status="ready", stage="ready", activity="approved_blocked",
             bucket="needs_you", importance="high", approval_valid=True,
             issues=[{"code": "runtime_stopped", "target": "runtime", "node": "runtime:llama", "fix": "map"}]),
        card("draft", title="A draft", last_event={"at": "2026-11-22T08:00:00Z", "type": "saved",
                                                   "text": "Project created."}),
        card("done", title="Finished", status="completed", stage="finished", activity="agents_finished",
             bucket="finished", importance="low"),
    ])
    page = host.build_projects_page()
    host._project_center = center            # no real center: the fake answers
    window.set_child(page)
    window.present()
    host.open_projects()                     # the pages' style sheet
    ctrl = host.projects_ctrl
    spin(lambda: ctrl.loaded and host.projects_stack.get_visible_child_name() == "board")
    board = host.projects_board
    spin(lambda: len(board.order) == 4)
    shown = labels(board.root)
    for text in ("Running one", "Blocked one", "Approved · blocked", "A local runtime is stopped", "Go to Team",
                 "All 4", "Running 1", "Needs you 1", "Agents finished 1", "Pause", "Open", "High", "Low"):
        assert text in shown, (text, shown)
    shot(window, "board")
    # The trash symbol is off while agents work, on for a draft.
    running_card = board.cards["run"][0]
    assert not buttons(running_card, "Delete project")[0].get_sensitive()
    assert buttons(board.cards["draft"][0], "Delete project")[0].get_sensitive()
    assert "1" in labels(running_card)                       # the messages badge
    # Fix on map appears once the shell links to the map.
    host.projects_map_fix = lambda node: None
    board.cards.clear()
    ctrl.notify()
    spin(lambda: "Fix on map" in labels(board.root))
    # Filters narrow the cards; All clears.
    ctrl.set_filter("needs_you")
    spin(lambda: board.order == ["blocked"])
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
    ctrl.set_view("cards")
    board.on_width(1200)
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
    assert not detail.messages.send_button.get_sensitive()   # empty box
    detail.messages.text.get_buffer().set_text("More tests please")
    assert detail.messages.send_button.get_sensitive()
    assert ctrl.message_drafts["run"] == "More tests please"
    shot(window, "activity")
    ctrl.set_tab(texts.TAB_TEAM)
    spin(lambda: "No agents in this phase yet." in labels(detail.root))
    shot(window, "team")
    # Narrow: tabs as a list, the footer keeps one action and More.
    detail.on_width(500)
    spin(lambda: isinstance(detail.tabs.get_first_child(), Gtk.DropDown))
    assert any(isinstance(w, Gtk.MenuButton) for w in [detail.footer.get_last_child()])
    detail.on_width(1200)
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
    print("SMOKE OK")


if __name__ == "__main__":
    main()
