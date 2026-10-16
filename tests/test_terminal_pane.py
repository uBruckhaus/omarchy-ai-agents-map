import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import terminal_pane as tp  # noqa: E402


class TerminalPaneTest(unittest.TestCase):
    def test_gui_apps_are_not_started_in_a_terminal(self):
        self.assertIsNone(tp.program("bionic"))
        with mock.patch.object(tp.shutil, "which", lambda name: f"/usr/bin/{name}"):
            self.assertEqual(tp.program("claude"), "/usr/bin/claude")

    def test_child_env(self):
        env = dict(item.split("=", 1) for item in tp.child_env({"HOME": "/h", "AI_AGENTS_MAP_PAGE": "agent:x",
                                                                 "BAD": "a\0b", "TERM": "dumb"}))
        self.assertEqual(env["TERM"], "xterm-256color")
        self.assertEqual(env["COLORTERM"], "truecolor")
        self.assertNotIn("AI_AGENTS_MAP_PAGE", env)
        self.assertNotIn("BAD", env)

    def test_theme_colors(self):
        path = Path(tempfile.mkdtemp()) / "colors.toml"
        path.write_text('background = "#101010"\nred = "#ff0000"\nname = "not a colour"\n')
        self.assertEqual(tp.theme_colors(path), {"background": "#101010", "red": "#ff0000"})
        self.assertEqual(tp.theme_colors(path.with_name("missing.toml")), {})

    def test_outside_is_an_argv(self):
        calls = []
        with mock.patch.object(tp.shutil, "which", lambda name: "/usr/bin/xdg-terminal-exec"), \
                mock.patch.object(tp.subprocess, "Popen", lambda argv, **kw: calls.append((argv, kw))):
            self.assertTrue(tp.open_outside("/usr/bin/claude", "/home/u/project; rm -rf ~"))
        argv, kw = calls[0]
        self.assertEqual(argv, ["/usr/bin/xdg-terminal-exec", "/usr/bin/claude"])
        self.assertEqual(kw["cwd"], "/home/u/project; rm -rf ~")      # a folder, never shell text
        self.assertNotIn("shell", kw)


if __name__ == "__main__":
    unittest.main()
