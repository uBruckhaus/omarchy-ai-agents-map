"""A real GTK build of the window's header (run by test_tab_links.HeaderNarrowTest under a headless
Broadway display with an isolated HOME): wide, every symbol in the row; narrow (600 px), Reports, Help,
Settings and Refresh in More and nothing cut off. Prints HEADER OK."""

import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import gi  # noqa: E402
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

import ai_popup  # noqa: E402
from main_tabs import MainTabs  # noqa: E402

WINDOW_BUTTONS = 110
COPIED = ("build_header", "build_header_more", "header_clipper", "update_header_layout", "setup_header_order", "apply_header_order",
          "header_items", "header_position", "header_item_at")


class Host(MainTabs):
    HEADER_ITEMS = ai_popup.AgentsMap.HEADER_ITEMS
    HEADER_NARROW = ai_popup.AgentsMap.HEADER_NARROW
    MORE_ITEMS = ai_popup.AgentsMap.MORE_ITEMS

    def __init__(self, window):
        self.window = window
        self.project_summary = {"connected": True, "running": 1, "needs_you": 1}
        self.opened = []

    def page_name(self):
        return "main"

    def after_leaving(self, action):
        action()

    def open_reports(self, tab="log"):
        self.opened.append("reports")

    def open_help(self):
        self.opened.append("help")

    def open_settings(self):
        self.opened.append("settings")

    def refresh_everything(self):
        self.opened.append("refresh")

    def to_map_first(self, then):
        then()

    def open_health(self):
        pass

    def open_ask_ai(self, *a, **k):
        pass

    def open_legend(self):
        pass


for name in COPIED:
    setattr(Host, name, getattr(ai_popup.AgentsMap, name))


def spin(seconds):
    context = GLib.MainContext.default()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        time.sleep(0.01)


def check(width: int):
    window = Gtk.Window(default_width=width, default_height=200)
    host = Host(window)
    header = host.build_header()
    holder = Gtk.Box()
    holder.append(host.header_clipper(header))
    header.set_hexpand(True)
    window.set_child(holder)
    window.present()
    spin(0.6)
    host.update_header_layout()
    spin(0.6)
    narrow = width < Host.HEADER_NARROW
    assert host.header_narrow == narrow, (width, host.header_narrow)
    assert host.header_more.get_visible() == narrow
    for key in Host.MORE_ITEMS:
        assert getattr(host, f"{key}_button").get_visible() == (not narrow), key
    # Nothing that shows is cut off: every visible child lies inside the space the header row was given.
    room = host.header_clip.get_width()
    child = header.get_first_child()
    while child is not None:
        if child.get_visible():
            ok, bounds = child.compute_bounds(header)
            assert ok and bounds.get_x() + bounds.get_width() <= room + 0.5 and bounds.get_x() >= -0.5, \
                (width, type(child).__name__, bounds.get_x(), bounds.get_width(), room)
        child = child.get_next_sibling()
    assert room <= width, (room, width)
    # The window does not grow for it: the header's least width fits, with room for the desktop's window
    # buttons beside it (a header bar), WINDOW_BUTTONS px.
    minimum = header.measure(Gtk.Orientation.HORIZONTAL, -1)[0]
    assert window.get_width() <= width + 0.5, ("the header widened the window", window.get_width(), width)
    assert minimum <= width - WINDOW_BUTTONS, ("header needs", minimum, "of", width)
    print(f"{width}: header needs {minimum} px", flush=True)
    if narrow:
        for key in Host.MORE_ITEMS:
            host.header_more_items[key].emit("clicked")
        assert host.opened == ["reports", "help", "settings", "refresh"], host.opened
    window.destroy()
    spin(0.2)


Adw.init()
os.environ.setdefault("AI_AGENTS_MAP_TEST", "1")
check(1300)
check(600)
print("HEADER OK")
