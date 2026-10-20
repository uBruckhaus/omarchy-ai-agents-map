"""The Information page starts AI Playground only from a launcher of this user that no one else
may change, and passes it a plain agent command."""

import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import info_page  # noqa: E402


class LauncherTest(unittest.TestCase):
    def test_the_app_on_path(self):
        with tempfile.TemporaryDirectory() as folder:
            app = Path(folder, "ai-playground")
            app.write_text("#!/bin/sh\n")
            app.chmod(0o755)
            with unittest.mock.patch.dict(os.environ, {"PATH": folder}):
                self.assertEqual(info_page.playground_launcher(Path(folder, "nowhere")), app)
                app.chmod(0o777)
                self.assertIsNone(info_page.playground_launcher(Path(folder, "nowhere")))

    def test_only_an_own_unwritable_executable(self):
        with tempfile.TemporaryDirectory() as folder:
            launcher = Path(folder, "ai-playground")
            self.assertIsNone(info_page.playground_launcher(launcher, on_path=False))   # not installed
            launcher.write_text("#!/bin/sh\n")
            launcher.chmod(0o644)
            self.assertIsNone(info_page.playground_launcher(launcher, on_path=False))   # not executable
            launcher.chmod(0o755)
            self.assertEqual(info_page.playground_launcher(launcher, on_path=False), launcher)
            launcher.chmod(0o775)
            self.assertIsNone(info_page.playground_launcher(launcher, on_path=False))   # the group may change it
            link = Path(folder, "link")
            launcher.chmod(0o755)
            os.symlink(launcher, link)
            self.assertIsNone(info_page.playground_launcher(link, on_path=False))       # not a link

    def test_agent_argument(self):
        self.assertEqual(info_page.playground_argv(Path("/p/ai-playground"), "claude"),
                         ["/p/ai-playground", "--agent", "claude"])
        self.assertEqual(info_page.playground_argv(Path("/p/ai-playground"), "--evil x"), ["/p/ai-playground"])
        self.assertEqual(info_page.playground_argv(Path("/p/ai-playground")), ["/p/ai-playground"])


if __name__ == "__main__":
    unittest.main()
