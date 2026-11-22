"""Settings › Map › 3D map layout (Maprisma task 069b): the setting, the 3D geometry, and that the map's
boxes, cards, cables and lights take the 3D look while the switch is on and stay flat while it is off.
Headless: Cairo image surfaces and plain stand-ins for the widget."""

import math
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")
import cairo  # noqa: E402

import flutter_map_editor  # noqa: E402
import graph  # noqa: E402
import map_3d  # noqa: E402
import map_view  # noqa: E402
import settings_schema  # noqa: E402

M = map_view.MapView
COLORS = {"bg": "#1e1e2e", "fg": "#cdd6f4", "dim": "#9399b2", "muted": "#888894", "accent": "#89b4fa",
          "red": "#f04452", "yellow": "#f9e2af", "green": "#a6e3a1", "cyan": "#94e2d5", "magenta": "#cba6f7",
          "hook": "#fab387", "collab": "#ff55aa", "outside": "#94e2d5"}


class SettingTest(unittest.TestCase):
    def test_on_unless_switched_off(self):
        self.assertTrue(map_3d.enabled(None))
        self.assertTrue(map_3d.enabled({}))
        self.assertTrue(map_3d.enabled({"three_d": True}))
        self.assertFalse(map_3d.enabled({"three_d": False}))

    def test_the_layout_validator_takes_a_boolean_only(self):
        self.assertTrue(flutter_map_editor.valid_layout({"three_d": False}))
        self.assertTrue(flutter_map_editor.valid_layout({"three_d": True, "dim_lines": False}))
        for bad in ("false", 0, 1, None, []):
            self.assertFalse(flutter_map_editor.valid_layout({"three_d": bad}), bad)
        stored = {"flutter_map_layout": {"three_d": False}}
        self.assertEqual(settings_schema.value(stored, "flutter_map_layout"), {"three_d": False})

    def test_the_settings_switch_keeps_the_rest_of_the_layout(self):
        import settings_pages
        saved, shown = [], []
        page = types.SimpleNamespace(save_setting=lambda **v: saved.append(v),
                                     side_map=types.SimpleNamespace(set_three_d=shown.append),
                                     sub_map=types.SimpleNamespace(set_three_d=shown.append))
        with mock.patch.object(settings_pages.ai_core, "setting",
                               lambda key: {"line_colors": {"mcp": "#ff0000"}} if key == "flutter_map_layout" else None):
            settings_pages.SettingsPages.save_three_d(page, False)
        self.assertEqual(saved, [{"flutter_map_layout": {"line_colors": {"mcp": "#ff0000"}, "three_d": False}}])
        self.assertEqual(shown, [False, False])          # the map and the Subagents map

    def test_the_map_starts_with_the_saved_choice(self):
        import ai_core
        import ai_popup
        for stored, expected in ((None, True), ({"line_colors": {}}, True), ({"three_d": False}, False)):
            app = types.SimpleNamespace(side_map=types.SimpleNamespace(order={}, cards={}, collapsed=set()),
                                        layout_pinned=lambda: False)
            with mock.patch.object(ai_core, "setting",
                                   lambda key, stored=stored: stored if key == "flutter_map_layout" else None):
                ai_popup.AgentsMap.load_map_layout(app)
            self.assertIs(app.side_map.three_d, expected)


