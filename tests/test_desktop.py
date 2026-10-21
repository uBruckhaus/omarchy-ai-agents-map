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
                 if line.startswith("    ") and line.split()[0][0].islower() and "(" in line.split()[0]]
        self.assertGreaterEqual(len(names), 20)
        for module in (desktop_omarchy, g):
            missing = [n for n in names if not hasattr(module, n)]
            self.assertEqual(missing, [], module.__name__)


class GenericTest(unittest.TestCase):
    def test_config_folder_is_asterism(self):
        self.assertEqual(g.CONFIG_DIR.name, "asterism")

    def test_theme_from_the_style_manager(self):
        with mock.patch.object(g, "_adw_style", return_value=(True, "#9141ac")):
            colors = g.theme_colors()
        self.assertEqual((colors["background"], colors["accent"]), (g.DARK["background"], "#9141ac"))

    def test_theme_follows_style_and_accent(self):
        """Without a display (no style manager): gsettings."""
        answers = {"color-scheme": "prefer-dark", "accent-color": "purple"}
        with mock.patch.object(g, "_adw_style", return_value=None), \
                mock.patch.object(g, "_gsetting", side_effect=lambda key: answers.get(key, "")):
            dark = g.theme_colors()
        self.assertEqual(dark["background"], g.DARK["background"])
        self.assertEqual(dark["accent"], g.ACCENTS["purple"])
        with mock.patch.object(g, "_adw_style", return_value=None), mock.patch.object(g, "_gsetting", return_value=""):
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

    def test_omarchy_floating_terminal_quotes_every_word(self):
        """The floating terminal runs its argument through bash: no word may become shell syntax."""
        hostile = ["agent", "x; touch /tmp/pwned", "$(id)", "`id`", "a b", "'q'", "\n"]
        with mock.patch.object(desktop_omarchy.shutil, "which", return_value="/usr/bin/float"), \
                mock.patch.object(desktop_omarchy.subprocess, "Popen") as popen:
            self.assertTrue(desktop_omarchy.run_in_terminal(hostile, {"K": "v w"}))
            self.assertEqual(shlex.split(popen.call_args[0][0][1]), ["env", "K=v w", *hostile])
            self.assertTrue(desktop_omarchy.run_steps_in_terminal([hostile, ["b", "&&", "c"]]))
            line = popen.call_args[0][0][1]
            self.assertEqual([shlex.split(part) for part in line.split(" && ")], [hostile, ["b", "&&", "c"]])

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
        with mock.patch.object(g, "_window", None), mock.patch.object(g, "_hyprland", return_value=None):
            self.assertFalse(g.maximize("x"))
            self.assertIsNone(g.is_maximized("x"))

    def test_hyprland_session_asks_the_compositor(self):
        # Hyprland ignores the window's own maximize request and calls every tile maximized.
        hypr = mock.Mock(maximize=mock.Mock(return_value=True), is_maximized=mock.Mock(return_value=False))
        window = mock.Mock(is_maximized=mock.Mock(return_value=True))
        with mock.patch.object(g, "_window", window), mock.patch.object(g, "_hyprland", return_value=hypr):
            self.assertTrue(g.maximize("x"))
            self.assertFalse(g.is_maximized("x"))           # the compositor's state, not GTK's
        hypr.maximize.assert_called_once_with("x")
        window.maximize.assert_not_called()

    def test_like_windows_follows_hyprland_rounding(self):
        # On Hyprland the windows' corners are its rounding (Omarchy: square), not libadwaita's.
        for rounding, expected in ((0, 0), (8, 8), (None, g.RADIUS)):
            hypr = mock.Mock(window_rounding=mock.Mock(return_value=rounding))
            with mock.patch.object(g, "_hyprland", return_value=hypr):
                self.assertEqual(g.window_rounding(), expected)
        with mock.patch.object(g, "_hyprland", return_value=None):
            self.assertEqual(g.window_rounding(), g.RADIUS)
            self.assertEqual(g.rounding_source(), "desktop")

    def test_hyprland_only_when_its_session_runs(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(g._hyprland())

    def test_no_usage_records_and_built_in_agents(self):
        import agents
        catalog = g.agent_catalog()
        self.assertEqual([c for c, *_rest in catalog], [c for c, *_rest in agents.FALLBACK])
        self.assertFalse([c for c, _l, _i, font in catalog if font == "omarchy"])
        self.assertEqual(g.default_agent(), "")

    def test_usage_records_only_where_omarchy_keeps_them(self):
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(g, "OMARCHY_USAGE", Path(tmp) / "omarchy/agents/usage"):
                self.assertIsNone(g.usage_dir())                  # another desktop: none
                self.assertFalse(g.refresh_usage("claude"))
                (Path(tmp) / "omarchy/agents/usage").mkdir(parents=True)
                self.assertEqual(g.usage_dir(), Path(tmp) / "omarchy/agents/usage")   # this PC runs Omarchy too
                self.assertFalse(g.has_usage_collector("../x"))


class LookTest(unittest.TestCase):
    """Settings › Look: Omarchy's theme (default) or GNOME's Adwaita, on Omarchy only."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.settings = Path(folder.name) / "settings.json"

    def test_default_and_values(self):
        import ai_core
        with mock.patch.object(desktop, "current", return_value=desktop_omarchy):
            self.assertEqual(ai_core.saved_look(self.settings), "omarchy")
            self.settings.write_text('{"look": "adwaita"}')
            self.assertEqual(ai_core.saved_look(self.settings), "adwaita")
            self.settings.write_text('{"look": "../x"}')
            self.assertEqual(ai_core.saved_look(self.settings), "omarchy")
        with mock.patch.object(desktop, "current", return_value=g):
            self.assertEqual(ai_core.saved_look(self.settings), "adwaita")

    def test_colours_follow_the_look(self):
        import ai_core
        with mock.patch.object(desktop, "current", return_value=desktop_omarchy), \
                mock.patch.object(desktop_omarchy, "theme_colors", return_value={"accent": "#ff8800"}), \
                mock.patch.object(g, "theme_colors", return_value={"accent": "#3584e4"}):
            with mock.patch.object(ai_core, "saved_look", return_value="omarchy"):
                self.assertEqual(ai_core.look_colors()["accent"], "#ff8800")
            with mock.patch.object(ai_core, "saved_look", return_value="adwaita"):
                self.assertEqual(ai_core.look_colors()["accent"], "#3584e4")


if __name__ == "__main__":
    unittest.main()
