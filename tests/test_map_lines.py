"""Which lines on the map can be grabbed: an agent's line to a local LLM, and a local LLM's line to
a model that agents use (moving it switches those agents to another model)."""

import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
import graph  # noqa: E402
import map_view  # noqa: E402


def fake_map():
    g = graph.Graph()
    for node in (graph.Node("agent:pi", 1, "Pi", "", "agent"), graph.Node("agent:dsh", 1, "DeepSeek Harness", "", "agent"),
                 graph.Node("runtime:llamacpp", 2, "llama.cpp", "", "runtime"),
                 graph.Node("model:runtime:llamacpp:gemma", 3, "gemma", "", "model"),
                 graph.Node("model:runtime:llamacpp:qwen", 3, "qwen", "", "model"),
                 graph.Node("model:runtime:llamacpp:tts", 3, "tts", "", "model")):
        g.add(node)
    for source, target, kind in (("agent:pi", "runtime:llamacpp", "uses"), ("agent:dsh", "runtime:llamacpp", "uses"),
                                 ("runtime:llamacpp", "model:runtime:llamacpp:gemma", "serves"),
                                 ("runtime:llamacpp", "model:runtime:llamacpp:qwen", "serves"),
                                 ("runtime:llamacpp", "model:runtime:llamacpp:tts", "serves"),
                                 ("agent:pi", "model:runtime:llamacpp:gemma", "default"),
                                 ("agent:dsh", "model:runtime:llamacpp:qwen", "default")):
        g.link(source, target, kind)
    view = types.SimpleNamespace(graph=g, link=None, outside={}, usage=None)
    for name in ("kind_of", "model_users", "movable_edge", "link_target_ok", "model_busy", "loaded_edge"):
        setattr(view, name, types.MethodType(getattr(map_view.MapView, name), view))
    view.model_state = lambda node, use=None: None
    return view, g


class MapLinesTest(unittest.TestCase):
    def test_model_lines_in_use_can_be_grabbed(self):
        view, g = fake_map()
        lines = {(e.source, e.target): view.movable_edge(e) for e in g.edges}
        self.assertTrue(lines[("runtime:llamacpp", "model:runtime:llamacpp:qwen")])
        self.assertTrue(lines[("runtime:llamacpp", "model:runtime:llamacpp:gemma")])
        self.assertFalse(lines[("runtime:llamacpp", "model:runtime:llamacpp:tts")])      # nobody uses it
        self.assertTrue(lines[("agent:pi", "runtime:llamacpp")])
        self.assertEqual(view.model_users("runtime:llamacpp", "model:runtime:llamacpp:qwen"), ["agent:dsh"])

    def test_a_model_line_moves_to_another_model_of_the_same_llm(self):
        view, _g = fake_map()
        view.link = {"remodel": "model:runtime:llamacpp:qwen"}
        self.assertTrue(view.link_target_ok("runtime:llamacpp", "model:runtime:llamacpp:gemma"))
        self.assertFalse(view.link_target_ok("runtime:llamacpp", "model:runtime:llamacpp:qwen"))   # itself
        self.assertFalse(view.link_target_ok("runtime:llamacpp", "agent:pi"))


class LoadedTest(unittest.TestCase):
    def test_a_loaded_model_nobody_uses_can_be_grabbed(self):
        view, g = fake_map()
        line = next(e for e in g.edges if e.target == "model:runtime:llamacpp:tts" and e.kind == "serves")
        self.assertFalse(view.movable_edge(line))                     # not loaded, nobody uses it
        view.model_state = lambda node, use=None: "loaded" if node.label == "tts" else None
        self.assertTrue(view.movable_edge(line))                      # loaded: unload it, or load another


class OnlineTest(unittest.TestCase):
    def test_online_models_and_providers(self):
        view, g = fake_map()
        for node in (graph.Node("agent:claude", 1, "Claude Code", "", "agent"),
                     graph.Node("cloud:anthropic", 2, "Anthropic", "", "cloud"),
                     graph.Node("model:cloud:anthropic:opus", 3, "opus", "", "model"),
                     graph.Node("model:cloud:anthropic:sonnet", 3, "sonnet", "", "model")):
            g.add(node)
        for source, target, kind in (("agent:claude", "cloud:anthropic", "uses"),
                                     ("cloud:anthropic", "model:cloud:anthropic:opus", "serves"),
                                     ("cloud:anthropic", "model:cloud:anthropic:sonnet", "serves"),
                                     ("agent:claude", "model:cloud:anthropic:opus", "default")):
            g.link(source, target, kind)
        view.selected = ["agent:claude"]
        view.model_pick = types.MethodType(map_view.MapView.model_pick, view)
        self.assertEqual(view.model_pick("model:cloud:anthropic:sonnet"), ("agent:claude", "cloud:anthropic", "sonnet"))
        line = next(e for e in g.edges if e.target == "model:cloud:anthropic:opus" and e.kind == "serves")
        self.assertTrue(view.movable_edge(line))
        next(n for n in g.nodes if n.id == "agent:claude").status = "running"
        self.assertEqual(view.model_busy("cloud:anthropic", "model:cloud:anthropic:opus"), [])   # next start
        provider = next(e for e in g.edges if e.target == "cloud:anthropic" and e.kind == "uses")
        self.assertTrue(view.movable_edge(provider))                                  # drag away: sign out
        self.assertTrue(view.link_target_ok("agent:pi", "cloud:anthropic"))           # draw: sign in


class AttachTest(unittest.TestCase):
    def test_a_line_from_a_local_llm_to_its_model(self):
        view, _g = fake_map()
        self.assertTrue(view.link_target_ok("runtime:llamacpp", "model:runtime:llamacpp:tts"))
        self.assertFalse(view.link_target_ok("runtime:llamacpp", "model:runtime:other:x"))        # not its model
        self.assertFalse(view.link_target_ok("model:runtime:llamacpp:tts", "runtime:llamacpp"))   # from the model: no


class BusyTest(unittest.TestCase):
    def test_free_only_when_nothing_uses_it(self):
        view, g = fake_map()
        view.model_state = lambda node, use=None: None
        self.assertEqual(view.model_busy("runtime:llamacpp", "model:runtime:llamacpp:qwen"), [])
        next(n for n in g.nodes if n.id == "agent:dsh").status = "running"
        self.assertEqual(view.model_busy("runtime:llamacpp", "model:runtime:llamacpp:qwen"), ["DeepSeek Harness"])
        view.model_state = lambda node, use=None: "loaded" if node.label == "gemma" else None
        self.assertEqual(view.model_busy("runtime:llamacpp", "model:runtime:llamacpp:gemma"), [])   # only loaded
        view.outside = {"runtime:llamacpp": {"model:runtime:llamacpp:tts": "in use · Video Trans"}}
        self.assertEqual(view.model_busy("runtime:llamacpp", "model:runtime:llamacpp:tts"), ["in use · Video Trans"])


if __name__ == "__main__":
    unittest.main()
