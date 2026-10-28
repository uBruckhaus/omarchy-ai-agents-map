import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import dsh_ctl  # noqa: E402
import graph  # noqa: E402
import harnesses  # noqa: E402
import online_models  # noqa: E402

PATCH = """- id: llm-pi-ai
  config:
    providers:
      llama-local:
        baseURL: http://127.0.0.1:8080/v1
        models:
        - id: qwen
- id: agent-default-model
  name: '@deepseek-ai/dsh-agent-default-model'
  config:
    provider: llama-local
    model: qwen
    reasoningEffort: high
"""


class DshTest(unittest.TestCase):
    def test_the_default_model_changes_and_the_rest_stays(self):
        new = dsh_ctl.model_text(PATCH, "deepseek-official", "deepseek-v4-pro")
        self.assertEqual(dsh_ctl.default_model(new), ("deepseek-official", "deepseek-v4-pro"))
        self.assertIn("reasoningEffort: high", new)
        self.assertIn("llama-local", dsh_ctl.providers(new))
        for bad in (("Deep Seek", "x"), ("deepseek-official", "x\n- id: evil")):
            with self.assertRaises(ValueError):
                dsh_ctl.model_text(PATCH, *bad)

    def test_its_own_route_is_read_from_its_packages(self):
        with tempfile.TemporaryDirectory() as tmp:
            packages = Path(tmp)
            (packages / "dsh-base").mkdir()
            (packages / "dsh-base/cordis.patch.yml").write_text(
                "    - id: agent-default-model\n      name: '@deepseek-ai/dsh-agent-default-model'\n"
                "      config:\n        provider: deepseek-official\n        model: deepseek-flash\n")
            (packages / "dsh-llm-deepseek/lib").mkdir(parents=True)
            (packages / "dsh-llm-deepseek/lib/index.js").write_text(
                'const DEFAULT_MODELS = [{\n\tid: "deepseek-flash",\n\tname: "Flash"\n}, {\n\tid: "deepseek-v4-pro"\n}];\n')
            self.assertEqual(dsh_ctl.builtin_choices(packages),
                             ["deepseek-official/deepseek-flash", "deepseek-official/deepseek-v4-pro"])
            (packages / "dsh-llm-deepseek/lib/index.js").write_text("nothing here")
            self.assertEqual(dsh_ctl.builtin_choices(packages), [])          # nothing guessed


class HermesTest(unittest.TestCase):
    def test_models_of_its_providers_keep_their_tags(self):
        hermes = harnesses.get("hermes")
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".hermes").mkdir()
            (home / ".hermes/config.yaml").write_text(
                "model:\n  default: qwen:latest\n  provider: ollama-local\nproviders:\n  ollama-local:\n"
                "    api: http://127.0.0.1:11434/v1\n    models:\n      qwen:latest: {}\n      gemma3:\n"
                "        context_length: 8192\n  openrouter:\n    models:\n      anthropic/claude-x: {}\n")
            self.assertEqual(hermes.models[0]("", home), ["ollama-local/qwen:latest", "ollama-local/gemma3",
                                                         "openrouter/anthropic/claude-x"])
            path, old, new, provider = hermes.models[1]("openrouter/anthropic/claude-x", home)
            self.assertIn('  default: "anthropic/claude-x"\n  provider: "openrouter"\n', new)
            self.assertEqual(provider, "openrouter")

    def test_a_scalar_like_id_stays_a_name_and_an_inline_block_is_not_edited(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("hermes_hooks", Path(__file__).resolve().parent.parent
                                                      / "adapters/hermes.py")
        hooks = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hooks)
        self.assertIn('  default: "null"\n', hooks.model_text("model:\n  default: x\n", "openrouter", "null"))
        with self.assertRaises(ValueError):
            hooks.model_text("model: {default: old, provider: openrouter}\nagent: {}\n", "openrouter", "new")
        with self.assertRaises(ValueError):                # four-space children: not edited
            hooks.model_text("model:\n    default: old\n    provider: x\n", "openrouter", "new")

    def test_a_broken_settings_file_gives_no_choices(self):
        def broken(_current, _home):
            raise ValueError("not JSON")
        with mock.patch.object(online_models, "_switcher", return_value=(broken, None)):
            self.assertEqual(online_models.choices("pi"), [])


class TargetTest(unittest.TestCase):
    def test_the_exact_choice_of_the_service_shown(self):
        found = ["llama-local/qwen", "deepseek-official/deepseek-flash", "deepseek-official/deepseek-v4-pro"]
        with mock.patch.object(online_models, "choices", lambda *a, **k: found):
            self.assertEqual(online_models.target("dsh", "DeepSeek", "deepseek-v4-pro"),
                             "deepseek-official/deepseek-v4-pro")
            self.assertIsNone(online_models.target("dsh", "OpenAI", "deepseek-v4-pro"))
            self.assertIsNone(online_models.target("dsh", "DeepSeek", "gpt-x"))
        with mock.patch.object(online_models, "choices", lambda *a, **k: ["gpt-a", "gpt-b"]):
            self.assertEqual(online_models.target("codex", "OpenAI", "gpt-b"), "gpt-b")

    def test_split_at_the_first_slash(self):
        self.assertEqual(online_models.split("openrouter/qwen/qwen3"), ("openrouter", "qwen/qwen3"))
        self.assertEqual(online_models.split("gpt-6"), ("", "gpt-6"))


class MapTest(unittest.TestCase):
    def test_online_providers_of_a_local_harness_are_offered_quietly(self):
        g = graph.Graph()
        agent = mock.Mock(command="dsh")
        found = ["llama-local/qwen", "deepseek-official/deepseek-flash"]
        with mock.patch.object(online_models, "choices", lambda *a, **k: found):
            graph.online_choices(g, agent, "agent:dsh", "qwen", "llama-local", {"llama-local": "llamacpp"}, {})
        ids = {n.id for n in g.nodes}
        self.assertIn("cloud:deepseek", ids)
        self.assertIn("model:cloud:deepseek:deepseek-flash", ids)
        self.assertNotIn("model:cloud:llama-local:qwen", ids)            # the local LLM's box shows it
        self.assertIn(("agent:dsh", "cloud:deepseek", "offer"), {(e.source, e.target, e.kind) for e in g.edges})


if __name__ == "__main__":
    unittest.main()
