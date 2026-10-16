import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import isolate  # noqa: E402
import connect  # noqa: E402
import mcp_ctl  # noqa: E402
import mcp_library  # noqa: E402

ENTRY = {"transport": "stdio", "command": "/opt/x/x-cli", "args": ["mcp"], "env": {"KEY": "k-1"},
         "url": "", "headers": {}}


class LibraryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        isolate.desktop_in(self, self.home)
        for module in (connect, mcp_ctl, mcp_library):
            patcher = mock.patch.object(module, "HOME", self.home)
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def test_stays_listed_and_comes_back(self):
        connect.apply(mcp_ctl.plan("pi", "x", entry=ENTRY), self.home / "b")
        mcp_library.remember({"x": mcp_ctl.normal(mcp_ctl.raw_entries("pi")["x"])})
        self.assertEqual(mcp_library.library_file().stat().st_mode & 0o777, 0o600)
        # The last agent loses it: still listed, and a new line sets it up from the list.
        connect.apply(mcp_ctl.plan("pi", "x", remove=True), self.home / "b")
        mcp_library.remember({})
        self.assertIn("x", mcp_library.load())
        again = mcp_ctl.plan("gemini", "x", sources=[])
        connect.apply(again, self.home / "b")
        self.assertEqual(mcp_ctl.normal(mcp_ctl.raw_entries("gemini")["x"])["env"], {"KEY": "k-1"})

    def test_remove_for_good_and_undo(self):
        connect.apply(mcp_ctl.plan("pi", "x", entry=ENTRY), self.home / "b")
        mcp_library.remember({"x": ENTRY})
        listed = connect.Plan("", "", "x")
        listed.changes = [mcp_library.change("x", None)]
        gone = connect.merge([mcp_ctl.plan("pi", "x", remove=True), listed])
        backup = connect.apply(gone, self.home / "b")
        self.assertEqual(mcp_library.load(), {})
        self.assertNotIn("x", mcp_ctl.raw_entries("pi"))
        connect.undo(backup)
        self.assertIn("x", mcp_library.load())
        self.assertIn("x", mcp_ctl.raw_entries("pi"))


if __name__ == "__main__":
    unittest.main()
