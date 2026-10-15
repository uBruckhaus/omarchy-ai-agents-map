import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import connect  # noqa: E402
import skills_ctl  # noqa: E402

HERMES = "model:\n  default: x\nskills:\n  creation_nudge_interval: 15\n  external_dirs:\n    - {shared}\nkanban:\n  a: 1\n"


class SkillsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name).resolve()
        for module in (connect, skills_ctl):
            patcher = mock.patch.object(module, "HOME", self.home)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.shared = self.home / ".agents/skills"
        self.extra = self.home / "extra-skills"
        for folder, names in ((self.shared, ["json", "python"]), (self.extra, ["omaraw", "photo"])):
            for name in names:
                (folder / name).mkdir(parents=True)
                (folder / name / "SKILL.md").write_text("---\nname: x\n---\n")
        (self.home / ".pi/agent").mkdir(parents=True)
        (self.home / ".pi/agent/settings.json").write_text(json.dumps({"skills": [str(self.shared)], "theme": "x"}))
        (self.home / ".hermes").mkdir()
        (self.home / ".hermes/config.yaml").write_text(HERMES.format(shared=self.shared))

    def tearDown(self):
        self.tmp.cleanup()

    def apply(self, agent, folder, remove=False):
        result = skills_ctl.plan(agent, str(folder), remove=remove)
        return result, connect.apply(result, self.home / "backups")

    def test_links_for_claude_and_undo(self):
        self.assertFalse(skills_ctl.is_connected("claude", self.extra))
        result, backup = self.apply("claude", self.extra)
        own = self.home / ".claude/skills"
        self.assertEqual(sorted(p.name for p in own.iterdir()), ["omaraw", "photo"])
        self.assertTrue(skills_ctl.is_connected("claude", self.extra))
        self.assertIn("+ ~/.claude/skills/omaraw → ~/extra-skills/omaraw", result.diff())
        connect.undo(backup)
        self.assertEqual(list(own.iterdir()), [])
        self.apply("claude", self.extra)
        removed, _backup = self.apply("claude", self.extra, remove=True)
        self.assertEqual(len(removed.links_remove), 2)
        self.assertFalse(skills_ctl.is_connected("claude", self.extra))

    def test_settings_lists(self):
        self.apply("pi", self.extra)
        self.assertEqual(json.loads((self.home / ".pi/agent/settings.json").read_text())["skills"],
                         [str(self.shared), str(self.extra)])
        self.apply("hermes", self.extra)
        import yaml
        data = yaml.safe_load((self.home / ".hermes/config.yaml").read_text())
        self.assertEqual(data["skills"]["external_dirs"], [str(self.shared), str(self.extra)])
        self.assertEqual(data["kanban"], {"a": 1})
        self.assertTrue(skills_ctl.is_connected("hermes", self.extra))
        self.apply("hermes", self.extra, remove=True)
        self.apply("hermes", self.shared, remove=True)
        data = yaml.safe_load((self.home / ".hermes/config.yaml").read_text())
        self.assertEqual(data["skills"], {"creation_nudge_interval": 15})

    def test_shared_folder_stays_for_agents_that_read_it(self):
        result = skills_ctl.plan("codex", str(self.shared), remove=True)
        self.assertTrue(result.empty())
        self.assertEqual(result.notes, ["~/.agents/skills"])
        # Pi: removed from its settings list, but Pi reads the folder anyway.
        result = skills_ctl.plan("pi", str(self.shared), remove=True)
        self.assertEqual(len(result.changes), 1)
        self.assertEqual(result.notes, ["~/.agents/skills"])

    def test_already_linked_name_is_skipped(self):
        own = self.home / ".claude/skills"
        (own / "photo").mkdir(parents=True)
        result = skills_ctl.plan("claude", str(self.extra))
        self.assertEqual([link.name for link, _t in result.links_add], ["omaraw"])


if __name__ == "__main__":
    unittest.main()
