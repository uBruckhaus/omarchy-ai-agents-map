import math
import sys
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import graph  # noqa: E402
import map_view  # noqa: E402


def sample() -> graph.Graph:
    g = graph.Graph()
    g.add(graph.Node("agent:claude", 1, "Claude Code", kind="agent", status="running"))
    g.add(graph.Node("agent:pi", 1, "Pi", kind="agent"))                       # installed, not running
    g.add(graph.Node("provider:anthropic", 2, "Anthropic", kind="provider"))
    g.add(graph.Node("runtime:llamacpp", 2, "llama.cpp", kind="runtime", status="running"))
    g.add(graph.Node("model:opus", 3, "opus", kind="model"))
    g.add(graph.Node("model:runtime:llamacpp:qwen", 3, "qwen", kind="model"))
    g.link("agent:claude", "provider:anthropic", "uses")
    g.link("provider:anthropic", "model:opus", "serves")
    g.link("agent:claude", "model:opus", "default")
    g.link("agent:pi", "runtime:llamacpp", "uses")
    g.link("runtime:llamacpp", "model:runtime:llamacpp:qwen", "serves")
    g.link("agent:pi", "model:runtime:llamacpp:qwen", "default")
    return g


class FlowOnly:
    """MapView's flow logic without the widget."""
    flow_paths = map_view.MapView.flow_paths
    running_agents = map_view.MapView.running_agents


class FlowTest(unittest.TestCase):
    def setUp(self):
        self.view = FlowOnly()
        self.view.graph = sample()
        self.view.active = map_view.MapView.active_paths(self.view.graph)
        self.view.working = None
        self.all = set(range(len(self.view.graph.edges)))

    def test_only_running_agents_flow_agent_provider_model(self):
        paths = self.view.flow_paths(self.all, self.all)
        self.assertEqual([agent for agent, _e in paths], ["agent:claude"])   # Pi is not running
        self.assertEqual([(e.source, e.target) for e in paths[0][1]],
                         [("agent:claude", "provider:anthropic"), ("provider:anthropic", "model:opus")])

    def test_nothing_flows_on_lines_not_drawn_bold(self):
        first = next(i for i, e in enumerate(self.view.graph.edges) if e.source == "agent:claude")
        self.assertEqual(self.view.flow_paths(self.all - {first}, self.all), [])   # not bold (another agent hovered)
        second = next(i for i, e in enumerate(self.view.graph.edges) if e.source == "provider:anthropic")
        paths = self.view.flow_paths(self.all, self.all - {second})              # model hidden: agent → provider
        self.assertEqual(len(paths[0][1]), 1)
        self.assertEqual(self.view.flow_paths(self.all, self.all - {first, second}), [])   # nothing drawn

    def test_agents_hidden_light_runs_provider_to_model(self):
        first = next(i for i, e in enumerate(self.view.graph.edges) if e.source == "agent:claude")
        paths = self.view.flow_paths(self.all, self.all - {first})               # Show › Agents off
        self.assertEqual([(e.source, e.target) for e in paths[0][1]], [("provider:anthropic", "model:opus")])

    def test_only_working_agents_flow_once_known(self):
        next(n for n in self.view.graph.nodes if n.id == "agent:pi").status = "running"
        self.view.working = {"pi"}                                  # Claude runs, but waits at its prompt
        self.assertEqual([a for a, _e in self.view.flow_paths(self.all, self.all)], ["agent:pi"])
        self.view.working = set()
        self.assertEqual(self.view.flow_paths(self.all, self.all), [])

    def test_pi_flows_once_running(self):
        next(n for n in self.view.graph.nodes if n.id == "agent:pi").status = "running"
        paths = dict(self.view.flow_paths(self.all, self.all))
        self.assertEqual([e.target for e in paths["agent:pi"]], ["runtime:llamacpp", "model:runtime:llamacpp:qwen"])


