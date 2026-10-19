import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import ai_core  # noqa: E402
import settings_schema as schema  # noqa: E402

READ = re.compile(r'(?<![\w.])(?:ai_core\.)?setting\("([a-z_]+)"')
WRITE = re.compile(r'(?:save_setting|update_settings)\(([^)]*)\)')
KEYWORD = re.compile(r'(?:^|[(,]\s*)([a-z_]+)=')


class SchemaTest(unittest.TestCase):
    """Every key the app's settings.json holds is declared once (settings_schema.SCHEMA)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "settings.json"

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, data: dict):
        self.path.write_text(json.dumps(data))

    def test_the_code_reads_and_writes_only_declared_keys(self):
        problems = []
        for path in sorted(ROOT.glob("*.py")):
            text = path.read_text()
            for key in READ.findall(text):
                if key not in schema.SCHEMA:
                    problems.append(f"{path.name} reads {key}")
            for args in WRITE.findall(text):
                for key in KEYWORD.findall(args):
                    if key != "path" and key not in schema.SCHEMA:
                        problems.append(f"{path.name} writes {key}")
        self.assertEqual(problems, [])

    def test_one_way_to_read_the_settings(self):
        # ai_core.setting() checks the shape; a direct .get() would bring back its own defaults and checks.
        direct = [p.name for p in ROOT.glob("*.py")
                  if "ai_core.load_settings().get(" in p.read_text()]
        self.assertEqual(direct, [])

    def test_a_value_of_the_wrong_shape_counts_as_not_set(self):
        self.write({"map_zoom": "big", "terminal_tabs": "yes", "window_size": [800], "map_animations": "wild"})
        self.assertEqual(ai_core.setting("map_zoom", self.path), 1.0)
        self.assertIs(ai_core.setting("terminal_tabs", self.path), True)
        self.assertIsNone(ai_core.setting("window_size", self.path))
        self.assertEqual(ai_core.setting("map_animations", self.path), "all")

    def test_stored_values_win(self):
        self.write({"map_zoom": 1.5, "terminal_tabs": False, "window_size": [1200, 800]})
        self.assertEqual(ai_core.setting("map_zoom", self.path), 1.5)
        self.assertIs(ai_core.setting("terminal_tabs", self.path), False)
        self.assertEqual(ai_core.setting("window_size", self.path), [1200, 800])

    def test_defaults_are_fresh_copies(self):
        first = ai_core.setting("details_open", self.path)
        first.append("changed")
        self.assertEqual(ai_core.setting("details_open", self.path), [])

    def test_an_undeclared_key_is_refused(self):
        with self.assertRaises(KeyError):
            ai_core.update_settings(self.path, termnal_tabs=True)      # a typo
        self.assertFalse(self.path.exists())

    def test_migration_renames_and_drops(self):
        self.write({"animate_lines": False, "hidden_nodes": ["x"], "sidebar_width": 360, "map_zoom": 1.2})
        ai_core.migrate_settings(self.path)
        data = json.loads(self.path.read_text())
        self.assertEqual(data, {"map_animations": "off", "map_zoom": 1.2})

    def test_migration_keeps_a_newer_key(self):
        self.write({"animate_lines": False, "map_animations": "agents"})
        ai_core.migrate_settings(self.path)
        self.assertEqual(json.loads(self.path.read_text()), {"map_animations": "agents"})

    def test_every_default_has_its_own_shape(self):
        for key, setting in schema.SCHEMA.items():
            if setting.default is not None:
                self.assertTrue(setting.valid(setting.default), key)


if __name__ == "__main__":
    unittest.main()
