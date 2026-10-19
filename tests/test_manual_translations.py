import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "docs/manual/translations.py"


def load_tool():
    spec = importlib.util.spec_from_file_location("manual_translations", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(TOOL.is_file(), "docs/ is part of Asterism only (the plugin ships the built PDFs)")
class ManualTranslationsTest(unittest.TestCase):
    """The translated manuals keep the English markup and say which English they were made from.
    Text behind the English is not a failure here (an English edit must not break the tests):
    docs/manual/translations.py check and build.py name it."""

    def setUp(self):
        self.tool = load_tool()

    def test_markup_and_stamp_of_every_translation(self):
        for lang in self.tool.LANGUAGES:
            with self.subTest(lang=lang):
                _changed, _new, _removed, problems = self.tool.report(lang)
                self.assertEqual(problems, [])

    def test_a_changed_english_part_is_named(self):
        english = self.tool.blocks(self.tool.ENGLISH.read_text())
        edited = {k: v.replace("</p>", " (changed)</p>", 1) if k == "health" else v for k, v in english.items()}
        before = {k: self.tool.fingerprint(v) for k, v in english.items()}
        after = {k: self.tool.fingerprint(v) for k, v in edited.items()}
        self.assertEqual([k for k in before if before[k] != after[k]], ["health"])

    def test_whitespace_alone_is_no_change(self):
        self.assertEqual(self.tool.fingerprint("<p>a  b\n c</p>"), self.tool.fingerprint("<p>a b c</p>"))


if __name__ == "__main__":
    unittest.main()
