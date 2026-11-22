import builtins
import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import i18n
from isolate import desktop_in


class I18nTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name)
        desktop_in(self, self.home)

    def test_only_english_is_available(self):
        self.assertEqual(i18n.LANGUAGES, [("en", "English")])
        self.assertEqual(i18n.CODES, {"en"})
        self.assertEqual(i18n.STRINGS, {})
        self.assertEqual(i18n.language_name("en"), "English")
        self.assertEqual(i18n.language_name("unknown"), "unknown")

    def test_locale_and_legacy_settings_never_change_english(self):
        settings = self.home / "settings.json"
        settings.write_text('{"language": "de"}')
        with mock.patch.object(i18n, "SETTINGS_FILE", settings):
            for locale in ("de_DE.UTF-8", "fr_FR.UTF-8", "pl_PL", "C", ""):
                with self.subTest(locale=locale), mock.patch.dict(os.environ, {
                    "LANG": locale, "LC_ALL": locale, "LC_MESSAGES": locale,
                }):
                    self.assertEqual(i18n.current(), "en")
                    self.assertEqual(i18n.system_language(), "en")
                    self.assertEqual(i18n.language_setting(), "en")
                    self.assertEqual(i18n._("Settings"), "Settings")

    def test_text_and_format_placeholders_pass_through(self):
        for text in ("Settings", "Opens {0}", "", "A & B <C>"):
            self.assertEqual(i18n._(text), text)
            self.assertEqual(i18n.N_(text), text)
        self.assertEqual(i18n._("Opens {0}").format("manual"), "Opens manual")

    def test_plurals_use_english_even_with_legacy_tables(self):
        for n in (0, 1, 2, 5, 12, 21):
            expected = "one" if n == 1 else "other"
            self.assertEqual(i18n.ngettext("one", "other", n), expected)
            self.assertEqual(i18n.pick_plural("pl", ["jeden", "kilka", "wiele"], n, "one", "other"), expected)

    def test_saving_any_language_is_a_no_op(self):
        settings = self.home / "settings.json"
        settings.write_text('{"language": "de", "map_layers": ["agents"]}')
        before = settings.read_bytes()
        with mock.patch.object(i18n, "SETTINGS_FILE", settings):
            for code in ("de", "fr", "en", "auto", "unknown", [], None):
                i18n.save_language(code)
            self.assertEqual(settings.read_bytes(), before)
            settings.unlink()
            i18n.save_language("en")
            self.assertFalse(settings.exists())

    def test_import_and_api_do_not_read_files(self):
        with mock.patch.object(builtins, "open", side_effect=AssertionError("unexpected file read")), \
                mock.patch.object(Path, "open", side_effect=AssertionError("unexpected file read")):
            importlib.reload(i18n)
            self.assertEqual(i18n.current(), "en")
            self.assertEqual(i18n.language_setting(), "en")
            i18n.save_language("de")


if __name__ == "__main__":
    unittest.main()
