import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class ManualTest(unittest.TestCase):
    """The English user manual Help opens: present, and acceptable to the marketplace's static scan (a file
    over 512 KiB is read only up to its first 4 KiB and passes as a non-executable binary only with
    a NUL byte there; docs/manual/build.py checks the same when it copies it in)."""

    def test_manual_present_and_scannable(self):
        folder = next((f for f in (ROOT / "assets", ROOT / "data") if (f / "user-manual.pdf").is_file()), None)
        self.assertIsNotNone(folder, "no user-manual.pdf in assets/ or data/")
        path = folder / "user-manual.pdf"
        data = path.read_bytes()
        self.assertTrue(data.startswith(b"%PDF-"))
        self.assertGreater(len(data), 512 * 1024)
        self.assertIn(b"\0", data[:4096])
        self.assertEqual(path.stat().st_mode & 0o777, 0o644)

    def test_no_translated_manuals_remain(self):
        for folder in (ROOT / "assets", ROOT / "data"):
            with self.subTest(folder=folder):
                self.assertEqual(list(folder.glob("user-manual.*.pdf")), [])
        self.assertEqual(list((ROOT / "docs/manual").glob("manual.*.html")), [])

    @unittest.skipUnless(shutil.which("pdftotext"), "pdftotext is not available")
    def test_bundled_pdf_documents_english_only_settings(self):
        result = subprocess.run(["pdftotext", str(ROOT / "assets/user-manual.pdf"), "-"],
                                check=True, capture_output=True, text=True, timeout=30)
        # pdftotext breaks table cells over lines: compare with whitespace joined.
        text = " ".join(result.stdout.split())
        self.assertTrue("Language and manual" in text,
                        "bundled PDF is missing the English-only settings hint")
        self.assertFalse("in your language" in text,
                         "bundled PDF still describes translated manuals")


class EnglishManualBuildTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location("manual_build", ROOT / "docs/manual/build.py")
        cls.builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.builder)

    def test_only_english_can_be_built(self):
        self.assertEqual(self.builder.LANGUAGES, ("en",))
        for lang in ("de", "es", "fr", "it", "nl", "pl", "pt"):
            with self.subTest(lang=lang), self.assertRaises(ValueError):
                self.builder.build(lang, None, None, "unused-chromium", "unused-pdfcraft")

    def test_english_source_renders_numbered_contents_without_translations(self):
        source = (ROOT / "docs/manual/manual.html").read_text()
        chapters = self.builder.outline(source)
        self.assertTrue(chapters)
        rendered = self.builder.render_html(source, chapters, {}, {"PLUGIN_VERSION": "1.0.2", "DATE": "November 2026"})
        self.assertIn('lang="en"', rendered)
        self.assertIn('class="chapter-label">Chapter 1', rendered)
        self.assertIn('class="toc-chapter"', rendered)
        self.assertIn("Language and manual", rendered)
        self.assertNotIn("<!-- TOC -->", rendered)
        self.assertNotIn("{{PLUGIN_VERSION}}", rendered)


if __name__ == "__main__":
    unittest.main()
