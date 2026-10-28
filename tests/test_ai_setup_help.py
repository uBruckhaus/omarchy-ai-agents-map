import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ai_setup_help as ash  # noqa: E402


class ReadyAgentTest(unittest.TestCase):
    def test_first_signed_in_agent(self):
        self.assertEqual(ash.ready_agent(["claude", "codex"], lambda c: c == "codex"), "codex")

    def test_none_when_no_one_is_signed_in(self):
        self.assertIsNone(ash.ready_agent(["claude"], lambda c: False))
        self.assertIsNone(ash.ready_agent([], lambda c: True))


class PromptTest(unittest.TestCase):
    def make(self, **kw):
        args = dict(kind="mcp", action="install", name="github", details="", app="AI Agents Map",
                    agents=["Claude Code"], runtimes=["Ollama"], mcp=["figcraft"])
        args.update(kw)
        return ash.prompt(**args)

    def test_names_job_inventory_and_rules(self):
        text = self.make()
        self.assertIn('install and set up an MCP server called "github"', text)
        self.assertIn("AI agents: Claude Code; local LLMs: Ollama; MCP servers: figcraft", text)
        self.assertIn("ask me before each step", text)
        self.assertIn("never print, copy or store an API key", text)

    def test_empty_name_asks_for_suggestions(self):
        self.assertIn("suggest two or three", self.make(name="  "))

    def test_remove_cleans_agent_settings(self):
        self.assertIn("every agent's settings", self.make(kind="agent", action="remove", name="goose"))

    def test_user_text_is_one_bounded_line(self):
        text = self.make(name="x\n" * 500, details="a\nb\tc")
        self.assertIn("My notes: a b c", text)
        self.assertTrue(all(len(line) < 1500 for line in text.splitlines()))
        self.assertEqual(len(text.splitlines()), len(self.make(details="a b c").splitlines()))

    def test_nothing_here_yet(self):
        self.assertIn("nothing yet", self.make(agents=[], runtimes=[], mcp=[]))

    def test_unknown_kind_or_action(self):
        with self.assertRaises(ValueError):
            self.make(kind="shell")
        with self.assertRaises(ValueError):
            self.make(action="rm -rf")


if __name__ == "__main__":
    unittest.main()
