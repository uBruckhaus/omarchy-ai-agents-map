import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import i18n  # noqa: E402

SOURCES = sorted(p.name for p in Path(__file__).resolve().parent.parent.glob("*.py") if p.name != "i18n.py")


class I18nTest(unittest.TestCase):
    def msgids(self):
        """Every _("…") and N_("…") text, also one written over several lines (the parser joins them)."""
        import ast
        root = Path(__file__).resolve().parent.parent
        found = set()
        for name in SOURCES:
            for node in ast.walk(ast.parse((root / name).read_text())):
                if isinstance(node, ast.Call) and getattr(node.func, "id", None) in ("_", "N_") and node.args \
                        and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    found.add(node.args[0].value)
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


if __name__ == "__main__":
    unittest.main()
