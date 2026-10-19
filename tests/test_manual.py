import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

LANGUAGES = ("en", "de", "es", "fr", "it", "nl", "pl", "pt")   # one manual per app language


class ManualTest(unittest.TestCase):
    """The user manuals Help opens (one per language): present, and acceptable to the marketplace's static scan (a file
    over 512 KiB is read only up to its first 4 KiB and passes as a non-executable binary only with
    a NUL byte there; docs/manual/build.py checks the same when it copies it in)."""

    def test_manual_present_and_scannable(self):
        folder = next((f for f in (ROOT / "assets", ROOT / "data") if (f / "user-manual.pdf").is_file()), None)
        self.assertIsNotNone(folder, "no user-manual.pdf in assets/ or data/")
        for lang in LANGUAGES:
            path = folder / ("user-manual.pdf" if lang == "en" else f"user-manual.{lang}.pdf")
            with self.subTest(lang=lang):
                self.assertTrue(path.is_file(), f"no {path.name}")
                data = path.read_bytes()
                self.assertTrue(data.startswith(b"%PDF-"))
                self.assertGreater(len(data), 512 * 1024)
                self.assertIn(b"\0", data[:4096])
                self.assertFalse(path.stat().st_mode & 0o111, "must not be executable")

if __name__ == "__main__":
    unittest.main()
