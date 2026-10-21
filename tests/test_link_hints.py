import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import graph  # noqa: E402
import inventory  # noqa: E402
import link_check  # noqa: E402
import link_hints  # noqa: E402


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "failed-links.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_remember_load_forget(self):
        key = link_hints.key("agent:pi", "runtime:jan")
        link_hints.remember(key, "provider", True, [{"level": "error", "text": "x" * 1000},
                                                   {"level": "bogus", "text": "dropped"}], "gemma", self.path)
        entry = link_hints.load(self.path)[key]
        self.assertEqual(len(entry["hints"]), 1)
        self.assertEqual(len(entry["hints"][0]["text"]), link_hints.MAX_TEXT)
        self.assertEqual((entry["applied"], entry["target"], link_hints.level(entry)), (True, "gemma", "error"))
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        self.assertTrue(link_hints.forget(key, self.path))
        self.assertFalse(link_hints.forget(key, self.path))
        self.assertEqual(link_hints.load(self.path), {})

    def test_bad_entries_ignored(self):
        self.path.write_text('{"nonsense": 1, "agent:pi|runtime:x": {"kind": "evil", "hints": []}}')
        self.assertEqual(link_hints.load(self.path), {})

    def test_failing(self):
        self.assertTrue(link_hints.failing([{"level": "warning", "text": "w"}]))
        self.assertFalse(link_hints.failing([{"level": "ok", "text": "fine"}]))


class GraphTest(unittest.TestCase):
    def test_failed_lines_drawn_and_marked(self):
        built = graph.Graph()
        for node in (graph.Node("agent:pi", 1, "Pi", kind="agent"), graph.Node("runtime:jan", 2, "Jan", kind="runtime"),
                     graph.Node("mcp:x", 0, "x", kind="mcp")):
            built.add(node)
        built.link("agent:pi", "mcp:x", "mcp")
        failures = {"agent:pi|runtime:jan": {"kind": "provider", "applied": False,
                                             "hints": [{"level": "error", "text": "no"}]},
                    "agent:pi|mcp:x": {"kind": "mcp", "applied": True, "hints": [{"level": "warning", "text": "w"}]},
                    "agent:pi|runtime:gone": {"kind": "provider", "applied": False,
                                              "hints": [{"level": "error", "text": "no"}]}}
        graph.mark_failures(built, failures)
        edges = {(e.source, e.target): (e.kind, e.failed) for e in built.edges}
        self.assertEqual(edges[("agent:pi", "runtime:jan")], ("failed", "error"))     # never written: drawn anyway
        self.assertEqual(edges[("agent:pi", "mcp:x")], ("mcp", "warning"))
        self.assertNotIn(("agent:pi", "runtime:gone"), edges)                        # its box is gone


