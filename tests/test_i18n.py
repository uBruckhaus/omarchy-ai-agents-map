import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import i18n  # noqa: E402

SOURCES = sorted(p.name for p in Path(__file__).resolve().parent.parent.glob("*.py") if p.name != "i18n.py")


class I18nTest(unittest.TestCase):
    def msgids(self):
        root = Path(__file__).resolve().parent.parent
        found = set()
        for name in SOURCES:
            found |= set(re.findall(r'_\("((?:[^"\\]|\\.)*)"\)', (root / name).read_text()))
        return found

    def test_every_msgid_translated(self):
        for code, _name in i18n.LANGUAGES:
            if code == "en":
                continue
            missing = self.msgids() - set(i18n.STRINGS[code])
            self.assertFalse(missing, (code, missing))

    def test_placeholders_match(self):
        for code, table in i18n.STRINGS.items():
            for key, value in table.items():
                if isinstance(value, list):   # plural forms: the singular's placeholders and the count
                    allowed = set(re.findall(r"\{\d+\}", key)) | {"{0}"}
                    for form in value:
                        extra = set(re.findall(r"\{\d+\}", form)) - allowed
                        self.assertLessEqual(len(extra), 1, (code, key))
                    continue
                self.assertEqual(sorted(re.findall(r"\{\d+\}", key)), sorted(re.findall(r"\{\d+\}", value)),
                                 (code, key))


class RenameTest(unittest.TestCase):
    def tearDown(self):
        i18n.set_name("AI Agents Map", "AI Agents Map")

    def test_texts_say_the_other_name(self):
        i18n.set_name("AI Agents Map", "Asterism")
        self.assertEqual(i18n._("AI Agents Map restarts in the new language."), "Asterism restarts in the new language.")
        self.assertNotIn("AI Agents Map", i18n.ngettext("1 more you could set up", "{0} more you could set up", 2))
        i18n.set_name("AI Agents Map", "AI Agents Map")
        self.assertEqual(i18n._("AI Agents Map restarts in the new language."), "AI Agents Map restarts in the new language.")


if __name__ == "__main__":
    unittest.main()