class GeometryTest(unittest.TestCase):
    def test_columns_are_tiered_agents_proudest(self):
        agents, providers, models, tools = (map_3d.column_depth(c) for c in (1, 2, 3, 0))
        self.assertGreater(agents, providers)
        self.assertGreater(providers, models)
        self.assertEqual(models, tools)

    def test_lifted_and_faded_blocks(self):
        base = map_3d.column_depth(1)
        self.assertGreater(map_3d.block_depth(1, elevated=True), base)
        self.assertLess(map_3d.block_depth(1, dimmed=True), base)

    def test_square_walls(self):
        right, bottom, c0, c1 = map_3d.walls(100, 50, 0, 4, 5)
        self.assertEqual(right, ((100, 0), (104, 5), (104, 55), (100, 50)))
        self.assertEqual(bottom, ((100, 50), (104, 55), (4, 55), (0, 50)))
        self.assertEqual((c0, c1), ((100, 50), (104, 55)))

    def test_rounded_walls_stay_right_and_below_and_are_cached(self):
        right, bottom, c0, c1 = map_3d.walls(100, 50, 8, 5.2, 6.8)
        self.assertIs(map_3d.walls(100, 50, 8, 5.2, 6.8)[0], right)          # worked out once
        self.assertTrue(all(x >= 100 - 8 - 1e-9 for x, _y in right))
        self.assertTrue(all(y >= 50 - 8 - 1e-9 for _x, y in bottom))
        self.assertTrue(all(x <= 105.2 + 1e-9 and y <= 56.8 + 1e-9 for x, y in right + bottom))
        self.assertAlmostEqual(c1[0] - c0[0], 5.2)
        self.assertAlmostEqual(c1[1] - c0[1], 6.8)
        # The crease sits on the bottom-right corner's arc at 45°.
        self.assertAlmostEqual(math.hypot(c0[0] - 92, c0[1] - 42), 8)

    def test_recessed_blocks(self):
        self.assertTrue(map_3d.recessed(True, False, False))
        self.assertTrue(map_3d.recessed(False, True, False))      # a finished subagent, an unused server
        self.assertFalse(map_3d.recessed(False, True, True))      # … unless it is lit
        self.assertFalse(map_3d.recessed(False, False, False))

    def test_floor_grid(self):
        lines = map_3d.stage_lines(1200, 600)
        rows = [line for line in lines if line[1] == line[3]]
        self.assertEqual(len(rows), 14)
        ys = [line[1] for line in rows]
        self.assertEqual(ys, sorted(ys))
        self.assertGreater(ys[-1] - ys[-2], ys[1] - ys[0])        # wider apart towards the front
        self.assertTrue(all(0 <= s <= 1 for *_xy, s in lines))


def surface(width=400, height=200):
    image = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    return image, cairo.Context(image)


def alpha_at(image, x, y) -> int:
    image.flush()
    return image.get_data()[y * image.get_stride() + x * 4 + 3]


class Boxes:
    """MapView's box, card and summary drawing without the widget."""
    for name in ("draw_box", "draw_cards", "draw_summaries", "color", "palette_3d", "rounded", "rounded_top",
                 "draw_chevron", "outside_label", "runtime_use", "model_state", "flow_marks", "set_three_d"):
        locals()[name] = M.__dict__[name]
    del name
    FLOW_PERIOD, FLOW_PULSES = M.FLOW_PERIOD, M.FLOW_PULSES

    def __init__(self, three_d):
        self.three_d, self.colors, self.radius = three_d, dict(COLORS), 6
        self.selected, self.hover, self.outside, self.usage = [], None, {}, None
        self.menu_rects, self.details_rects, self.filter_rects, self.status_rects = {}, {}, {}, {}
        self.batteries, self.costs, self.updates, self.subagents, self.traffic = {}, {}, {}, {}, {}
        self.has_details = self.start_state = self.on_stop = None
        self.link, self.collapsed, self.used_only = None, set(), set()
        self.group_labels, self.summaries, self.column_status = [], {}, {}
        self.summary_counts, self.column_titles = None, {1: "Agents"}
        self.summary_texts = ("{0} on · {1} more available", "{0} found · Expand", "Nothing yet")
        self.flow_layer, self.redrawn = None, 0

    def queue_draw(self):
        self.redrawn += 1

    def shown(self, _column):
        return [graph.Node("agent:pi", 1, "Pi", "", "agent")]

    def draw_small_icon(self, *_args):
        pass

    def draw_pin(self, *_args):
        pass

    def called_agents(self):
        return set()

    def curve_point(self, edge, t):
        return (edge * 100 + 100 * t, 50.0)