class FadeOnly:
    """FlowLayer's fade and grace logic without the widget."""
    update = map_view.FlowLayer.update
    target = map_view.FlowLayer.target
    on_tick = map_view.FlowLayer.on_tick

    def __init__(self):
        self.paths, self.visible, self.running, self.stopped, self.levels = {}, set(), set(), {}, {}
        self.tick, self.last, self.drawn_at = "ticking", 0, 0
        self.draws = 0
        self.mapped = True

    def get_mapped(self):
        return self.mapped

    FLOW_FPS = map_view.FlowLayer.FLOW_FPS

    def follow(self):
        pass

    def queue_draw(self):
        self.draws += 1


class Clock:
    def __init__(self):
        self.us = 1_000_000

    def get_frame_time(self):
        return self.us


class FadeTest(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        patcher = mock.patch.object(map_view.GLib, "get_monotonic_time", side_effect=lambda: int(self.now * 1e6))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.layer, self.clock = FadeOnly(), Clock()

    def run_for(self, seconds: float, hz: int = 60):
        for _ in range(int(seconds * hz)):          # the display's frames
            self.now += 1 / hz
            self.clock.us += 1_000_000 // hz
            self.layer.on_tick(None, self.clock)

    def test_a_fast_display_draws_at_most_flow_fps(self):
        self.layer.update([("agent:claude", ["e1"])], {"agent:claude"})
        self.run_for(1.0, hz=144)
        self.assertLessEqual(self.layer.draws, map_view.FlowLayer.FLOW_FPS + 1)
        self.assertGreaterEqual(self.layer.draws, map_view.FlowLayer.FLOW_FPS - 2)

    def test_a_hidden_window_stops_ticking(self):
        self.layer.update([("agent:claude", ["e1"])], {"agent:claude"})
        self.run_for(0.2)
        self.layer.mapped = False
        self.assertEqual(self.layer.on_tick(None, self.clock), map_view.GLib.SOURCE_REMOVE)
        self.assertIsNone(self.layer.tick)

    def test_a_60_hz_display_draws_every_other_frame(self):
        self.layer.update([("agent:claude", ["e1"])], {"agent:claude"})
        self.run_for(1.0)
        self.assertLessEqual(abs(self.layer.draws - map_view.FlowLayer.FLOW_FPS), 1)   # a steady beat

    def test_fades_in_and_out_never_switches(self):
        self.layer.update([("agent:claude", ["e1"])], {"agent:claude"})
        self.run_for(0.1)
        level = self.layer.levels["agent:claude"]
        self.assertTrue(0 < level < 1)                # on its way up, not on at once
        self.run_for(0.5)
        self.assertEqual(self.layer.levels["agent:claude"], 1.0)

    def test_a_missed_check_does_not_blink(self):
        self.layer.update([("agent:claude", ["e1"])], {"agent:claude"})
        self.run_for(0.5)
        self.layer.update([("agent:claude", ["e1"])], set())      # one check missed it
        self.run_for(1.0)
        self.assertEqual(self.layer.levels["agent:claude"], 1.0)  # still fully lit
        self.layer.update([("agent:claude", ["e1"])], {"agent:claude"})
        self.run_for(3.0)
        self.assertEqual(self.layer.levels["agent:claude"], 1.0)

    def test_stopped_agent_fades_out_after_the_grace(self):
        self.layer.update([("agent:claude", ["e1"])], {"agent:claude"})
        self.run_for(0.5)
        self.layer.update([("agent:claude", ["e1"])], set())
        self.run_for(map_view.MapView.FLOW_GRACE + map_view.MapView.FLOW_FADE + 0.2)
        self.assertNotIn("agent:claude", self.layer.levels)
        self.assertEqual(self.layer.paths, {})

    def test_hidden_path_fades_out_without_grace(self):
        self.layer.update([("agent:claude", ["e1"])], {"agent:claude"})
        self.run_for(0.5)
        self.layer.update([], {"agent:claude"})                   # another agent hovered: not bold
        self.run_for(0.1)
        self.assertTrue(0 < self.layer.levels["agent:claude"] < 1)
        self.run_for(0.4)
        self.assertNotIn("agent:claude", self.layer.levels)


if __name__ == "__main__":
    unittest.main()


class UsageBoxTest(unittest.TestCase):
    """The usage statistics box keeps its readings apart from the GPU usage the map also gets."""

    class View:
        set_usage = map_view.MapView.set_usage
        set_usage_box = map_view.MapView.set_usage_box
        usage_glyphs = map_view.MapView.usage_glyphs
        usage_font = map_view.MapView.usage_font
        line_color = map_view.MapView.line_color
        single_agent = lambda self: None
        UB_GAP, UB_SPACE = map_view.MapView.UB_GAP, map_view.MapView.UB_SPACE
        flow_layer = None
        usage_items = map_view.usage_box.ITEMS
        usage_size = 7

        def __init__(self):
            self.usage, self.ub = None, {}
            self.drawn = []
            self.edge = graph.Edge("agent:claude", "provider:x", "uses")
            self.graph = graph.Graph(edges=[self.edge])
            self.active, self.tints = {}, {0: "violet"}

        def queue_draw(self):
            pass

        def color(self, name, alpha=1.0):
            self.drawn.append(name)
            return (1, 1, 1, alpha)

        def curve_point(self, _edge, t):
            return (20 + 300 * t, 60 + 30 * t * t)     # a gently bending line

    def layout(self):
        import cairo
        cr = cairo.Context(cairo.ImageSurface(cairo.FORMAT_ARGB32, 4, 4))
        return map_view.PangoCairo.create_layout(cr)

    def test_gpu_usage_does_not_replace_the_readings(self):
        view = self.View()
        reading = map_view.usage_box.Reading(60.0, 950, 1000, "m")
        view.set_usage_box({"agent:claude": reading})
        view.set_usage(object())                     # the GPU poll's gpu_usage.Usage
        glyphs = view.usage_glyphs("agent:claude", [view.edge], 1.0, self.layout())
        self.assertEqual(len(glyphs), len(view.drawn))
        # Each character in the line's colour (its tint here), the 95 % in red.
        self.assertEqual(set(view.drawn[:-3]), {"violet"})
        self.assertEqual(view.drawn[-3:], ["red"] * 3)

    def test_characters_follow_the_line_left_to_right(self):
        view = self.View()
        view.set_usage_box({"agent:claude": map_view.usage_box.Reading(60.0, 500, 1000, "m")})
        glyphs = view.usage_glyphs("agent:claude", [view.edge], 1.0, self.layout())
        xs = [g[1] for g in glyphs]
        self.assertEqual(xs, sorted(xs))
        self.assertTrue(all(20 <= x <= 320 for x in xs))         # on the line, centred on it
        self.assertEqual(view.usage_glyphs("agent:claude", [view.edge], 0.0, self.layout()), [])


class FlowMarksTest(unittest.TestCase):
    """The comets as marks: a tapering tail, then a glow and a dot at the head."""

    class View:
        flow_marks = map_view.MapView.flow_marks
        FLOW_PERIOD, FLOW_PULSES = map_view.MapView.FLOW_PERIOD, map_view.MapView.FLOW_PULSES

        def color(self, name, alpha=1.0):
            return (0.5, 0.5, 0.5, alpha)

        def curve_point(self, edge, t):
            return (edge * 100 + 100 * t, 50.0)          # edge i runs from x=100i to x=100(i+1)

    def test_each_pulse_ends_in_a_glow_and_a_dot(self):
        marks = self.View().flow_marks([("agent:claude", [0, 1])], now=1.0)
        kinds = [m[0] for m in marks]
        self.assertEqual(kinds.count("glow"), kinds.count("dot"))
        self.assertTrue(1 <= kinds.count("dot") <= map_view.MapView.FLOW_PULSES)
        lines = [m for m in marks if m[0] == "line"]
        self.assertTrue(lines)
        self.assertTrue(all(1.0 <= m[5] <= 4.5 for m in lines))   # widths taper 4.5 → 1

    def test_dark_agents_make_no_marks(self):
        view = self.View()
        self.assertEqual(view.flow_marks([("agent:claude", [0, 1])], 1.0, {"agent:claude": 0.0}), [])
        self.assertEqual(view.flow_marks([("agent:claude", [0, 1])], 1.0, {}), [])


class SmallImagesTest(unittest.TestCase):
    """Each comet gets an image just large enough for it, not one as large as the map."""

    def test_marks_split_into_comets_at_each_dot(self):
        marks = [("line", 0, 0, 5, 0, 2.0, (1, 1, 1, 1)), ("glow", 5, 0, 18, (1, 1, 1), (1, 1, 1), 1.0),
                 ("dot", 5, 0, 3.2, (1, 1, 1, 1)), ("line", 100, 0, 105, 0, 2.0, (1, 1, 1, 1)),
                 ("glow", 105, 0, 18, (1, 1, 1), (1, 1, 1), 1.0), ("dot", 105, 0, 3.2, (1, 1, 1, 1))]
        comets = list(map_view.comets(marks))
        self.assertEqual([len(c) for c in comets], [3, 3])

    def test_bounds_hold_the_glow_and_the_line_width(self):
        comet = [("line", 0, 0, 40, 0, 4.0, (1, 1, 1, 1)), ("glow", 40, 0, 18, (1, 1, 1), (1, 1, 1), 1.0),
                 ("dot", 40, 0, 3.2, (1, 1, 1, 1))]
        self.assertEqual(map_view.comet_bounds(comet), (-3.0, -19.0, 59.0, 19.0))


class UsageTextureTest(unittest.TestCase):
    """The usage box is drawn once into a texture and kept while it shows the same."""

    class Layer:
        usage_texture = map_view.FlowLayer.usage_texture

        def __init__(self, view):
            self.view, self.box_textures, self.levels = view, {}, {"agent:claude": 1.0}

        def get_scale_factor(self):
            return 2

        def create_pango_layout(self, _text):
            import cairo
            return map_view.PangoCairo.create_layout(cairo.Context(cairo.ImageSurface(cairo.FORMAT_ARGB32, 4, 4)))

    def setUp(self):
        self.view = UsageBoxTest.View()
        self.view.colors = {}
        self.view.zoom = 1.0
        self.view.edge_points = lambda edge: (20, 60, 320, 90)
        self.view.set_usage_box({"agent:claude": map_view.usage_box.Reading(60.0, 500, 1000, "m")})
        self.layer = self.Layer(self.view)
        self.calls = 0
        glyphs = self.view.usage_glyphs

        def counted(*args):
            self.calls += 1
            return glyphs(*args)
        self.view.usage_glyphs = counted

    def test_kept_while_the_same_and_drawn_again_on_change(self):
        first = self.layer.usage_texture("agent:claude", [self.view.edge])
        texture, bounds = first
        self.assertEqual(texture.get_width(), math.ceil(bounds.get_width() * 2))   # sharp on a scale-2 display
        self.assertIs(self.layer.usage_texture("agent:claude", [self.view.edge]), first)
        self.assertEqual(self.calls, 1)
        self.view.set_usage_box({"agent:claude": map_view.usage_box.Reading(80.0, 600, 1000, "m")})
        self.assertIsNot(self.layer.usage_texture("agent:claude", [self.view.edge]), first)
        self.assertEqual(self.calls, 2)

    def test_no_reading_no_texture(self):
        self.view.set_usage_box({})
        self.assertIsNone(self.layer.usage_texture("agent:claude", [self.view.edge]))
        self.assertEqual(self.layer.box_textures, {})


class FrameCostTest(unittest.TestCase):
    """What keeps a frame of the lights cheap (tools/profile-map.py measures it): the map is drawn
    once into a texture, and each light into a small image, never one as large as the map."""

    def test_map_drawn_once_then_its_texture_reused(self):
        class View:
            do_snapshot = map_view.MapView.do_snapshot
            texture, size, drawn = None, (800, 600), 0

            def get_width(self):
                return self.size[0]

            def get_height(self):
                return self.size[1]

            def get_scale_factor(self):
                return 2

            def on_draw(self, _area, cr, width, height):
                self.drawn += 1
                cr.rectangle(10, 10, 50, 50)
                cr.fill()

        view = View()
        for _ in range(3):
            view.do_snapshot(map_view.Gtk.Snapshot())
        self.assertEqual(view.drawn, 1)
        self.assertEqual(view.texture[1].get_width(), 1600)          # sharp at scale 2
        view.texture = None                                            # what queue_draw does
        view.do_snapshot(map_view.Gtk.Snapshot())
        view.size = (900, 600)
        view.do_snapshot(map_view.Gtk.Snapshot())
        self.assertEqual(view.drawn, 3)

    def test_no_image_of_a_frame_is_near_the_size_of_the_map(self):
        map_view.gi.require_version("Gsk", "4.0")
        from gi.repository import Gsk
        view = UsageBoxTest.View()
        view.colors, view.zoom = {}, 1.0
        view.edge_points = lambda edge: (20, 60, 320, 90)
        view.FLOW_PERIOD, view.FLOW_PULSES = map_view.MapView.FLOW_PERIOD, map_view.MapView.FLOW_PULSES
        view.flow_marks = lambda paths, now, levels: map_view.MapView.flow_marks(view, paths, now, levels)
        view.set_usage_box({"agent:claude": map_view.usage_box.Reading(60.0, 500, 1000, "m")})
        layer = UsageTextureTest.Layer(view)
        layer.paths = {"agent:claude": [view.edge]}
        snapshot = map_view.Gtk.Snapshot()
        map_view.FlowLayer.do_snapshot(layer, snapshot)
        sizes = []

        def walk(node):
            if node is None:
                return
            if isinstance(node, Gsk.ContainerNode):
                for i in range(node.get_n_children()):
                    walk(node.get_child(i))
            elif hasattr(node, "get_child"):
                walk(node.get_child())
            else:
                bounds = node.get_bounds()
                sizes.append(bounds.get_width() * bounds.get_height())

        walk(snapshot.to_node())
        self.assertGreaterEqual(len(sizes), 2)                       # comets and the usage box
        self.assertLess(max(sizes), 400 * 200)                       # the map here is 1366 x 1452


class HelperLightsTest(unittest.TestCase):
    """Lights on an arrow from an agent to one it uses, and on the lines to working subagents only."""

    def test_arrows_and_working_subagents_get_lights(self):
        g = graph.Graph()
        g.add(graph.Node("agent:claude", 1, "Claude", kind="agent", status="running"))
        g.add(graph.Node("agent:pi", 1, "Pi", kind="agent", status="running"))
        g.add(graph.Node("sub:a", 2, "Working", kind="subagent", status="running"))
        g.add(graph.Node("sub:b", 2, "Done", kind="subagent", dim=True))
        g.link("agent:claude", "sub:a", "subagent")
        g.link("agent:claude", "sub:b", "subagent")

        class View:
            helper_paths = map_view.MapView.helper_paths
            drawn_agent_links = map_view.MapView.drawn_agent_links
            agent_link_curve = map_view.MapView.agent_link_curve
            bezier = staticmethod(map_view.MapView.bezier)
            curve_point = map_view.MapView.curve_point

        view = View()
        view.graph, view.agent_links = g, {("agent:claude", "agent:pi")}
        view.boxes = {"agent:claude": (100, 0, 200, 40), "agent:pi": (100, 50, 200, 40),
                      "sub:a": (500, 0, 200, 40), "sub:b": (500, 50, 200, 40)}
        paths = dict(view.helper_paths())
        self.assertEqual(sorted(paths), ["link:agent:claude>agent:pi", "sub:sub:a"])
        arc = paths["link:agent:claude>agent:pi"][0]
        start, end = view.curve_point(arc, 0), view.curve_point(arc, 1)
        self.assertEqual((round(start[0]), round(end[0])), (100, 93))     # from the user to the arrowhead
        self.assertLess(view.curve_point(arc, 0.5)[0], 100)                # bowing out left of the card
