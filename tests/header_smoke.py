"""A real GTK build of the window's header (run by test_tab_links.HeaderNarrowTest under a headless
Broadway display with an isolated HOME), with the app's CSS and room kept free for the desktop's window
buttons as in a header bar: wide, every symbol in the row; narrower, Reports, Help, Settings and Refresh in
More, then no title, then the legend in More too. At every width, also when the counts, the status and the
legend appear later at an unchanged width, nothing that shows is cut off. Prints HEADER OK."""

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
import palette  # noqa: E402
from main_tabs import MainTabs  # noqa: E402

WINDOW_BUTTONS = 110            # what the desktop's minimize / maximize / close take beside the header
COPIED = ("build_header", "build_header_more", "header_clipper", "update_header_layout", "setup_header_order",
          "apply_header_order", "header_items", "header_position", "header_item_at", "header_child_shown",
          "header_need", "header_child_width", "header_title_width", "queue_header_layout", "run_header_layout", "update_header", "apply_theme_css",
          "apply_style_scheme", "adw_colors_css", "header_wanted", "header_child_changed", "set_header_wanted")
FULL = {"connected": True, "running": 3, "pausing": 1, "needs_you": 12, "attention_projects": 7,
        "error": "project_store_corrupt"}


class Host(MainTabs):
    HEADER_ITEMS = ai_popup.AgentsMap.HEADER_ITEMS
    HEADER_NARROW = ai_popup.AgentsMap.HEADER_NARROW
    HEADER_LEVELS = ai_popup.AgentsMap.HEADER_LEVELS
    MORE_ITEMS = ai_popup.AgentsMap.MORE_ITEMS

    def __init__(self, window):
        self.window = window
        self.project_summary = {"connected": True}
        self.opened = []
        self.view_buttons = {}
        self.updating_buttons = False
        self.theme = SimpleNamespace(colors=dict(palette.DEFAULT_COLORS))
        self.style_provider = None

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

    def open_legend(self):
        self.opened.append("legend")

    def to_map_first(self, then):
        then()

    def open_health(self):
        pass

    def open_ask_ai(self, *a, **k):
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


def settled(host, header):
    """Laid out again since the last change: no layout check is waiting, and every symbol that shows has
    been given its place (a symbol shown again keeps its old empty place until the next frame; Broadway's
    frames come when they come: wait for them, at most a few seconds)."""
    deadline = time.monotonic() + 4
    spin(0.3)
    while time.monotonic() < deadline:
        child = header.get_first_child()
        done = not getattr(host, "header_layout_queued", False) \
            and header.get_width() <= host.header_clip.get_width() + 0.5 \
            and header.measure(Gtk.Orientation.HORIZONTAL, -1)[0] <= header.get_width() + 0.5
        while child is not None and done:
            if child.get_visible() and isinstance(child, (Gtk.Button, Gtk.MenuButton)) and child.get_width() < 20:
                done = False
            child = child.get_next_sibling()
        if done:
            return
        spin(0.1)


def fits(host, header, label):
    """Every child that shows lies inside the room the header row was given."""
    settled(host, header)
    room = host.header_clip.get_width()
    child = header.get_first_child()
    if "--debug" in sys.argv:
        c = header.get_first_child()
        while c is not None:
            ok, b = c.compute_bounds(host.header_clip)
            print(label, type(c).__name__, c.get_visible(), c.get_tooltip_text(), ok and (round(b.get_x()), round(b.get_width())))
            c = c.get_next_sibling()
    while child is not None:
        if child.get_visible():
            ok, bounds = child.compute_bounds(host.header_clip)
            assert ok and bounds.get_x() >= -0.5 and bounds.get_x() + bounds.get_width() <= room + 0.5, \
                (label, type(child).__name__, round(bounds.get_x()), round(bounds.get_width()), room)
        child = child.get_next_sibling()


def stable(host, header, level, label):
    """Checked again and again at the same width (the 10 s summary, the status, a redraw): the same level,
    nothing cut off (Codex r8: hidden symbols measured as 0 made it flip back to the full row)."""
    for turn in range(3):
        host.update_header_layout()
        assert host.header_level == level, (label, "check", turn, host.header_level, "was", level)
        host.update_main_tabs()                 # as the summary poll does
        spin(0.2)
        assert host.header_level == level, (label, "turn", turn, host.header_level, "was", level)
        fits(host, header, f"{label}, turn {turn}")


