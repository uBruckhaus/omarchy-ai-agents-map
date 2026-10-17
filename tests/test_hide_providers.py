"""Hide: a provider can be hidden unless a shown agent is fully connected to it (a line to the
provider and one of its models in use). A line without a model does not count."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import graph  # noqa: E402
from graph import Graph, Node  # noqa: E402


def setup():
    g = Graph()
    for node in (Node("agent:claude", 0, "Claude Code", "", "agent"), Node("agent:hermes", 0, "Hermes", "", "agent"),
                 Node("cloud:anthropic", 2, "Anthropic", "", "cloud"), Node("runtime:ollama", 2, "Ollama", "", "runtime"),
                 Node("model:cloud:anthropic:opus", 3, "opus", "", "model"),
                 Node("model:runtime:ollama:qwen", 3, "qwen", "", "model")):
        g.add(node)
    g.link("agent:claude", "cloud:anthropic", "uses")
    g.link("cloud:anthropic", "model:cloud:anthropic:opus", "serves")
    g.link("agent:claude", "model:cloud:anthropic:opus", "default")
    g.link("agent:hermes", "runtime:ollama", "available")        # a line, but no Ollama model
    g.link("runtime:ollama", "model:runtime:ollama:qwen", "serves")
    return g


class HideProvidersTest(unittest.TestCase):
    def test_fully_connected_provider_is_in_use(self):
        users = graph.provider_users(setup(), {"agent:claude": "Claude Code", "agent:hermes": "Hermes"})
        self.assertEqual(users, {"cloud:anthropic": ["Claude Code"]})

    def test_line_without_model_does_not_count(self):
        users = graph.provider_users(setup(), {"agent:hermes": "Hermes"})
        self.assertNotIn("runtime:ollama", users)

    def test_hidden_agent_frees_its_provider(self):
        users = graph.provider_users(setup(), {"agent:hermes": "Hermes"})    # Claude Code hidden
        self.assertNotIn("cloud:anthropic", users)

    def test_model_of_another_provider_does_not_count(self):
        g = setup()
        g.link("agent:hermes", "model:cloud:anthropic:opus", "default")       # its model, but via no line
        self.assertNotIn("runtime:ollama", graph.provider_users(g, {"agent:hermes": "Hermes"}))
        self.assertNotIn("cloud:anthropic", graph.provider_users(g, {"agent:hermes": "Hermes"}))


if __name__ == "__main__":
    unittest.main()
