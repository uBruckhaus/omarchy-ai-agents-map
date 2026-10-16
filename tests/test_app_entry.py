import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app_entry  # noqa: E402


class AppEntryTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.here = self.root / "plugin"
        self.here.mkdir()
        self.path = self.root / "applications" / f"{app_entry.APP_ID}.desktop"

    def test_write_update_remove(self):
        self.assertTrue(app_entry.ensure_entry(True, self.path, self.here))
        text = self.path.read_text()
        self.assertIn(f"TryExec={self.here}/ai-agents-map-toggle", text)
        self.assertIn(f"StartupWMClass={app_entry.APP_ID}", text)
        self.assertEqual(oct(self.path.stat().st_mode & 0o777), "0o644")
        self.assertFalse(app_entry.ensure_entry(True, self.path, self.here))     # unchanged: not rewritten
        self.assertTrue(app_entry.ensure_entry(False, self.path, self.here))
        self.assertFalse(self.path.exists())

    def test_never_touches_a_file_it_did_not_write(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("[Desktop Entry]\nName=Mine\n")
        self.assertFalse(app_entry.ensure_entry(True, self.path, self.here))
        self.assertFalse(app_entry.ensure_entry(False, self.path, self.here))
        self.assertEqual(self.path.read_text(), "[Desktop Entry]\nName=Mine\n")

    def test_symlink_left_alone(self):
        self.path.parent.mkdir(parents=True)
        target = self.root / "elsewhere"
        target.write_text("x")
        os.symlink(target, self.path)
        self.assertFalse(app_entry.ensure_entry(True, self.path, self.here))
        self.assertEqual(target.read_text(), "x")

    def test_folder_names_a_desktop_entry_cannot_hold(self):
        odd = self.root / "a b$x"
        odd.mkdir()
        self.assertFalse(app_entry.ensure_entry(True, self.path, odd))
        self.assertFalse(self.path.exists())


class WidenTest(unittest.TestCase):
    def test_shares_workspace(self):
        own = {"class": app_entry.APP_ID, "pid": os.getpid(), "workspace": {"id": 1}}
        other = {"class": "foot", "pid": 1, "workspace": {"id": 1}}
        elsewhere = {"class": "foot", "pid": 2, "workspace": {"id": 3}}
        self.assertIs(app_entry.own_window([other, own]), own)
        self.assertTrue(app_entry.shares_workspace([own, other], own))
        self.assertFalse(app_entry.shares_workspace([own, elsewhere], own))
        stranger = dict(own, pid=os.getpid() + 1)          # another copy of the app is not ours
        self.assertIsNone(app_entry.own_window([stranger]))

    def test_widen_moves_or_maximizes(self):
        own = {"class": app_entry.APP_ID, "pid": os.getpid(), "workspace": {"id": 1}, "floating": False}
        calls = []
        with unittest.mock.patch.object(app_entry, "_dispatch", lambda lua: calls.append(lua) or True):
            with unittest.mock.patch.object(app_entry, "_clients", lambda: [own, {"class": "foot", "workspace": {"id": 1}}]):
                app_entry.widen()
            with unittest.mock.patch.object(app_entry, "_clients", lambda: [own]):
                app_entry.widen()
        self.assertIn('workspace = "emptym"', calls[0])
        self.assertIn('mode = "maximized"', calls[1])


if __name__ == "__main__":
    unittest.main()
