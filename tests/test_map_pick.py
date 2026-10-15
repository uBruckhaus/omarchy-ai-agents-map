import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import graph  # noqa: E402
import map_view  # noqa: E402


def sample() -> graph.Graph:
    g = graph.Graph()
    g.add(graph.Node("agent:hermes", 1, "Hermes", kind="agent"))
    g.add(graph.Node("agent:pi", 1, "Pi", kind="agent"))
    g.add(graph.Node("runtime:llamacpp", 2, "llama.cpp", kind="runtime", status="running"))
    g.add(graph.Node("runtime:ollama", 2, "Ollama", kind="runtime", status="running"))
    g.add(graph.Node("model:runtime:llamacpp:qwen3-coder", 3, "Qwen3-Coder", kind="model"))
    g.add(graph.Node("model:runtime:ollama:gemma3", 3, "gemma3", kind="model"))
    g.add(graph.Node("more:runtime:llamacpp", 3, "+3 more", kind="more"))
    g.link("agent:hermes", "runtime:llamacpp", "available")
    g.link("runtime:llamacpp", "model:runtime:llamacpp:qwen3-coder", "serves")
    g.link("runtime:llamacpp", "more:runtime:llamacpp", "serves")
    g.link("runtime:ollama", "model:runtime:ollama:gemma3", "serves")
    return g


class SelectionOnly:
    """MapView's selection logic without the widget."""
    kind_of = map_view.MapView.kind_of
    model_pick = map_view.MapView.model_pick


class ModelPickTest(unittest.TestCase):
    def setUp(self):
        self.view = SelectionOnly()
        self.view.graph = sample()
        self.view.selected = []

    def test_model_of_a_connected_llm(self):
        self.view.selected = ["agent:hermes"]
        self.assertEqual(self.view.model_pick("model:runtime:llamacpp:qwen3-coder"),
                         ("agent:hermes", "runtime:llamacpp", "Qwen3-Coder"))

    def test_not_connected_more_box_or_no_agent(self):
        self.view.selected = ["agent:hermes"]
        self.assertIsNone(self.view.model_pick("model:runtime:ollama:gemma3"))
        self.assertIsNone(self.view.model_pick("more:runtime:llamacpp"))
        self.view.selected = ["runtime:llamacpp"]
        self.assertIsNone(self.view.model_pick("model:runtime:llamacpp:qwen3-coder"))
        self.view.selected = ["agent:pi"]
        self.assertIsNone(self.view.model_pick("model:runtime:llamacpp:qwen3-coder"))


class ActivePathTest(unittest.TestCase):
    def test_agent_provider_model(self):
        g = sample()
        g.add(graph.Node("cloud:nous", 2, "Nous", kind="cloud"))
        g.add(graph.Node("model:cloud:nous:hermes-4", 3, "Hermes-4", kind="model"))
        g.link("agent:hermes", "cloud:nous", "uses")
        g.link("cloud:nous", "model:cloud:nous:hermes-4", "serves")
        g.link("agent:hermes", "model:cloud:nous:hermes-4", "default")
        g.link("agent:pi", "runtime:ollama", "uses")
        g.link("agent:pi", "model:runtime:ollama:gemma3", "default")
        paths = map_view.MapView.active_paths(g)
        named = {a: {(g.edges[i].source, g.edges[i].target) for i in edges} for a, edges in paths.items()}
        self.assertEqual(named, {
            "agent:hermes": {("agent:hermes", "cloud:nous"), ("cloud:nous", "model:cloud:nous:hermes-4")},
            "agent:pi": {("agent:pi", "runtime:ollama"), ("runtime:ollama", "model:runtime:ollama:gemma3")},
        })


if __name__ == "__main__":
    unittest.main()
