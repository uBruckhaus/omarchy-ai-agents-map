import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app_entry  # noqa: E402
import desktop_omarchy  # noqa: E402


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
    """The Omarchy desktop's window actions (desktop_omarchy), for our app id."""

    def test_shares_workspace(self):
        own = {"class": app_entry.APP_ID, "pid": os.getpid(), "workspace": {"id": 1}}
        other = {"class": "foot", "pid": 1, "workspace": {"id": 1}}
        elsewhere = {"class": "foot", "pid": 2, "workspace": {"id": 3}}
        self.assertIs(desktop_omarchy.own_window([other, own], app_entry.APP_ID), own)
        self.assertTrue(desktop_omarchy.shares_workspace([own, other], own))
        self.assertFalse(desktop_omarchy.shares_workspace([own, elsewhere], own))
        stranger = dict(own, pid=os.getpid() + 1)          # another copy of the app is not ours
        self.assertIsNone(desktop_omarchy.own_window([stranger], app_entry.APP_ID))

    def test_widen_moves_or_maximizes(self):
        own = {"class": app_entry.APP_ID, "pid": os.getpid(), "workspace": {"id": 1}, "floating": False}
        calls = []
        with unittest.mock.patch.object(desktop_omarchy, "_dispatch", lambda lua: calls.append(lua) or True):
            with unittest.mock.patch.object(desktop_omarchy, "_clients",
                                            lambda: [own, {"class": "foot", "workspace": {"id": 1}}]):
                desktop_omarchy.widen(app_entry.APP_ID)
            with unittest.mock.patch.object(desktop_omarchy, "_clients", lambda: [own]):
                desktop_omarchy.widen(app_entry.APP_ID)
        self.assertIn('workspace = "emptym"', calls[0])
        self.assertIn('mode = "maximized"', calls[1])
        self.assertIn("class:^(org[.]omarchy[.]aiagentsmap)$", calls[1])

    def test_app_id_is_checked_before_it_reaches_a_dispatcher(self):
        with unittest.mock.patch.object(desktop_omarchy, "_dispatch") as dispatch:
            with self.assertRaises(ValueError):
                desktop_omarchy.maximize('x" }) hl.exec("touch /tmp/x')
        dispatch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