class LookTest(unittest.TestCase):
    """The box at (40, 40) 200×66: in 3D its walls fill the gap right of and below it; flat, nothing."""

    def draw(self, three_d, lit=None, **node):
        view = Boxes(three_d)
        image, cr = surface()
        box = graph.Node("agent:pi", 1, "Pi", "", "agent", **node)
        layout = map_view.PangoCairo.create_layout(cr)
        view.draw_box(cr, layout, box, 40, 40, 200, 66, False, 1.0, lit)
        return image

    def test_a_dimmed_box_lies_nearly_flat_unless_lit(self):
        full, dim, lit = self.draw(True), self.draw(True, dim=True), self.draw(True, lit={"agent:pi"}, dim=True)
        self.assertLess(alpha_at(dim, 140, 111), alpha_at(full, 140, 111))   # walls 6.8 px deep → 3.7
        self.assertGreater(alpha_at(lit, 140, 111), alpha_at(dim, 140, 111))

    def test_a_box_is_a_block_in_3d_only(self):
        flat, raised = self.draw(False), self.draw(True)
        for x, y in ((243, 75), (140, 110)):              # right wall, bottom wall
            self.assertEqual(alpha_at(flat, x, y), 0)
            self.assertGreater(alpha_at(raised, x, y), 100)
        self.assertEqual(alpha_at(raised, 20, 20), 0)     # nothing above or left of it

    def test_a_running_box_lifts(self):
        still, running = self.draw(True), self.draw(True, status="running")
        self.assertGreater(alpha_at(running, 246, 80), alpha_at(still, 246, 80))

    def test_a_card_is_a_plinth_in_3d_only(self):
        for three_d in (False, True):
            view = Boxes(three_d)
            view.group_labels = [("agents", (20, 20, 300, 150), "Agents", "", 2, "agent")]
            image, cr = surface()
            view.draw_cards(cr, map_view.PangoCairo.create_layout(cr))
            self.assertEqual(alpha_at(image, 323, 100) > 100, three_d)
            self.assertEqual(alpha_at(image, 150, 175) > 100, three_d)

    def test_a_summary_card_is_a_block_in_3d_only(self):
        for three_d in (False, True):
            view = Boxes(three_d)
            view.summaries = {1: (20, 6, 300, 48)}
            image, cr = surface()
            view.draw_summaries(cr, map_view.PangoCairo.create_layout(cr))
            self.assertEqual(alpha_at(image, 323, 30) > 100, three_d)
            if three_d:                                   # its face reaches its walls: no seam
                self.assertEqual(alpha_at(image, 319, 30), 255)
                self.assertEqual(alpha_at(image, 20, 30), 255)

    def test_a_3d_card_keeps_its_plinth_face(self):
        """No flat fill over the plinth's face and tray: below its header the card is the plinth."""
        view = Boxes(True)
        view.group_labels = [("agents", (20, 20, 300, 150), "Agents", "", 2, "agent")]
        image, cr = surface()
        view.draw_cards(cr, map_view.PangoCairo.create_layout(cr))
        plinth, cr = surface()
        map_3d.paint_card(cr, 20, 20, 300, 150, view.radius + 2, view.palette_3d(), False)
        image.flush()
        plinth.flush()
        row = lambda img, y: bytes(img.get_data()[y * img.get_stride():(y + 1) * img.get_stride()])   # noqa: E731
        for y in (100, 140, 168):
            self.assertEqual(row(image, y), row(plinth, y))

    def test_switching_redraws_only_on_change(self):
        view = Boxes(True)
        view.flow_layer = types.SimpleNamespace(redrawn=0)
        view.flow_layer.queue_draw = lambda: setattr(view.flow_layer, "redrawn", view.flow_layer.redrawn + 1)
        view.stage = map_3d.Stage()
        view.stage.image = object()
        view.set_three_d(True)
        self.assertEqual(view.redrawn, 0)
        view.set_three_d(False)
        self.assertEqual((view.three_d, view.redrawn, view.flow_layer.redrawn), (False, 1, 1))
        self.assertIsNone(view.stage.image)              # the floor's image is let go while flat

    def test_the_palette_is_worked_out_once_per_palette(self):
        view = Boxes(True)
        first = view.palette_3d()
        self.assertIs(view.palette_3d(), first)
        self.assertTrue(first.dark)
        view.colors = {**COLORS, "bg": "#ffffff", "fg": "#000000"}
        self.assertFalse(view.palette_3d().dark)
        view.three_d = False
        self.assertIsNone(view.palette_3d())


