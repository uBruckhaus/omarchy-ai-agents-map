import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import connect  # noqa: E402
import inventory  # noqa: E402

HERMES = """model:
  default: stealth/model
  provider: nous
  base_url: ''
agent:
  x: 1
image_gen:
  provider: nous
whatsapp:
  a: b
"""


class ConnectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        patcher = mock.patch.object(connect, "HOME", self.home)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        self.rt = inventory.Runtime("llamacpp", "llama.cpp", True, False, address="127.0.0.1:8080",
                                    models=["Qwen", "Gemma"])

    def write(self, rel, text):
        path = self.home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def test_hermes_add_use_now_and_remove(self):
        cfg = self.write(".hermes/config.yaml", HERMES)
        result = connect.plan("hermes", self.rt, "Qwen", use_now=True, context={"Qwen": 98304})
        self.assertFalse(result.connected)
        new = result.changes[0].new
        self.assertIn("providers:\n  llama-local:\n    name: llama.cpp (local)\n    api: http://127.0.0.1:8080/v1", new)
        self.assertIn("      Qwen:\n        context_length: 98304\n      Gemma: {}", new)
        self.assertIn("model:\n  default: Qwen\n  provider: llama-local\n  base_url: ''\nagent:", new)
        self.assertIn("image_gen:\n  provider: nous", new)     # other blocks untouched
        backup = connect.apply(result, self.home / "backups")
        self.assertTrue(connect.is_connected("hermes", "llamacpp"))
        again = connect.plan("hermes", self.rt, "Gemma", use_now=False)
        self.assertTrue(again.connected)
        self.assertEqual(again.changes[0].new.count("llama-local:"), 1)   # replaced, not doubled
        connect.undo(backup)
        self.assertEqual(cfg.read_text(), HERMES)

    def test_hermes_remove_leaves_rest(self):
        self.write(".hermes/config.yaml", HERMES)
        connect.apply(connect.plan("hermes", self.rt, "Qwen", use_now=False), self.home / "b")
        removed = connect.plan("hermes", self.rt, "Qwen", use_now=False, remove=True)
        self.assertEqual(removed.changes[0].new, HERMES)

    def test_hermes_remove_the_provider_in_use(self):
        # The model must not keep pointing at a provider that is gone (the map showed a dead line).
        self.write(".hermes/config.yaml", HERMES)
        connect.apply(connect.plan("hermes", self.rt, "Qwen", use_now=True), self.home / "b")
        ollama = inventory.Runtime("ollama", "Ollama", True, False, address="127.0.0.1:11434", models=["mistral"])
        connect.apply(connect.plan("hermes", ollama, "mistral", use_now=False), self.home / "b2")
        removed = connect.plan("hermes", self.rt, "Qwen", use_now=False, remove=True).changes[0].new
        self.assertIn("model:\n  default: mistral\n  provider: ollama-local\n", removed)   # the next one takes over
        self.write(".hermes/config.yaml", removed)
        alone = connect.plan("hermes", ollama, "mistral", use_now=False, remove=True).changes[0].new
        self.assertIn("model:\n  base_url: ''\nagent:", alone)                        # none left: no model
        self.assertNotIn("ollama-local", alone)

    def test_pi(self):
        self.write(".pi/agent/models.json", json.dumps({"providers": {"other": {"baseUrl": "x"}}}))
        self.write(".pi/agent/settings.json", json.dumps({"theme": "dark"}))
        result = connect.plan("pi", self.rt, "Qwen", use_now=True)
        connect.apply(result, self.home / "b")
        models = json.loads((self.home / ".pi/agent/models.json").read_text())
        settings = json.loads((self.home / ".pi/agent/settings.json").read_text())
        self.assertEqual(sorted(models["providers"]), ["llama-local", "other"])
        self.assertEqual(models["providers"]["llama-local"]["baseUrl"], "http://127.0.0.1:8080/v1")
        self.assertEqual((settings["defaultProvider"], settings["defaultModel"], settings["theme"]),
                         ("llama-local", "Qwen", "dark"))

    def test_pi_reconnected_keeps_pictures_and_thinking_switch(self):
        compat = {"thinkingFormat": "chat-template"}
        self.write(".pi/agent/models.json", json.dumps({"providers": {"llama-local": {"models": [
            {"id": "Qwen", "name": "Qwen", "input": ["text", "image"], "reasoning": True, "compat": compat,
             "cost": {"input": 9}}]}}}))
        self.write(".pi/agent/settings.json", "{}")
        connect.apply(connect.plan("pi", self.rt, "Qwen", use_now=False), self.home / "b")
        entry = json.loads((self.home / ".pi/agent/models.json").read_text())["providers"]["llama-local"]["models"][0]
        self.assertEqual((entry["input"], entry["reasoning"], entry["compat"]), (["text", "image"], True, compat))
        self.assertNotIn("cost", entry)            # only what AI Playground sets is carried over

    def test_openclaw_new_file_then_removed(self):
        result = connect.plan("openclaw", self.rt, "Qwen", use_now=True, context={"Qwen": 98304})
        backup = connect.apply(result, self.home / "b")
        data = json.loads((self.home / ".openclaw/openclaw.json").read_text())
        entry = data["models"]["providers"]["llama-local"]
        self.assertEqual((entry["baseUrl"], entry["api"], entry["models"][0]["contextWindow"]),
                         ("http://127.0.0.1:8080/v1", "openai-completions", 98304))
        self.assertEqual(data["agents"]["defaults"]["model"]["primary"], "llama-local/Qwen")
        self.assertTrue(connect.is_connected("openclaw", "llamacpp"))
        connect.undo(backup)
        self.assertFalse((self.home / ".openclaw/openclaw.json").exists())

    def test_openclaw_keeps_its_other_settings(self):
        self.write(".openclaw/openclaw.json", json.dumps({
            "gateway": {"port": 18789}, "agents": {"defaults": {"model": {"primary": "anthropic/claude",
                                                                         "fallbacks": ["openai/gpt"]}}},
            "models": {"providers": {"other": {"baseUrl": "https://x.example/v1"}}}}))
        connect.apply(connect.plan("openclaw", self.rt, "Qwen", use_now=True), self.home / "b")
        data = json.loads((self.home / ".openclaw/openclaw.json").read_text())
        self.assertEqual(data["gateway"], {"port": 18789})
        self.assertEqual(data["agents"]["defaults"]["model"], {"primary": "llama-local/Qwen", "fallbacks": ["openai/gpt"]})
        self.assertEqual(sorted(data["models"]["providers"]), ["llama-local", "other"])
        connect.apply(connect.plan("openclaw", self.rt, "Qwen", use_now=False, remove=True), self.home / "b")
        data = json.loads((self.home / ".openclaw/openclaw.json").read_text())
        self.assertEqual(sorted(data["models"]["providers"]), ["other"])
        self.assertEqual(data["agents"]["defaults"]["model"], {"fallbacks": ["openai/gpt"]})

    def test_openclaw_json5_is_left_alone(self):
        self.write(".openclaw/openclaw.json", "{ gateway: { port: 18789 }, }")
        with self.assertRaises(ValueError):
            connect.plan("openclaw", self.rt, "Qwen", use_now=True)

    def test_no_model_known_keeps_the_agents_model(self):
        # A freshly installed local LLM may not list a model yet: "ollama-local/" would break OpenCode.
        self.write(".config/opencode/opencode.json", '{"model": "anthropic/claude"}')
        rt = inventory.Runtime("ollama", "Ollama", True, False, address="127.0.0.1:11434")
        connect.apply(connect.plan("opencode", rt, "", use_now=True), self.home / "b")
        data = json.loads((self.home / ".config/opencode/opencode.json").read_text())
        self.assertIn("ollama-local", data["provider"])
        self.assertEqual(data["model"], "anthropic/claude")

    def test_opencode_new_file_and_undo_deletes_it(self):
        result = connect.plan("opencode", self.rt, "Qwen", use_now=True)
        backup = connect.apply(result, self.home / "b")
        data = json.loads((self.home / ".config/opencode/opencode.json").read_text())
        self.assertEqual(data["model"], "llama-local/Qwen")
        connect.undo(backup)
        self.assertFalse((self.home / ".config/opencode/opencode.json").exists())

    def test_changed_file_is_not_overwritten(self):
        cfg = self.write(".hermes/config.yaml", HERMES)
        result = connect.plan("hermes", self.rt, "Qwen", use_now=False)
        cfg.write_text(HERMES + "extra: 1\n")
        with self.assertRaises(RuntimeError):
            connect.apply(result, self.home / "b")

    def test_unsupported_agent(self):
        with self.assertRaises(ValueError):
            connect.plan("claude", self.rt, "Qwen", use_now=False)


