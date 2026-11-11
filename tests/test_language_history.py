import json
import tempfile
import unittest
import subprocess
import sys
from pathlib import Path
from unittest import mock

import ai_core
import i18n


class LanguageHistoryTest(unittest.TestCase):
    def test_language_changes_preserve_history_larger_than_the_old_64k_reader(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'settings.json'
            history = {f'test-agent-{agent}': [f'/project/{i}/' + 'long folder name ' * 6 for i in range(50)] for agent in range(14)}
            ai_core.update_settings(path, language='de', agent_project_history=history, map_layers=['agents'])
            self.assertGreater(path.stat().st_size, 64 * 1024)
            with mock.patch.object(i18n, 'SETTINGS_FILE', path):
                self.assertEqual(i18n.language_setting(), 'de')
                i18n.save_language('fr')
                saved = json.loads(path.read_text())
                self.assertEqual(saved['language'], 'fr')
                self.assertEqual(saved['agent_project_history'], history)
                self.assertEqual(saved['map_layers'], ['agents'])
                i18n.save_language('auto')
                self.assertEqual(i18n.language_setting(), 'auto')
                self.assertEqual(json.loads(path.read_text())['agent_project_history'], history)

    def test_invalid_language_does_not_rewrite_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'settings.json'
            ai_core.update_settings(path, language='de')
            before = path.read_bytes()
            with mock.patch.object(i18n, 'SETTINGS_FILE', path):
                i18n.save_language('unknown')
                i18n.save_language([])
            self.assertEqual(path.read_bytes(), before)

    def test_standalone_import_preserves_an_explicit_settings_path(self):
        with tempfile.TemporaryDirectory() as directory:
            script = '''import json, sys
from pathlib import Path
import identity
root = Path(sys.argv[1])
identity.config_dir = lambda: root / 'default-config'
import i18n
chosen = root / 'chosen' / 'settings.json'
i18n.SETTINGS_FILE = chosen
i18n.save_language('de')
assert i18n.SETTINGS_FILE == chosen
assert i18n.language_setting() == 'de'
i18n.save_language('fr')
assert json.loads(chosen.read_text())['language'] == 'fr'
assert not (root / 'default-config' / 'settings.json').exists()
'''
            result = subprocess.run([sys.executable, '-c', script, directory],
                cwd=Path(__file__).resolve().parent.parent, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_old_history_is_trimmed_without_preventing_settings_saves(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'settings.json'
            history = {'fixture': [f'/project/{i}' for i in range(700)]}
            path.write_text(json.dumps({'agent_project_history':history}))
            ai_core.update_settings(path, language='de')
            self.assertEqual(ai_core.load_settings(path)['agent_project_history']['fixture'], history['fixture'][:50])
