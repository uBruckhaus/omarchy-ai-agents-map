import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class CompileTest(unittest.TestCase):
    def test_every_module_compiles(self):
        """The popup is not imported by the other tests: a syntax error must still fail here."""
        for path in sorted(ROOT.glob("*.py")):
            with self.subTest(path.name):
                compile(path.read_text(encoding="utf-8"), str(path), "exec")


if __name__ == "__main__":
    unittest.main()
