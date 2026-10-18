import json
import os
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import agent_security  # noqa: E402
import connect  # noqa: E402
import disk_usage  # noqa: E402
from isolate import desktop_in  # noqa: E402

CODEX = """# my settings
model = "o3"

[tui]
screen_reader_detection_done = true

[projects."/home/u/p"]
trust_level = "trusted"
"""

HERMES = """model:
  default: x
terminal:
  backend: local
approvals:
  mode: smart
  timeout: 60
whatsapp:
  unauthorized_dm_behavior: ignore
"""


class EditorsTest(unittest.TestCase):
    def test_toml_top_level_goes_before_the_first_table(self):
        new = agent_security.toml_set(CODEX, "approval_policy", "untrusted")
        data = tomllib.loads(new)
        self.assertEqual(data["approval_policy"], "untrusted")
        self.assertEqual(data["projects"]["/home/u/p"]["trust_level"], "trusted")
        self.assertIn("# my settings", new)

    def test_toml_replaces_and_removes(self):
        text = agent_security.toml_set(CODEX, "model", "o4")
        self.assertEqual(tomllib.loads(text)["model"], "o4")
        self.assertNotIn("model", tomllib.loads(agent_security.toml_set(text, "model", None)))

    def test_toml_table_key(self):
        new = agent_security.toml_set(CODEX, "sandbox_workspace_write.network_access", True)
        self.assertIs(tomllib.loads(new)["sandbox_workspace_write"]["network_access"], True)
        again = agent_security.toml_set(new, "sandbox_workspace_write.network_access", False)
        self.assertIs(tomllib.loads(again)["sandbox_workspace_write"]["network_access"], False)
        self.assertEqual(again.count("[sandbox_workspace_write]"), 1)

    def test_yaml_changes_only_the_key(self):
        new = agent_security.yaml_set(HERMES, "approvals.mode", "off")
        self.assertIn('  mode: "off"\n', new)            # quoted: unquoted off reads as false
        self.assertIn("  timeout: 60\n", new)
        self.assertEqual(new.replace('  mode: "off"', "  mode: smart"), HERMES)
        self.assertEqual(agent_security._yaml_value(new, "approvals.mode"), "off")

    def test_yaml_new_block_and_key(self):
        new = agent_security.yaml_set(HERMES, "security.redact_secrets", False)
        self.assertIs(agent_security._yaml_value(new, "security.redact_secrets"), False)
        new = agent_security.yaml_set(HERMES, "terminal.cwd", ".")
        self.assertEqual(agent_security._yaml_value(new, "terminal.backend"), "local")
        self.assertEqual(agent_security._yaml_value(new, "terminal.cwd"), ".")

    def test_json_opencode_single_value_becomes_per_tool(self):
        text = json.dumps({"permission": "allow", "model": "m"})
        data = json.loads(agent_security._json_set(text, "permission.bash", "ask"))
        self.assertEqual(data["permission"], {"*": "allow", "bash": "ask"})

    def test_json_remove_drops_empty_parent(self):
        text = json.dumps({"permissions": {"disableBypassPermissionsMode": "disable"}, "x": 1})
        data = json.loads(agent_security._json_set(text, "permissions.disableBypassPermissionsMode", None))
        self.assertEqual(data, {"x": 1})


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        desktop_in(self, self.home)
        patcher = mock.patch.object(agent_security, "HOME", self.home)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, rel: str, text: str):
        path = self.home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def test_opencode_defaults_are_high_risk(self):
        report = agent_security.report_for("opencode")
        self.assertEqual(report.level(), "high")
        self.assertEqual(report.risks[0].fix_key, "permission.bash")

    def test_codex_defaults_are_sandboxed(self):
        report = agent_security.report_for("codex")
        self.assertEqual(report.level(), "low")
        self.assertNotIn(_("Works with your rights"), [r.title for r in report.risks])

    def test_codex_full_access_never_asking(self):
        self.write(".codex/config.toml", 'approval_policy = "never"\nsandbox_mode = "danger-full-access"\n')
        report = agent_security.report_for("codex")
        self.assertEqual(report.level(), "high")
        self.assertIn("sandbox_mode", [r.fix_key for r in report.risks])

    def test_wide_allow_rule_is_high(self):
        self.write(".claude/settings.json", json.dumps({"permissions": {"allow": ["Bash(*)", "Edit"]}}))
        report = agent_security.report_for("claude")
        self.assertEqual(report.level(), "high")
        self.assertEqual(report.rules[0][2], ["Bash(*)", "Edit"])

    def test_unknown_agent(self):
        report = agent_security.report_for("muse", 2)
        self.assertFalse(report.known)
        self.assertTrue(report.note)
        self.assertEqual([r.level for r in report.risks], ["low"])     # the MCP servers

    def test_plan_apply_undo(self):
        path = self.write(".hermes/config.yaml", HERMES)
        plan = agent_security.plan("hermes", "approvals.mode", "manual")
        self.assertIn('+  mode: "manual"', plan.diff())
        backup = connect.apply(plan, self.home / "backups")
        self.assertEqual(agent_security.report_for("hermes").settings[0].value(), "manual")
        connect.undo(backup)
        self.assertEqual(path.read_text(), HERMES)

    def test_plan_refuses_unknown_values_and_read_only_settings(self):
        with self.assertRaises(ValueError):
            agent_security.plan("codex", "sandbox_mode", "everything")
        with self.assertRaises(ValueError):
            agent_security.plan("hermes", "terminal.backend", "docker")

    def test_copilot_settings_file_is_created(self):
        plan = agent_security.plan("copilot", "defaultPermissionMode", "manual")
        self.assertEqual(plan.changes[0].path, self.home / ".copilot/settings.json")
        self.assertEqual(json.loads(plan.changes[0].new), {"defaultPermissionMode": "manual"})

    def test_runtime_on_all_interfaces(self):
        rt = SimpleNamespace(key="ollama", installed=True, address="0.0.0.0:11434")
        self.assertEqual(agent_security.runtime_report(rt).level(), "high")
        rt.address = "127.0.0.1:11434"
        self.assertEqual(agent_security.runtime_report(rt).risks[0].level, "ok")


