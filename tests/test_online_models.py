"""Switching an agent's online model: where the choices come from and what is written."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import connect  # noqa: E402
import online_models as om  # noqa: E402


class OnlineModelsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_choices(self):
        self.assertEqual(om.choices("claude", "opus", self.home), ["fable", "opus", "sonnet"])
        self.assertEqual(om.choices("claude", "claude-x-1", self.home)[0], "claude-x-1")   # in use: always there
        (self.home / ".codex").mkdir()
        (self.home / ".codex/models_cache.json").write_text(json.dumps({"models": [
            {"slug": "gpt-a", "visibility": "list"}, {"slug": "hidden", "visibility": "hide"},
            {"slug": "bad name;rm", "visibility": "list"}]}))
        self.assertEqual(om.choices("codex", "", self.home), ["gpt-a"])
        self.assertEqual(om.choices("hermes", "", self.home), [])

    def test_claude_switch_keeps_other_settings(self):
        (self.home / ".claude").mkdir()
        (self.home / ".claude/settings.json").write_text(json.dumps({"model": "opus", "theme": "dark"}))
        plan = om.plan("claude", "sonnet", self.home)
        data = json.loads(plan.changes[0].new)
        self.assertEqual((data["model"], data["theme"]), ("sonnet", "dark"))
        self.assertEqual(om.plan("claude", "sonnet", self.home).agent, "claude")
        with self.assertRaises(ValueError):
            om.plan("claude", "x; rm -rf", self.home)

    def test_codex_model_before_the_first_table(self):
        (self.home / ".codex").mkdir()
        cfg = self.home / ".codex/config.toml"
        cfg.write_text('approval = "never"\n\n[mcp_servers.x]\ncommand = "y"\nmodel = "inside a table"\n')
        new = om.plan("codex", "gpt-a", self.home).changes[0].new
        self.assertTrue(new.startswith('model = "gpt-a"\napproval'))
        self.assertIn('model = "inside a table"', new)                 # a table's own key untouched
        cfg.write_text(new)
        again = om.plan("codex", "gpt-b", self.home).changes[0].new
        self.assertEqual(again.count("\nmodel = ") + again.startswith("model = "), 2)
        self.assertTrue(again.startswith('model = "gpt-b"'))
        backup = connect.apply(om.plan("codex", "gpt-b", self.home), self.home / "b")
        connect.undo(backup)
        self.assertEqual(cfg.read_text(), new)


if __name__ == "__main__":
    unittest.main()
