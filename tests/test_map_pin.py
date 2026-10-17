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


if __name__ == "__main__":
    unittest.main()
