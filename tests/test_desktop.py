import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import desktop  # noqa: E402
import desktop_generic as g  # noqa: E402
import desktop_omarchy  # noqa: E402


class DetectTest(unittest.TestCase):
    def test_override_and_omarchy_folder(self):
        self.assertEqual(desktop.detect({"ASTERISM_DESKTOP": "generic"}), "generic")
        self.assertEqual(desktop.detect({"ASTERISM_DESKTOP": "omarchy", "OMARCHY_PATH": "/nowhere"}), "omarchy")
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(desktop.detect({"OMARCHY_PATH": tmp}), "omarchy")
        self.assertEqual(desktop.detect({"OMARCHY_PATH": "/nowhere/omarchy"}), "generic")
        self.assertEqual(desktop.detect({"ASTERISM_DESKTOP": "kde", "OMARCHY_PATH": "/nowhere"}), "generic")

    def test_both_implement_the_whole_interface(self):
        names = [line.split()[0].split("(")[0] for line in desktop.__doc__.split("The interface")[1].splitlines()
                 if line.startswith("    ") and line.split()[0][0].islower()]
        self.assertGreaterEqual(len(names), 20)
        for module in (desktop_omarchy, g):
            missing = [n for n in names if not hasattr(module, n)]
            self.assertEqual(missing, [], module.__name__)


class GenericTest(unittest.TestCase):
    def test_config_folder_is_asterism(self):
        self.assertEqual(g.CONFIG_DIR.name, "asterism")

    def test_theme_follows_style_and_accent(self):
        answers = {"color-scheme": "prefer-dark", "accent-color": "purple"}
        with mock.patch.object(g, "_gsetting", side_effect=lambda key: answers.get(key, "")):
            dark = g.theme_colors()
        self.assertEqual(dark["background"], g.DARK["background"])
        self.assertEqual(dark["accent"], g.ACCENTS["purple"])
        with mock.patch.object(g, "_gsetting", return_value=""):
            light = g.theme_colors()
        self.assertEqual((light["background"], light["accent"]), (g.LIGHT["background"], g.ACCENTS["blue"]))

    def test_terminal_choice(self):
        found = {"konsole": "/usr/bin/konsole", "xterm": "/usr/bin/xterm"}
        with mock.patch.object(g.shutil, "which", side_effect=found.get), mock.patch.dict(g.os.environ, {"TERMINAL": ""}):
            self.assertEqual(g.terminal(), ["/usr/bin/konsole", "-e"])
        with mock.patch.object(g.shutil, "which", side_effect=lambda n: "/usr/bin/" + n if n in ("foot", "kitty") else None), \
                mock.patch.dict(g.os.environ, {"TERMINAL": "kitty"}):
            self.assertEqual(g.terminal(), ["/usr/bin/kitty"])
        with mock.patch.object(g.shutil, "which", return_value=None):
            self.assertIsNone(g.terminal())
            self.assertFalse(g.open_terminal(["x"], "/"))

    def test_run_in_terminal_quotes_every_word(self):
        with mock.patch.object(g, "terminal", return_value=["/usr/bin/kgx", "--"]), \
                mock.patch.object(g.subprocess, "Popen") as popen:
            self.assertTrue(g.run_in_terminal(["mise", "upgrade", "http:muse"], {"MISE_USE_VERSIONS_HOST": "0"}))
        argv = popen.call_args[0][0]
        self.assertEqual(argv[:4], ["/usr/bin/kgx", "--", "bash", "-c"])
        self.assertTrue(argv[4].startswith("env MISE_USE_VERSIONS_HOST=0 mise upgrade http:muse; "))
        self.assertEqual(shlex.split(argv[4].split(";")[0]), ["env", "MISE_USE_VERSIONS_HOST=0", "mise", "upgrade", "http:muse"])

    def test_install_command_per_package_manager(self):
        for manager, expected in (("apt", "sudo apt install gir1.2-vte-3.91"), ("dnf", "sudo dnf install vte291-gtk4"),
                                  ("zypper", "sudo zypper install typelib-1_0-Vte-3_91"), ("pacman", "sudo pacman -S vte4")):
            with mock.patch.object(g, "package_manager", return_value=manager):
                self.assertEqual(g.install_command("vte4"), expected)
        with mock.patch.object(g, "package_manager", return_value="apt"):
            self.assertEqual(g.install_command("xdg-terminal-exec"), "")
        with mock.patch.object(g, "package_manager", return_value=""):
            self.assertEqual(g.install_command("vte4"), "")

    def test_windows_without_an_attached_window(self):
        with mock.patch.object(g, "_window", None):
            self.assertFalse(g.maximize("x"))
            self.assertIsNone(g.is_maximized("x"))

    def test_no_usage_records_and_built_in_agents(self):
        self.assertIsNone(g.agent_catalog())
        self.assertEqual(g.default_agent(), "")
        self.assertIsNone(g.usage_dir())
        self.assertFalse(g.refresh_usage("claude"))


if __name__ == "__main__":
    unittest.main()
