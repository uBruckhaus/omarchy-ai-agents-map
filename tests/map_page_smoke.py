"""A real GTK build of the whole window (run by test_tab_links.MapPageNarrowTest under a headless Broadway
display with an isolated HOME, nothing scanned or started): the AI Map page at 420, 500, 600 and 800 px. Nothing
that shows on the page lies outside the window, the row above the map scrolls instead of widening the page,
the zoom, pin and default-layout controls stay inside, and the map scrolls sideways. Prints MAP OK."""

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import gi  # noqa: E402
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import GLib, Gtk  # noqa: E402

import ai_popup  # noqa: E402


def spin(seconds):
    context = GLib.MainContext.default()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        time.sleep(0.01)


def inside(widget, ancestor, room, label):
    ok, bounds = widget.compute_bounds(ancestor)
    assert ok, (label, "not placed")
    assert bounds.get_x() >= -0.5 and bounds.get_x() + bounds.get_width() <= room + 0.5, \
        (label, round(bounds.get_x()), round(bounds.get_width()), room)


def visible_children(box):
    child = box.get_first_child()
    while child is not None:
        if child.get_visible():
            yield child
        child = child.get_next_sibling()


def check(app, width):
    app.window.set_default_size(width, 800)
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and (app.window.get_width() != width
                                           or abs(app.card.get_width() + 34 - width) > 1):   # laid out anew
        spin(0.1)
    spin(0.8)
    assert app.window.get_width() == width, ("not resized", width, app.window.get_width())
    page = app.stack.get_child_by_name("main")
    room = app.window.get_width()
    # The page itself is not wider than the window (no part of it is cut off by the window's edge).
    assert app.card.get_width() <= room + 0.5, ("the page is wider than the window", app.card.get_width(), room)
    for child in visible_children(page):
        inside(child, app.window, room, f"{width}: {type(child).__name__} on the page")
    # The row above the map: the chips scroll, the zoom, pin and default layout stay in view.
    bar = app.map_bar_scroller.get_parent()
    inside(bar, app.window, room, f"{width}: the row above the map")
    for widget in (app.zoom_label, app.layout_pin):
        inside(widget, app.window, room, f"{width}: {widget.get_tooltip_text() or widget.get_label()}")
    chips = app.map_bar_scroller.get_hadjustment()
    if chips.get_upper() > chips.get_page_size() + 0.5:
        chips.set_value(chips.get_upper() - chips.get_page_size())   # reachable by scrolling
        spin(0.2)
    # The first connection's card fits, its steps one under the other in a narrow window.
    first = getattr(app, "first_steps_row", None)
    if first is not None and first.get_mapped():
        inside(first, app.window, room, f"{width}: the first connection's steps")
        assert (first.get_orientation() == Gtk.Orientation.VERTICAL) == (width < app.MAP_NARROW), width
    # The map: inside the page, and it scrolls sideways when its columns need more room.
    inside(app.map_scroller, app.window, room, f"{width}: the map's view")
    print(f"{width}: page {app.card.get_width()} px, map {app.side_map.get_width()} px in "
          f"{app.map_scroller.get_width()} px", flush=True)


# A desktop that sizes the window itself (as on Hyprland, or a user's resize): the window keeps the width given.
ai_popup.desktop.header_moves_window = lambda: True
app = ai_popup.AgentsMap()
app.register(None)
app.make_window()
app.build_window()
app.collect = lambda: None              # nothing of this PC is looked at
app.window.present()
spin(1.0)
for width in (800, 600, 500, 420, 1000):   # Broadway's screen is 1024 px wide
    check(app, width)
print("MAP OK")
