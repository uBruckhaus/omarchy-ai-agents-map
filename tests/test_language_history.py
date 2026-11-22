"""Shared settings saves preserve history, including upgrades with legacy language choices."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ai_core
from isolate import desktop_in


class LanguageHistoryTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name)
        self.path = self.home / "settings.json"
        desktop_in(self, self.home)

    def test_settings_changes_preserve_history_larger_than_the_old_64k_reader(self):
        history = {f"test-agent-{agent}": [f"/project/{i}/" + "long folder name " * 6 for i in range(50)]
                   for agent in range(14)}
        self.path.write_text(json.dumps({"language": "de", "agent_project_history": history,
                                         "map_layers": ["agents"]}))
        self.assertGreater(self.path.stat().st_size, 64 * 1024)
        ai_core.update_settings(self.path, map_animations="off")
        saved = ai_core.load_settings(self.path)
        self.assertEqual(saved["agent_project_history"], history)
        self.assertEqual(saved["map_layers"], ["agents"])
        self.assertEqual(saved["map_animations"], "off")
        self.assertEqual(saved["language"], "de")
        ai_core.update_settings(self.path, map_animations=None)
        saved = ai_core.load_settings(self.path)
        self.assertNotIn("map_animations", saved)
        self.assertEqual(saved["agent_project_history"], history)

    def test_invalid_setting_key_does_not_rewrite_settings(self):
        self.path.write_text('{"language": "de"}')
        before = self.path.read_bytes()
        with self.assertRaises(KeyError):
            ai_core.update_settings(self.path, unknown_setting=True)
        self.assertEqual(self.path.read_bytes(), before)

    def test_explicit_settings_path_does_not_write_the_default(self):
        with mock.patch.object(ai_core, "settings_file", return_value=self.home / "default-settings.json"):
            ai_core.update_settings(self.path, map_layers=["agents"])
        self.assertEqual(ai_core.load_settings(self.path)["map_layers"], ["agents"])
        self.assertFalse((self.home / "default-settings.json").exists())

    def test_old_history_is_trimmed_without_preventing_settings_saves(self):
        history = {"fixture": [f"/project/{i}" for i in range(700)]}
        self.path.write_text(json.dumps({"agent_project_history": history, "language": "de"}))
        ai_core.update_settings(self.path, map_animations="off")
        self.assertEqual(ai_core.load_settings(self.path)["agent_project_history"]["fixture"], history["fixture"][:50])
