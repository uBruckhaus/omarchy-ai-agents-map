import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class ManualTest(unittest.TestCase):
    """The user manual Help opens: present, and acceptable to the marketplace's static scan (a file
    over 512 KiB is read only up to its first 4 KiB and passes as a non-executable binary only with
    a NUL byte there; docs/manual/build.py checks the same when it copies it in)."""

    def test_manual_present_and_scannable(self):
        path = next((p for p in (ROOT / "assets/user-manual.pdf", ROOT / "data/user-manual.pdf") if p.is_file()), None)
        self.assertIsNotNone(path, "no user-manual.pdf in assets/ or data/")
        data = path.read_bytes()
        self.assertTrue(data.startswith(b"%PDF-"))
        self.assertGreater(len(data), 512 * 1024)
        self.assertIn(b"\0", data[:4096])
        self.assertFalse(path.stat().st_mode & 0o111, "must not be executable")


if __name__ == "__main__":
    unittest.main()
