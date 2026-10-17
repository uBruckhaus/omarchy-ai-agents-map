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
        self.assertEqual(self.view.flow_paths(self.all, self.all - {first}), [])   # not drawn (left out)
        second = next(i for i, e in enumerate(self.view.graph.edges) if e.source == "provider:anthropic")
        paths = self.view.flow_paths(self.all, self.all - {second})              # model hidden: agent → provider
        self.assertEqual(len(paths[0][1]), 1)

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
        self.tick, self.last = "ticking", 0

    def follow(self):
        pass

    def queue_draw(self):
        pass


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

    def run_for(self, seconds: float):
        for _ in range(int(seconds * 60)):          # 60 frames a second
            self.now += 1 / 60
            self.clock.us += 1_000_000 // 60
            self.layer.on_tick(None, self.clock)

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
