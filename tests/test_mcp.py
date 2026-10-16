import json
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import connect  # noqa: E402
import mcp_ctl  # noqa: E402

PI = {"mcpServers": {
    "photocraft": {"command": "/home/u/.local/bin/photocraft-cli", "args": ["mcp", "--root", "/home/u/Pictures"]},
    "search": {"url": "https://mcp.example.com/mcp", "headers": {"Authorization": "Bearer s3cret-token"}},
    "tools": {"command": "npx", "args": ["-y", "tools-mcp"], "env": {"API_KEY": "abcd-1234"}},
}}
HERMES = "model:\n  default: x\nagent:\n  y: 1\n"


class McpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        for module in (connect, mcp_ctl):
            patcher = mock.patch.object(module, "HOME", self.home)
            patcher.start()
            self.addCleanup(patcher.stop)
        (self.home / ".pi/agent").mkdir(parents=True)
        (self.home / ".pi/agent/mcp.json").write_text(json.dumps(PI))
        (self.home / ".hermes").mkdir()
        (self.home / ".hermes/config.yaml").write_text(HERMES)

    def tearDown(self):
        self.tmp.cleanup()

    def add(self, agent, name):
        result = mcp_ctl.plan(agent, name, ["pi"])
        connect.apply(result, self.home / "backups")
        return result

    def test_every_format_round_trips(self):
        for agent in ("hermes", "codex", "opencode", "gemini", "copilot", "cursor-agent", "crush", "grok", "omp",
                      "muse", "openclaw"):
            for name in ("photocraft", "search", "tools"):
                self.add(agent, name)
                self.assertEqual(mcp_ctl.normal(mcp_ctl.raw_entries(agent)[name]),
                                 mcp_ctl.normal(PI["mcpServers"][name]), (agent, name))

    def test_files_stay_valid_and_other_settings_stay(self):
        for name in ("photocraft", "tools"):
            self.add("hermes", name)
            self.add("codex", name)
        import yaml
        data = yaml.safe_load((self.home / ".hermes/config.yaml").read_text())
        self.assertEqual(data["model"], {"default": "x"})
        self.assertEqual(set(data["mcp_servers"]), {"photocraft", "tools"})
        codex = tomllib.loads((self.home / ".codex/config.toml").read_text())
        self.assertEqual(codex["mcp_servers"]["tools"]["env"], {"API_KEY": "abcd-1234"})

    def test_remove_and_undo(self):
        self.add("hermes", "photocraft")
        self.add("hermes", "tools")
        removed = mcp_ctl.plan("hermes", "photocraft", remove=True)
        backup = connect.apply(removed, self.home / "backups")
        self.assertEqual(set(mcp_ctl.raw_entries("hermes")), {"tools"})
        connect.undo(backup)
        self.assertEqual(set(mcp_ctl.raw_entries("hermes")), {"photocraft", "tools"})
        connect.apply(mcp_ctl.plan("hermes", "photocraft", remove=True), self.home / "backups")
        connect.apply(mcp_ctl.plan("hermes", "tools", remove=True), self.home / "backups")
        self.assertNotIn("mcp_servers", (self.home / ".hermes/config.yaml").read_text())
        self.assertEqual((self.home / ".hermes/config.yaml").read_text(), HERMES)

    def test_secrets_not_in_preview(self):
        for agent in ("hermes", "codex", "claude"):
            text = mcp_ctl.plan(agent, "search", ["pi"]).diff() + mcp_ctl.plan(agent, "tools", ["pi"]).diff()
            self.assertNotIn("s3cret-token", text)
            self.assertNotIn("abcd-1234", text)

    def test_secrets_never_land_in_a_readable_file(self):
        """A 0644 destination in a traversable folder must not expose copied secrets."""
        import os
        import stat
        for rel in (".hermes/config.yaml", ".pi/agent/mcp.json"):
            os.chmod(self.home / rel, 0o644)
        os.chmod(self.home / ".hermes", 0o755)
        self.add("hermes", "tools")
        self.add("hermes", "search")
        cfg = self.home / ".hermes/config.yaml"
        self.assertIn("abcd-1234", cfg.read_text())
        self.assertEqual(stat.S_IMODE(cfg.stat().st_mode), 0o600)
        (self.home / ".codex").mkdir(mode=0o755)
        (self.home / ".codex/config.toml").write_text("")
        os.chmod(self.home / ".codex/config.toml", 0o664)
        backup = connect.apply(mcp_ctl.plan("codex", "search", ["pi"]), self.home / "backups")
        self.assertEqual(stat.S_IMODE((self.home / ".codex/config.toml").stat().st_mode), 0o600)
        connect.undo(backup)                                 # Undo does not loosen it again
        self.assertEqual(stat.S_IMODE((self.home / ".codex/config.toml").stat().st_mode), 0o600)
        for agent in ("opencode", "gemini", "cursor-agent"):  # new files: owner-only too
            result = self.add(agent, "tools")
            for change in result.changes:
                self.assertEqual(stat.S_IMODE(change.path.stat().st_mode), 0o600, change.path)

    def test_claude_through_its_cli(self):
        result = mcp_ctl.plan("claude", "photocraft", ["pi"])
        self.assertEqual(result.changes, [])
        self.assertEqual(result.commands[-1][1:6], ["mcp", "add-json", "--scope", "user", "photocraft"])
        self.assertEqual(json.loads(result.commands[-1][6])["type"], "stdio")
        self.assertEqual(result.undo_commands[0][1:], ["mcp", "remove", "--scope", "user", "photocraft"])
        with mock.patch.object(connect, "_run") as run:
            backup = connect.apply(result, self.home / "backups")
            connect.undo(backup)
        self.assertEqual([c.args[0] for c in run.call_args_list], result.commands + result.undo_commands)

    def test_muse_gets_schema_version_and_openclaw_json5_refused(self):
        self.add("muse", "tools")
        data = json.loads((self.home / ".config/muse/settings.json").read_text())
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(data["mcp_servers"]["tools"]["transport"], "stdio")
        (self.home / ".openclaw").mkdir()
        (self.home / ".openclaw/openclaw.json").write_text("{ // json5\n gateway: {} }")
        with self.assertRaises(ValueError):
            mcp_ctl.plan("openclaw", "tools", ["pi"])

    def test_claude_keys_never_in_argv(self):
        result = mcp_ctl.plan("claude", "tools", ["pi"])      # env API_KEY
        self.assertEqual(result.commands, [])
        self.assertNotIn("abcd-1234", json.dumps(result.commands + result.undo_commands))
        connect.apply(result, self.home / "b")
        data = json.loads((self.home / ".claude.json").read_text())
        self.assertEqual(data["mcpServers"]["tools"]["env"], {"API_KEY": "abcd-1234"})
        removed = mcp_ctl.plan("claude", "tools", remove=True)
        self.assertEqual(removed.commands, [])
        self.assertNotIn("abcd-1234", removed.diff())

    def test_unknown_server_or_agent(self):
        with self.assertRaises(ValueError):
            mcp_ctl.plan("hermes", "nothere", ["pi"])
        with self.assertRaises(ValueError):
            mcp_ctl.plan("someagent", "photocraft", ["pi"])


if __name__ == "__main__":
    unittest.main()