class CableTest(unittest.TestCase):
    def test_a_cable_has_a_shadow_below_and_a_jacket(self):
        pal = map_3d.palette(Boxes(True).color)

        def trace(cr):
            cr.move_to(20, 50)
            cr.line_to(380, 50)
        image, cr = surface()
        map_3d.paint_cable(cr, trace, (0.5, 0.7, 1.0, 1.0), 2.0, pal)
        self.assertGreater(alpha_at(image, 200, 50), 200)        # the cable
        self.assertGreater(alpha_at(image, 200, 58), 0)          # its shadow on the floor
        self.assertEqual(alpha_at(image, 200, 40), 0)
        image, cr = surface()
        map_3d.paint_cable(cr, trace, (0.5, 0.7, 1.0, 1.0), 2.0, pal, shadow=False)
        self.assertEqual(alpha_at(image, 200, 58), 0)            # a faded cable lies flat

    def test_a_socket_where_a_cable_meets_a_box(self):
        image, cr = surface()
        map_3d.paint_socket(cr, 100, 100, (0.5, 0.7, 1.0, 1.0), map_3d.palette(Boxes(True).color))
        self.assertEqual(alpha_at(image, 100, 100), 255)
        self.assertEqual(alpha_at(image, 110, 100), 0)
        image, cr = surface()                            # as opaque as its cable
        map_3d.paint_socket(cr, 100, 100, (0.5, 0.7, 1.0, 0.55), map_3d.palette(Boxes(True).color))
        self.assertLess(alpha_at(image, 103, 100), 200)

    def test_a_faint_cable_casts_a_faint_shadow_and_dots_stay_apart(self):
        pal = map_3d.palette(Boxes(True).color)

        def trace(cr):
            cr.move_to(20, 50)
            cr.line_to(380, 50)
        strong, cr = surface()
        map_3d.paint_cable(cr, trace, (0.5, 0.7, 1.0, 1.0), 2.0, pal)
        faint, cr = surface()
        map_3d.paint_cable(cr, trace, (0.5, 0.7, 1.0, 0.25), 2.0, pal)
        self.assertLess(alpha_at(faint, 200, 58), alpha_at(strong, 200, 58))
        dots, cr = surface()
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        map_3d.paint_cable(cr, trace, (1.0, 0.3, 0.3, 1.0), 3.2, pal, dash=[0.1, 5])
        dots.flush()
        row = [alpha_at(dots, x, 50) for x in range(100, 140)]
        self.assertIn(0, row)                            # a gap between two dots

    def test_the_stage_is_kept_per_size(self):
        pal = map_3d.palette(Boxes(True).color)
        stage = map_3d.Stage()
        image, cr = surface()
        cr.scale(2, 2)
        stage.paint(cr, 200, 100, pal)
        kept = stage.image
        self.assertEqual((kept.get_width(), kept.get_height()), (400, 200))
        stage.paint(cr, 200, 100, pal)
        self.assertIs(stage.image, kept)
        stage.paint(cr, 150, 100, pal)
        self.assertIsNot(stage.image, kept)
        kept = stage.image
        stage.paint(cr, 150, 100, map_3d.palette(lambda name, a=1.0: {"bg": (1, 1, 1)}.get(name, (0, 0, 0)) + (a,)))
        self.assertIsNot(stage.image, kept)              # a new theme
        kept = stage.image
        cr.scale(1.5, 1.5)
        stage.paint(cr, 100, 66, pal)
        self.assertIsNot(stage.image, kept)              # another zoom or display scale
        kept = stage.image
        cr.scale(1, 2)                                   # stretched: drawn directly, not cached
        stage.paint(cr, 100, 33, pal)
        self.assertIs(stage.image, kept)


class LightsTest(unittest.TestCase):
    def test_lights_are_spheres_in_3d(self):
        flat = Boxes(False).flow_marks([("agent:pi", [0, 1])], now=1.0)
        raised = Boxes(True).flow_marks([("agent:pi", [0, 1])], now=1.0)
        self.assertNotIn("sphere", [m[0] for m in flat])
        kinds = [m[0] for m in raised]
        self.assertEqual(kinds.count("sphere"), kinds.count("dot"))
        for comet in map_view.comets(raised):                    # each comet still ends with its dot
            self.assertEqual([m[0] for m in comet][-2:], ["sphere", "dot"])
            map_view.comet_bounds(comet)
        image, cr = surface()
        map_view.draw_marks(cr, raised)
        self.assertGreater(max(image.get_data()[3::4]), 200)


class Headless(M):
    """The real MapView, built and drawn without a display (its GTK widget calls are no-ops)."""
    __gtype_name__ = "AamHeadlessMapView3d"

    def connect(self, *_a):
        pass

    def set_hexpand(self, *_a):
        pass

    set_vexpand = add_controller = set_content_height = set_content_width = set_hexpand

    def get_content_width(self):
        return 1300

    def get_width(self):
        return 1300

    def get_ancestor(self, *_a):
        return None

    def get_parent(self):
        return None

    def queue_draw(self):
        self.texture = None