class CheckTest(unittest.TestCase):
    def runtime(self, **kw):
        values = {"installed": True, "running": False, "service": "jan", "models": ["gemma-4-e2b"],
                  "address": "127.0.0.1:6767"}
        values.update(kw)
        return inventory.Runtime("jan", "Jan", **values)

    def levels(self, hints):
        return [h["level"] for h in hints]

    def test_provider_ok_when_it_starts_with_the_agent(self):
        with mock.patch.object(link_check.connect, "is_connected", lambda a, k: True), \
                mock.patch.object(link_check, "model_context", lambda rt, m: 32768):
            hints = link_check.check_provider("pi", "Pi", self.runtime(), "gemma-4-e2b", with_agents=True)
        self.assertEqual(self.levels(hints), ["ok"])

    def test_provider_problems(self):
        with mock.patch.object(link_check.connect, "is_connected", lambda a, k: False), \
                mock.patch.object(link_check, "model_context", lambda rt, m: 8192):
            hints = link_check.check_provider("pi", "Pi", self.runtime(service=""), "other-model", with_agents=True)
        self.assertEqual(self.levels(hints), ["error", "error", "error", "warning"])
        self.assertIn("8192", hints[-1]["text"])

    def test_freshly_installed_provider_says_what_to_do(self):
        # No models yet and no model chosen: each provider names its own next step.
        for key, name, words in (("ollama", "Ollama", "Models…"), ("llamacpp", "llama.cpp", "Models…"),
                                 ("lmstudio", "LM Studio", "Discover"), ("jan", "Jan", "Jan app"),
                                 ("koboldcpp", "KoboldCpp", "Models…"), ("vllm", "vLLM", "VLLM_MODEL")):
            with self.subTest(key), mock.patch.object(link_check.connect, "is_connected", lambda a, k: True):
                rt = inventory.Runtime(key, name, True, False, service=key, address="127.0.0.1:1")
                hints = link_check.check_provider("pi", "Pi", rt, "", with_agents=True)
                self.assertEqual(self.levels(hints), ["error"])
                self.assertIn(words, hints[0]["text"])

    def test_no_model_chosen_and_no_service_say_how_to_go_on(self):
        with mock.patch.object(link_check.connect, "is_connected", lambda a, k: True), \
                mock.patch.object(link_check.connect, "is_active", lambda a, k: False):
            rt = inventory.Runtime("ollama", "Ollama", True, False, address="127.0.0.1:1", models=["qwen3:8b"])
            hints = link_check.check_provider("opencode", "OpenCode", rt, "", with_agents=True)
        self.assertEqual(self.levels(hints), ["warning", "error"])
        self.assertIn("click one of Ollama's models", hints[0]["text"])
        self.assertIn("ollama serve", hints[1]["text"])

    def test_provider_running_on_a_foreign_port(self):
        with mock.patch.object(link_check.connect, "is_connected", lambda a, k: True), \
                mock.patch.object(link_check.models_ctl, "loopback_listeners", lambda port: [os.getuid() + 1]), \
                mock.patch.object(link_check, "model_context", lambda rt, m: 0):
            hints = link_check.check_provider("pi", "Pi", self.runtime(running=True), "gemma-4-e2b", with_agents=True)
        self.assertEqual(self.levels(hints), ["error"])

    def test_provider_running_without_the_model(self):
        with mock.patch.object(link_check.connect, "is_connected", lambda a, k: True), \
                mock.patch.object(link_check.models_ctl, "loopback_listeners", lambda port: [os.getuid()]), \
                mock.patch.object(link_check, "offered_models", lambda rt: ["something-else"]), \
                mock.patch.object(link_check, "model_context", lambda rt, m: 0):
            hints = link_check.check_provider("pi", "Pi", self.runtime(running=True), "gemma-4-e2b", with_agents=True)
        self.assertEqual(self.levels(hints), ["error"])

    def test_mcp(self):
        entry = {"transport": "stdio", "command": "/nowhere/x-cli", "args": ["mcp"], "env": {}, "headers": {}, "url": ""}
        with mock.patch.object(link_check.mcp_ctl, "raw_entries", lambda a: {"x": entry}), \
                mock.patch.object(link_check.mcp_ctl, "normal", lambda raw: raw):
            def fail(_entry):
                raise OSError("No such file")
            self.assertEqual(self.levels(link_check.check_mcp("pi", "Pi", "x", fail)), ["error", "error"])
            ok = link_check.check_mcp("pi", "Pi", "x", lambda e: {"tools": [1, 2]})
            self.assertEqual(self.levels(ok), ["ok"])
            self.assertEqual(self.levels(link_check.check_mcp("pi", "Pi", "missing", lambda e: {})), ["error"])

    def test_skills(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp, "skills")
            (folder / "one").mkdir(parents=True)
            (folder / "one/SKILL.md").write_text("---\nname: one\n---\n")
            own = Path(tmp, "own")
            own.mkdir()
            (own / "two").symlink_to(folder / "two")          # broken: two is not there
            with mock.patch.object(link_check.skills_ctl, "skill_dirs", lambda a: [own]):
                hints = link_check.check_skills("pi", "Pi", str(folder))
            self.assertEqual(self.levels(hints), ["error"])
            self.assertIn("two", hints[0]["text"])
            self.assertEqual(self.levels(link_check.check_skills("pi", "Pi", str(Path(tmp, "gone")))), ["error"])


if __name__ == "__main__":
    unittest.main()