if __name__ == "__main__":
    unittest.main()


class ChatModelsTest(unittest.TestCase):
    def test_speech_models_left_out(self):
        self.assertEqual(connect.chat_models(["Qwen3.8-27B", "Qwen3-TTS-12Hz", "qwen3-tts-tokenizer", "nomic-embed"]),
                         ["Qwen3.8-27B"])


class MoveTest(ConnectTest):
    def test_move_hermes_from_llama_to_ollama(self):
        self.write(".hermes/config.yaml", HERMES)
        connect.apply(connect.plan("hermes", self.rt, "Qwen", use_now=True), self.home / "b")
        ollama = inventory.Runtime("ollama", "Ollama", True, False, address="127.0.0.1:11434", models=["gemma:latest"])
        moved = connect.plan("hermes", ollama, "gemma:latest", use_now=True, replaces=self.rt)
        new = moved.changes[0].new
        self.assertNotIn("  llama-local:", new)
        self.assertIn("  ollama-local:\n", new)
        self.assertIn("  default: gemma:latest\n  provider: ollama-local", new)


class ActiveTest(ConnectTest):
    def test_is_active(self):
        self.write(".hermes/config.yaml", HERMES)
        connect.apply(connect.plan("hermes", self.rt, "Qwen", use_now=False), self.home / "b")
        self.assertFalse(connect.is_active("hermes", "llamacpp"))
        connect.apply(connect.plan("hermes", self.rt, "Qwen", use_now=True), self.home / "b")
        self.assertTrue(connect.is_active("hermes", "llamacpp"))


class UndoFoldersTest(unittest.TestCase):
    def test_undo_removes_folders_it_made(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as root:
            base = Path(root, "defaults")
            base.mkdir()
            keep = base / "pub/other.json"
            keep.parent.mkdir()
            keep.write_text("{}")
            new = base / "pub/repo/deep/m.json"
            backup = connect.apply(connect.Plan("x", "lmstudio", "", [connect.Change(new, "", "{}\n")]),
                                   backup_root=Path(root, "bk"))
            self.assertTrue(new.exists())
            connect.undo(backup)
            self.assertFalse(new.exists())
            self.assertFalse((base / "pub/repo").exists())     # made by apply: gone
            self.assertTrue(keep.exists())                     # was there before: kept