def headless(three_d=True, edges=(), collapsed=(), moved=None):
    """A drawn map: MCP servers, skills, two agents, a provider; the given edges; folded and moved cards."""
    init = gi.repository.Gtk.DrawingArea.__init__
    gi.repository.Gtk.DrawingArea.__init__ = lambda self, *a, **k: None
    try:
        view = Headless.__new__(Headless)
        M.__init__(view)
    finally:
        gi.repository.Gtk.DrawingArea.__init__ = init
    g = graph.Graph()
    for node in (graph.Node("agent:claude", 1, "Claude", "", "agent"), graph.Node("agent:pi", 1, "Pi", "", "agent"),
                 graph.Node("mcp:alpha", 0, "alpha", "", "mcp"), graph.Node("mcp:beta", 0, "beta", "", "mcp"),
                 graph.Node("skills:~/s", 0, "~/s", "", "skills"),
                 graph.Node("cloud:anthropic", 2, "Anthropic", "", "cloud")):
        g.add(node)
    for source, target, kind in edges:
        g.link(source, target, kind)
    view.set_palette(COLORS, 6)
    view.set_graph(g, ["", "", "", ""])
    view.set_three_d(three_d)
    view.collapsed = {view.group_key(n) for n in g.nodes if n.id in collapsed}
    if moved:
        node_id, place = moved
        view.cards = {view.group_key(next(n for n in g.nodes if n.id == node_id)): list(place)}
    image = cairo.ImageSurface(cairo.FORMAT_ARGB32, 1300, 600)
    view.on_draw(view, cairo.Context(image), 1300, 600)
    image.flush()
    return view, image


def pixels(image, rect, inset=6):
    x, y, w, h = (round(v) for v in rect)
    data, stride = image.get_data(), image.get_stride()
    return [bytes(data[row * stride + (x + inset) * 4:row * stride + (x + w - inset) * 4])
            for row in range(y + inset, y + h - inset)]


class WholeMapTest(unittest.TestCase):
    """MapView.on_draw as a whole, headless: what lies over a cable stays clean, and 3D moves no click area."""

    EDGES = (("agent:claude", "mcp:alpha", "mcp"), ("agent:pi", "skills:~/s", "skills"),
             ("agent:claude", "cloud:anthropic", "uses"), ("agent:pi", "mcp:beta", "mcp"))

    def test_3d_keeps_every_click_area(self):
        for collapsed in ((), ("mcp:alpha",)):
            flat, _image = headless(False, self.EDGES, collapsed)
            raised, _image = headless(True, self.EDGES, collapsed)
            for name in ("boxes", "card_rects", "summaries", "menu_rects", "details_rects", "filter_rects",
                         "pin_rects", "group_labels"):
                self.assertEqual(getattr(flat, name), getattr(raised, name), name)

    def test_no_cable_inside_a_folded_header(self):
        view, plain = headless(True, (), ("mcp:alpha",))
        key = view.group_key(next(n for n in view.graph.nodes if n.id == "mcp:alpha"))
        _view, wired = headless(True, self.EDGES, ("mcp:alpha",))
        self.assertEqual(pixels(wired, view.card_rects[key], 1), pixels(plain, view.card_rects[key], 1))

    def test_no_cable_over_a_card_moved_onto_its_end(self):
        """The skills card moved over the MCP servers: their cables stay under it, its box stays clean."""
        moved = ("skills:~/s", (0, 230))
        view, plain = headless(True, (), moved=moved)
        _view, wired = headless(True, self.EDGES, moved=moved)
        self.assertEqual(pixels(wired, view.boxes["skills:~/s"]), pixels(plain, view.boxes["skills:~/s"]))
        key = view.group_key(next(n for n in view.graph.nodes if n.id == "skills:~/s"))
        x, y, w, h = view.card_rects[key]
        self.assertEqual(pixels(wired, (x, y, w, 26), 2), pixels(plain, (x, y, w, 26), 2))   # its header

    def test_no_cable_over_a_lower_box(self):
        """A cable to a moved card is painted late: it still never crosses a box under it."""
        moved = ("cloud:anthropic", (700, 330))
        view, plain = headless(True, (), moved=moved)
        _view, wired = headless(True, self.EDGES, moved=moved)
        for node_id in ("agent:claude", "agent:pi"):
            x, y, w, h = view.boxes[node_id]
            shown = (x, y, min(w, 700 - x), h)            # the part the moved card does not lie over
            self.assertEqual(pixels(wired, shown), pixels(plain, shown), node_id)

    def test_the_flat_map_is_drawn_as_before(self):
        _view, image = headless(False, self.EDGES)
        self.assertEqual(_view.sockets, [])


if __name__ == "__main__":
    unittest.main()