def _(text):
    return text


class DiskTest(unittest.TestCase):
    def test_files_count_once(self):
        root = Path(tempfile.mkdtemp())
        (root / "models").mkdir()
        (root / "models/a.gguf").write_bytes(b"x" * 100_000)
        os.link(root / "models/a.gguf", root / "copy.gguf")
        (root / "links").mkdir()
        (root / "links/a.gguf").symlink_to(root / "models/a.gguf")
        items = [disk_usage.Item("models", "m", "m", [disk_usage.Place("models", root / "models")]),
                 disk_usage.Item("models", "l", "l", [disk_usage.Place("models", root / "links")]),
                 disk_usage.Item("agent", "x", "x", [disk_usage.Place("data", root)])]
        disks = disk_usage.measure(items)
        self.assertGreaterEqual(items[0].total(), 100_000)
        self.assertEqual(items[1].total(), 0)
        self.assertEqual(items[1].places[0].links, 1)
        self.assertEqual(items[2].places[0].files, 0)          # the hard link is the same file
        self.assertEqual(len(disks), 1)

    def test_program_folder(self):
        root = Path(tempfile.mkdtemp())
        (root / "app/v1/bin").mkdir(parents=True)
        (root / "app/v1/bin/tool").write_text("")
        (root / "app/current").symlink_to(root / "app/v1")
        self.assertEqual(disk_usage.program_folder(root / "app/current/bin/tool"), root / "app")

    def test_wrapper_script_target(self):
        root = Path(tempfile.mkdtemp())
        real = root / "real-server"
        real.write_text("")
        real.chmod(0o755)
        script = root / "wrapper"
        script.write_text(f"#!/bin/sh\nbinary={real}\nexec \"$binary\" \"$@\"\n")
        self.assertEqual(disk_usage.script_target(script), real)


if __name__ == "__main__":
    unittest.main()
