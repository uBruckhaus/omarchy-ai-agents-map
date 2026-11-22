"""A real GTK build of the whole window (run by test_tab_links.MapPageNarrowTest under a headless Broadway
display with an isolated HOME, nothing scanned or started): the AI Map page at 800, 600, 500 and 420 px and
wide again. The page is never wider than the window; the row above the map overflows into its own sideways
scroll in a narrow window (not at 1000 px), the rightmost chip comes into view by scrolling and by the
keyboard, what is selected stays readable (never "…"), the zoom, pin and Default layout stay inside; the
first connection's card is shown and its steps stack below 700 px; its animation (Watch how) does not widen
the page. Prints MAP OK."""

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

SELECTION = "Selected: Claude Code, Codex, Pi"


def spin(seconds):
    context = GLib.MainContext.default()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        time.sleep(0.01)


def inside(widget, ancestor, room, label):
    ok, bounds = widget.compute_bounds(ancestor)
    assert ok and widget.get_mapped(), (label, "not shown")
    assert bounds.get_x() >= -0.5 and bounds.get_x() + bounds.get_width() <= room + 0.5, \
        (label, round(bounds.get_x()), round(bounds.get_width()), room)


def visible_children(box):
    child = box.get_first_child()
    while child is not None:
        if child.get_visible():
            yield child
        child = child.get_next_sibling()


def page_fits(app, width, label):
    """The page is not wider than the window: nothing of it is cut off by the window's edge."""
    room = app.window.get_width()
    assert app.card.measure(Gtk.Orientation.HORIZONTAL, -1)[0] <= room, \
        (label, "the page needs", app.card.measure(Gtk.Orientation.HORIZONTAL, -1)[0], "of", room)
    for child in visible_children(app.stack.get_child_by_name("main")):
        inside(child, app.window, room, f"{label}: {type(child).__name__} on the page")


def resize(app, width):
    app.window.set_default_size(width, 800)
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and (app.window.get_width() != width or app.card.get_width() > width):
        spin(0.1)
    spin(0.8)
    assert app.window.get_width() == width, ("not resized", width, app.window.get_width())


def chip_row(app):
    return app.map_bar_scroller.get_hadjustment()


def select(app, on):
    """An active selection (its label and Clear selection), as update_selection_labels shows it, or none."""
    app.side_filter.set_label(SELECTION if on else "")
    app.map_show_all.set_visible(on)
    deadline = time.monotonic() + 5
    spin(0.3)
    natural = app.side_filter.measure(Gtk.Orientation.HORIZONTAL, -1)[1]
    while on and time.monotonic() < deadline and app.side_filter.get_width() < natural - 0.5:
        spin(0.1)                            # laid out anew (Broadway's frames come when they come)
    spin(0.3)


def check(app, width, selected):
    resize(app, width)
    select(app, selected)
    room = app.window.get_width()
    page_fits(app, width, f"{width}")
    # The zoom, the pin and Default layout (the rightmost control) stay in view.
    for widget, name in ((app.zoom_label, "zoom"), (app.layout_pin, "pin"), (app.map_layout_reset, "Default layout")):
        inside(widget, app.window, room, f"{width}: {name}")
    inside(app.map_scroller, app.window, room, f"{width}: the map's view")
    # The row above the map: scrolls in a narrow window, fits without scrolling in a wide one.
    row = chip_row(app)
    overflows = row.get_upper() > row.get_page_size() + 0.5
    if not selected and width >= 1000:
        assert not overflows, (width, "a wide window needs no scrolling", row.get_upper(), row.get_page_size())
    if width <= 500:
        assert overflows, (width, "the row should scroll here", row.get_upper(), row.get_page_size())
    # What is selected reads in full (scrolls, never "…").
    assert not app.side_filter.get_layout().is_ellipsized(), (width, "selection ellipsized",
                                                              app.side_filter.get_width())
    # The rightmost chip (Models): by scrolling, and by the keyboard.
    models = list(app.layer_buttons.values())[-1] if isinstance(getattr(app, "layer_buttons", None), dict) \
        else None
    if overflows:
        row.set_value(0)
        spin(0.2)
        # The keyboard: Tab brings a chip into view (the viewport scrolls to the focused child; Broadway's
        # focus handling is not reliable enough to press Tab here, the property that does it is checked).
        assert app.map_bar_scroller.get_child().get_scroll_to_focus(), "the chips must scroll to the keyboard"
        row.set_value(row.get_upper() - row.get_page_size())
        deadline = time.monotonic() + 4
        while models is not None and time.monotonic() < deadline:
            spin(0.1)                            # laid out at the new scroll position
            ok, bounds = models.compute_bounds(app.map_bar_scroller)
            if ok and bounds.get_x() + bounds.get_width() <= app.map_bar_scroller.get_width() + 0.5:
                break
        if models is not None:
            inside(models, app.map_bar_scroller, app.map_bar_scroller.get_width(), f"{width}: Models scrolled")
    # The first connection's card: shown, its steps stacked below 700 px, inside the window.
    first = app.first_steps_row
    inside(first, app.window, room, f"{width}: the first connection's steps")
    assert (first.get_orientation() == Gtk.Orientation.VERTICAL) == (width < app.MAP_NARROW), width
    print(f"{width}{' selected' if selected else ''}: page {app.card.get_width()} px, row {round(row.get_upper())} in {round(row.get_page_size())} px",
          flush=True)


# A desktop that sizes the window itself (as on Hyprland, or a user's resize): the window keeps the width given.
ai_popup.desktop.header_moves_window = lambda: True
app = ai_popup.AgentsMap()
app.register(None)
app.make_window()
app.build_window()
app.collect = lambda: None              # nothing of this PC is looked at
app.window.present()
spin(1.0)
app.first_card.set_visible(True)         # the first connection's card shown
spin(0.5)
for width in (800, 600, 500, 420, 1000):    # Broadway's screen is 1024 px wide
    for selected in (False, True):
        check(app, width, selected)
# Watch how at 420 px: the 560 px animation scrolls sideways inside the card, the page stays the window's width.
resize(app, 420)


def buttons(widget):
    found, stack = [], [widget]
    while stack:
        item = stack.pop()
        if isinstance(item, Gtk.Button):
            found.append(item)
        child = item.get_first_child()
        while child is not None:
            stack.append(child)
            child = child.get_next_sibling()
    return found


def texts(widget):
    found, stack = [], [widget]
    while stack:
        item = stack.pop()
        if isinstance(item, Gtk.Label):
            found.append(item.get_label())
        child = item.get_first_child()
        while child is not None:
            stack.append(child)
            child = child.get_next_sibling()
    return found


watch = next(b for b in buttons(app.first_card) if "Watch how" in texts(b))
watch.emit("clicked")
spin(1.5)
page_fits(app, 420, "420 with Watch how")
inside(app.first_steps_row, app.window, 420, "420 with Watch how: the steps")
app.first_sketch.stop() if hasattr(app.first_sketch, "stop") else None
print("MAP OK")
