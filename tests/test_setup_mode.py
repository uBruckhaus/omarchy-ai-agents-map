import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import graph  # noqa: E402
import map_view  # noqa: E402
from tests.test_map_pick import sample  # noqa: E402


class Hidden:
    """MapView's hiding without the widget."""
    hidden = map_view.MapView.hidden
    selection_hidden = map_view.MapView.selection_hidden

    def __init__(self, g):
        self.graph, self.layers = g, set(graph.LAYERS)
        self.mode_off, self.mode_hidden, self.selected, self.shift = set(), set(), [], False


class SetupModeTest(unittest.TestCase):
    def test_online_leaves_local_llms_and_their_models_out(self):
        view = Hidden(sample())
        view.mode_off = {"local"}
        hidden = view.hidden()
        self.assertIn("runtime:llamacpp", hidden)
        self.assertIn("model:runtime:ollama:gemma3", hidden)          # its provider is gone too
        self.assertNotIn("agent:hermes", hidden)

    def test_local_leaves_agents_out_without_touching_the_user_list(self):
        view = Hidden(sample())
        view.mode_off, view.mode_hidden = {"online"}, {"agent:pi"}
        self.assertIn("agent:pi", view.hidden())
        self.assertNotIn("agent:hermes", view.hidden())
        view.mode_off, view.mode_hidden = set(), set()                  # back to All: everything again
        self.assertEqual(view.hidden(), set())
