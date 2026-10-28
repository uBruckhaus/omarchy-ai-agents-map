import dataclasses
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import agent_guard  # noqa: E402
import harnesses  # noqa: E402


class GuardTest(unittest.TestCase):
    def test_codex_task_asks_and_has_no_mcp(self):
        argv = agent_guard.task("codex", ["/bin/codex", "PROMPT"], names=["figcraft"])
        self.assertEqual(argv[:5], ["/bin/codex", "--sandbox", "read-only", "--ask-for-approval", "on-request"])
        self.assertIn("mcp_servers.figcraft.enabled=false", argv)
        self.assertEqual(argv[-1], "PROMPT")
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)

    def test_a_name_the_option_cannot_spell_refuses(self):
        for bad in ("we.ird", "a b", "x=y", 'q"', ""):
            with self.assertRaises(agent_guard.Refused):
                agent_guard.background("codex", ["/bin/codex", "exec"], names=[bad])

    def test_unknown_agent_is_refused(self):
        claude = harnesses.get("claude")
        with mock.patch("harnesses.get", lambda c: dataclasses.replace(claude, closed=())):
            with self.assertRaises(agent_guard.Refused):
                agent_guard.task("claude", ["/bin/claude", "x"])
            with self.assertRaises(agent_guard.Refused):
                agent_guard.background("claude", ["/bin/claude", "-p"])

    def test_servers_are_read_strictly(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            env = {"CODEX_HOME": str(home)}
            self.assertEqual(agent_guard.servers("codex", "CODEX_HOME", env), [])          # no file: none
            (home / "config.toml").write_text('[mcp_servers.figcraft]\ncommand = "x"\n')
            self.assertEqual(agent_guard.servers("codex", "CODEX_HOME", env), ["figcraft"])  # its home moved
            (home / "config.toml").write_text("[mcp_servers\nbroken")
            with self.assertRaises(agent_guard.Refused):                                  # unreadable: refused
                agent_guard.servers("codex", "CODEX_HOME", env)
            (home / "config.toml").write_text("x" * (agent_guard.MAX_CONFIG + 1))
            with self.assertRaises(agent_guard.Refused):
                agent_guard.servers("codex", "CODEX_HOME", env)

    def test_claude_task_asks_even_where_its_allow_list_allows(self):
        import json
        argv = agent_guard.task("claude", ["/bin/claude", "P"])
        settings = json.loads(argv[argv.index("--settings") + 1])
        self.assertTrue(settings["disableAllHooks"])
        self.assertTrue({"Bash", "Edit", "Write"} <= set(settings["permissions"]["ask"]))
        background = agent_guard.background("claude", ["/bin/claude", "-p"])
        self.assertTrue(json.loads(background[background.index("--settings") + 1])["disableAllHooks"])

    def test_claude_background_has_no_mcp(self):
        self.assertIn("--strict-mcp-config", agent_guard.background("claude", ["/bin/claude", "-p"], names=["x"]))


if __name__ == "__main__":
    unittest.main()
