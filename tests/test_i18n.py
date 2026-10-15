import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import i18n  # noqa: E402

SOURCES = ["ai_popup.py", "ai_core.py"]


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
                self.assertEqual(sorted(re.findall(r"\{\d+\}", key)), sorted(re.findall(r"\{\d+\}", value)),
                                 (code, key))


if __name__ == "__main__":
    unittest.main()