def resize(width_from: int):
    """One window made narrow, wide and narrow again: each time the level fits its width again."""
    window = Gtk.Window(default_width=width_from, default_height=200)
    host = Host(window)
    try:
        host.apply_theme_css()
    except Exception:
        pass
    header = host.build_header()
    holder = Gtk.Box()
    holder.append(host.header_clipper(header))
    holder.append(Gtk.Box(width_request=WINDOW_BUTTONS))
    window.set_child(holder)
    window.present()
    host.project_summary = dict(FULL)
    host.update_main_tabs()
    host.set_header_wanted(host.problem_button, True)
    host.set_header_wanted(host.smartcheck_button, True)
    host.main_tab = "map"
    host.update_header()
    seen = []
    for width in (width_from, 1000, 600, 1000, 420, 900):   # Broadway's screen is 1024 px wide
        window.set_default_size(width, 200)
        deadline = time.monotonic() + 4
        while window.get_width() != width and time.monotonic() < deadline:
            spin(0.1)
        assert window.get_width() == width, ("not resized", width, window.get_width())
        host.update_main_tabs()                  # as width_changed does (the counts' compact form)
        host.update_header_layout()
        spin(0.6)
        fits(host, header, f"resized to {width}")
        stable(host, header, host.header_level, f"resized to {width}")
        seen.append((width, host.header_level))
    print("resize:", seen, flush=True)
    levels = dict(seen)
    assert levels[1000] == 0 and levels[600] >= 1 and levels[420] >= levels[600], seen
    window.destroy()
    spin(0.2)


def check(width: int):
    window = Gtk.Window(default_width=width, default_height=200)
    host = Host(window)
    try:
        host.apply_theme_css()             # the app's own sizes (title, tabs, symbols, paddings)
    except Exception as error:             # a missing theme part must not hide the layout check
        print("css:", error, flush=True)
    header = host.build_header()
    holder = Gtk.Box()
    holder.add_css_class("am-header-row")
    holder.append(host.header_clipper(header))
    holder.append(Gtk.Box(width_request=WINDOW_BUTTONS))       # the window buttons' place
    window.set_child(holder)
    window.present()
    spin(0.6)
    assert window.get_width() <= width + 0.5, ("the header widened the window", window.get_width(), width)
    fits(host, header, f"{width} at start")
    # Later, at the same width: the counts, the status symbol and Ask AI appear, the map shows the legend.
    host.project_summary = dict(FULL)
    host.update_main_tabs()
    host.set_header_wanted(host.problem_button, True)          # as update_problem_button does
    host.set_header_wanted(host.smartcheck_button, True)       # as update_smart_check_button does
    host.main_tab = "map"
    host.update_header()
    spin(0.6)
    assert window.get_width() <= width + 0.5, ("the content widened the window", window.get_width(), width)
    fits(host, header, f"{width} with counts and status")
    level = host.header_level
    stable(host, header, level, f"{width} with counts and status")
    if width < Host.HEADER_NARROW:
        assert level >= 1, (width, level)
    if level >= 1:
        assert host.header_more.get_visible()
        for key in Host.MORE_ITEMS:
            assert not getattr(host, f"{key}_button").get_visible(), key
            host.header_more_items[key].emit("clicked")
        assert host.opened[:4] == ["reports", "help", "settings", "refresh"], host.opened
    else:
        assert not host.header_more.get_visible()
    assert host.header_more_items["legend"].get_visible() == (level >= 3)
    assert host.header_more_items["ask"].get_visible() == (level >= 4)
    assert host.smartcheck_button.get_visible() == (level < 4)
    # The content goes again (no counts, no status): the row grows back where it fits, still inside.
    host.project_summary = {"connected": True}
    host.update_main_tabs()
    host.set_header_wanted(host.problem_button, False)
    host.set_header_wanted(host.smartcheck_button, False)
    spin(1.5)
    fits(host, header, f"{width} after the content went")
    stable(host, header, host.header_level, f"{width} after the content went")
    print(f"{width}: level {level} with content, {host.header_level} without", flush=True)
    window.destroy()
    spin(0.2)
    return level


Adw.init()
levels = {width: check(width) for width in (1400, 900, 850, 840, 830, 825, 800, 760, 600, 530, 525, 500, 420)}
assert levels[1400] == 0, levels
assert levels[420] >= levels[600] >= levels[800] >= levels[1400], levels
resize(600)
print("HEADER OK")
