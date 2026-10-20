import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import connect  # noqa: E402
import graph  # noqa: E402
import install_ctl  # noqa: E402
import provider_watch  # noqa: E402
import providers  # noqa: E402


class RegistryTest(unittest.TestCase):
    def test_keys_unique_and_kinds(self):
        keys = [p.key for p in providers.all_providers()]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertTrue(all(p.kind == "local" for p in providers.local()))
        self.assertTrue(all(p.kind == "online" for p in providers.online()))
        self.assertEqual(providers.get("ollama").name, "Ollama")
        self.assertIsNone(providers.get("nope"))

    def test_every_local_provider_is_complete(self):
        for p in providers.local():
            self.assertTrue(callable(p.detect), p.key)
            self.assertTrue(callable(p.installer), p.key)
            self.assertTrue(p.connect_key.endswith("-local"), p.key)
            self.assertEqual(p.connect_label, f"{p.name} (local)")
            self.assertEqual(p.detect.__module__, f"provider_{p.key}")


class SameTablesAsBeforeTest(unittest.TestCase):
    """The tables built from the registry are exactly the ones the modules had written out."""

    def test_connect(self):
        self.assertEqual(connect.KEYS, {"llamacpp": "llama-local", "ollama": "ollama-local",
                                        "lmstudio": "lmstudio-local", "vllm": "vllm-local", "jan": "jan-local",
                                        "koboldcpp": "koboldcpp-local"})
        self.assertEqual(connect.LABELS, {"llamacpp": "llama.cpp (local)", "ollama": "Ollama (local)",
                                          "lmstudio": "LM Studio (local)", "vllm": "vLLM (local)",
                                          "jan": "Jan (local)", "koboldcpp": "KoboldCpp (local)"})

    def test_graph(self):
        self.assertEqual(graph.CLOUD, {
            "claude": "Anthropic", "codex": "OpenAI", "copilot": "GitHub Copilot", "cursor-agent": "Cursor",
            "gemini": "Google", "grok": "xAI", "muse": "Meta"})       # OpenClaw: none of its own
        self.assertEqual(graph.PROVIDER_NAMES, {
            "anthropic": "Anthropic", "openai": "OpenAI", "openai-codex": "OpenAI", "google": "Google",
            "gemini": "Google", "github-copilot": "GitHub Copilot", "xai": "xAI", "nous": "Nous Research",
            "openrouter": "OpenRouter", "groq": "Groq", "mistral": "Mistral", "deepseek": "DeepSeek",
            "fireworks": "Fireworks", "together": "Together AI", "opencode": "OpenCode Zen", "zai": "Z.ai",
            "openclaw": "OpenClaw"})

    def test_installers(self):
        self.assertEqual({k: type(v).__name__ for k, v in install_ctl.INSTALLERS.items()}, {
            "ollama": "Ollama", "llamacpp": "LlamaCpp", "lmstudio": "LmStudio", "vllm": "Vllm", "jan": "Jan",
            "koboldcpp": "KoboldCpp"})
        for key, inst in install_ctl.INSTALLERS.items():
            self.assertEqual(inst.key, key)


class WatcherConfigTest(unittest.TestCase):
    """provider_watch.py runs isolated (stdlib only): the app writes what it needs to know of each
    local LLM into its config, from the registry."""

    def test_written_from_registry(self):
        import json
        import tempfile
        from types import SimpleNamespace as NS
        rts = [NS(key="koboldcpp", installed=True, agents=["pi"], active_agents=["pi"], service="koboldcpp",
                  address="127.0.0.1:5001"),
               NS(key="lmstudio", installed=True, agents=["bionic", "opencode"], active_agents=["bionic", "opencode"],
                  service="", address="127.0.0.1:1234")]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.json"
            self.assertTrue(provider_watch.write_config(NS(runtimes=rts), path))
            data = json.loads(path.read_text())
            self.assertEqual(data["runtimes"]["koboldcpp"]["unload"],
                             {"path": "/api/admin/reload_config", "body": {"filename": "unload_model"}})
            self.assertNotIn("stays_up", data["runtimes"]["lmstudio"])
            self.assertEqual(data["agents"], {"pi": ["koboldcpp"], "opencode": ["lmstudio"]})   # Bionic serves itself
            agents, runtimes = provider_watch.read_config(path)
            self.assertTrue(runtimes["koboldcpp"]["stays_up"])


class ProviderExtendTest(unittest.TestCase):
    """providers.extend: facts an extension add-on adds to a provider, as for agents."""

    def tearDown(self):
        providers.remove_added()

    def test_empty_fields_only_first_wins(self):
        mine, other = (lambda rt: None), (lambda rt: None)
        self.assertEqual(providers.extend("llamacpp", {"usage": mine}, "ext-a"), [])
        providers.extend("llamacpp", {"usage": other}, "ext-b")
        self.assertIs(providers.get("llamacpp").usage, mine)
        self.assertEqual(providers.conflicts(), {"ext-b": ["llamacpp.usage"]})

    def test_never_what_installs_or_names_it(self):
        self.assertEqual(sorted(providers.extend("llamacpp", {"installer": 1, "key": "x", "install": ()}, "ext-a")),
                         ["install", "installer", "key"])
        self.assertEqual(providers.get("llamacpp").key, "llamacpp")

    def test_removing_the_addon_removes_its_facts(self):
        providers.extend("ollama", {"usage": lambda rt: None}, "ext-a")
        providers.remove_added("ext-a")
        self.assertIsNone(providers.get("ollama").usage)


if __name__ == "__main__":
    unittest.main()
