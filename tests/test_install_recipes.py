import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import harnesses  # noqa: E402
import install_recipes as r  # noqa: E402
import providers  # noqa: E402
from harnesses import Way  # noqa: E402


def which(*present):
    return lambda name: f"/usr/bin/{name}" if name in present else None


HERMES = (Way("command", argv=("omarchy-install-hermes-cli", "--now"), sudo=True),
          Way("package", packages=(("pacman", "hermes-desktop"), ("apt", "hermes"))),
          Way("manual", url="https://example.org"))


class ChooseTest(unittest.TestCase):
    def test_mise_first_when_there(self):
        with mock.patch.object(r.shutil, "which", which("mise")):
            step = r.choose((Way("mise", spec="opencode"),))
        self.assertEqual((step.kind, step.run, step.commands, step.sudo),
                         ("mise", [["mise", "--yes", "use", "-g", "opencode"]], ["mise --yes use -g opencode"], False))

    def test_without_mise_the_commands_to_copy(self):
        with mock.patch.object(r.shutil, "which", which()):
            step = r.choose((Way("mise", spec="opencode"),))
        self.assertEqual((step.kind, step.run, step.commands), ("commands", None, ["mise --yes use -g opencode"]))

    def test_desktop_installer_with_sudo(self):
        with mock.patch.object(r.shutil, "which", which("omarchy-install-hermes-cli", "pacman")):
            step = r.choose(HERMES)
        self.assertEqual((step.kind, step.run, step.sudo), ("command", [["omarchy-install-hermes-cli", "--now"]], True))

    def test_package_for_this_systems_manager(self):
        with mock.patch.object(r.shutil, "which", which("apt")):
            step = r.choose(HERMES)
        self.assertEqual((step.run, step.sudo), ([["sudo", "apt", "install", "hermes"]], True))

    def test_uninstall_matches_how_it_was_installed(self):
        with mock.patch.object(r.shutil, "which", which("omarchy-install-hermes-cli", "pacman", "mise")):
            self.assertEqual(r.choose(HERMES, "uninstall", "package").run, [["sudo", "pacman", "-R", "hermes-desktop"]])
            # unuse alone keeps versions something else still tracks: uninstall --all removes them.
            self.assertEqual(r.choose((Way("mise", spec="opencode"),), "uninstall", "mise").run,
                             [["mise", "--yes", "unuse", "-g", "opencode"], ["mise", "--yes", "uninstall", "--all", "opencode"]])
            self.assertTrue(r.choose((Way("installer"),), "uninstall", "own").installer)
            # Installed by mise, but the recipe only knows the package: nothing removes it from here.
            self.assertEqual(r.choose(HERMES, "uninstall", "mise").kind, "manual")

    def test_only_plain_words_are_started(self):
        spec = "http:muse[url=https://x.example/l.sh,bin=muse]"
        with mock.patch.object(r.shutil, "which", which("mise")):
            step = r.choose((Way("mise", spec=spec),))
        self.assertIsNone(step.run)                                   # brackets: shown, never run
        self.assertEqual(step.commands, [f"mise --yes use -g '{spec}'"])     # quoted for copying
        with mock.patch.object(r.shutil, "which", which("x;rm")):
            step = r.choose((Way("command", argv=("x;rm", "-rf")),))
        self.assertIsNone(step.run)                                   # a strange program name: shown only
        self.assertEqual(step.commands, ["'x;rm' -rf"])

    def test_run_starts_only_the_shown_argv(self):
        import desktop
        with mock.patch.object(desktop, "run_steps_in_terminal", return_value=True) as started:
            self.assertTrue(r.run(r.Step("mise", ["mise use -g pi"], [["mise", "use", "-g", "pi"]])))
            self.assertFalse(r.run(r.Step("commands", ["x"])))
        started.assert_called_once_with([["mise", "use", "-g", "pi"]])


class RecipesTest(unittest.TestCase):
    def test_every_built_in_has_a_recipe_and_safe_data_paths(self):
        for h in harnesses.all_harnesses():
            self.assertTrue(h.install, h.command)
            self.assertTrue(all(p.startswith("~/") and ".." not in p for p in h.data), h.command)
        for p in providers.local():
            self.assertTrue(p.install, p.key)

    def test_this_pcs_agents(self):
        self.assertEqual(harnesses.get("opencode").install[0], Way("mise", spec="opencode"))
        self.assertEqual(harnesses.get("hermes").install[0].argv, ("omarchy-install-hermes-cli", "--now"))
        self.assertEqual(harnesses.get("hermes").data, ("~/.hermes",))


if __name__ == "__main__":
    unittest.main()
