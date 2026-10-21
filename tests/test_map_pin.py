import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import map_view  # noqa: E402
from test_map_pick import sample  # noqa: E402


class PinOnly:
    """MapView's highlighting without the widget."""
    focus = map_view.MapView.focus
    lit = map_view.MapView.lit
    related = map_view.MapView.related
    highlighted = map_view.MapView.highlighted
    active_now = map_view.MapView.active_now
    single_agent = map_view.MapView.single_agent


class PinTest(unittest.TestCase):
    def setUp(self):
        self.view = PinOnly()
        self.view.graph = sample()
        self.view.selected, self.view.pinned, self.view.hover = [], [], None

    def test_hover_lights_without_pins(self):
        self.view.hover = "runtime:ollama"
        nodes, _edges = self.view.highlighted()
        self.assertEqual(nodes, {"runtime:ollama", "model:runtime:ollama:gemma3"})

    def test_pinned_box_stays_lit_while_hovering_another(self):
        self.view.pinned = ["agent:hermes"]
        self.view.hover = "runtime:ollama"
        nodes, _edges = self.view.highlighted()
        self.assertIn("agent:hermes", nodes)
        self.assertIn("runtime:llamacpp", nodes)
        self.assertNotIn("runtime:ollama", nodes)

    def test_pinned_agent_does_not_hide_other_agents_paths(self):
        # Ollama selected, Pi (no model of its own) pinned: Hermes's line to its model stays blue.
        g = self.view.graph
        g.link("agent:hermes", "runtime:ollama", "uses")
        g.link("agent:pi", "runtime:ollama", "uses")
        g.link("agent:hermes", "model:runtime:ollama:gemma3", "default")
        self.view.active = map_view.MapView.active_paths(g)
        self.view.selected, self.view.pinned = ["runtime:ollama"], ["agent:pi"]
        edges, models = self.view.active_now(None)
        index = {(e.source, e.target): i for i, e in enumerate(g.edges)}
        self.assertIn(index[("runtime:ollama", "model:runtime:ollama:gemma3")], edges)
        self.assertEqual(models, {"model:runtime:ollama:gemma3"})

    def test_agent_lights_only_its_own_model(self):
        # Ollama serves two models; Hermes is set to gemma3: only that line is lit.
        g = self.view.graph
        g.add(map_view.graph_model.Node("model:runtime:ollama:qwen3", 3, "qwen3", kind="model"))
        g.link("runtime:ollama", "model:runtime:ollama:qwen3", "serves")
        g.link("agent:hermes", "runtime:ollama", "uses")
        g.link("agent:hermes", "model:runtime:ollama:gemma3", "default")
        self.view.selected = ["agent:hermes"]
        nodes, _edges = self.view.highlighted()
        self.assertIn("model:runtime:ollama:gemma3", nodes)
        self.assertNotIn("model:runtime:ollama:qwen3", nodes)
        # A provider it has no model on still shows all of its models.
        self.assertIn("model:runtime:llamacpp:qwen3-coder", nodes)

    def test_single_agent(self):
        self.view.selected = ["agent:hermes", "runtime:ollama"]
        self.assertEqual(self.view.single_agent(), "agent:hermes")
        self.view.selected = ["agent:hermes", "agent:pi"]
        self.assertIsNone(self.view.single_agent())
        self.view.hover = "agent:pi"
        self.assertEqual(self.view.single_agent(), "agent:pi")


if __name__ == "__main__":
    unittest.main()
