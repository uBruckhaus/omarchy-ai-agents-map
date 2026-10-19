import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import agents  # noqa: E402

MENU = '''{
  "setup.default.agent": {"icon":"x","label":"Agent","title":"Default Agent"},
  "setup.default.agent.claude": {"icon":"\\udb81\\udec4","label":"Claude","checked":"x","action":"y"},
  "setup.default.agent.codex": {"icon":"\\ue905","iconFont":"omarchy","label":"Codex","action":"y"},
  "setup.default.agent.cursor-agent": {"icon":"\\ue90d","iconFont":"omarchy","label":"Cursor CLI"},
  "setup.default.agent.muse": {"icon":"m","label":"Muse Code"},
  "setup.default.agent.openclaw": {"icon":"o","iconFont":"omarchy","label":"OpenClaw"},
  "setup.default.agent.pi": {"icon":"p","iconFont":"omarchy","label":"Pi"},
  "setup.default.browser": {"icon":"b","label":"Browser"}
}
'''
WRAPPER = '#!/bin/bash\nexport MISE_MINIMUM_RELEASE_AGE=0\nmise use -g --quiet "{spec}" || exit 1\nexec mise x "{spec}" -- "{cmd}" "$@"\n'


class AgentsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.menu = root / "menu.jsonc"
        self.menu.write_text(MENU)
        self.agent_file = root / "agent"
        self.bin = root / "bin"
        self.mise = root / "mise"
        self.db = root / "pacman"
        for d in (self.bin, self.mise / "installs", self.db):
            d.mkdir(parents=True)
        for cmd, spec in (("claude", "claude"), ("codex", "codex"), ("cursor-agent", "cursor-agent"),
                          ("muse", "http:muse[url=https://x,bin=muse]")):
            (self.bin / cmd).write_text(WRAPPER.format(spec=spec, cmd=cmd))

    def tearDown(self):
        self.tmp.cleanup()

    def install(self, folder, version):
        path = self.mise / "installs" / folder
        (path / version).mkdir(parents=True)
        os.symlink(f"./{version}", path / "latest")

    def listed(self):
        return {a.command: a for a in agents.list_agents(self.menu, self.agent_file, self.bin, self.mise, self.db)}

    def test_catalog_from_menu(self):
        found = agents.catalog(self.menu)
        self.assertEqual([c for c, *_ in found], ["claude", "codex", "cursor-agent", "muse", "openclaw", "pi"])
        self.assertEqual(found[1][3], "omarchy")
        self.assertEqual(found[0][3], "")

    def test_fallback_without_menu(self):
        self.assertEqual(agents.catalog(self.menu.with_name("missing")), agents.FALLBACK)

    def test_wrapper_alone_is_not_installed(self):
        self.assertFalse(self.listed()["claude"].installed)

    def test_mise_install_with_version(self):
        self.install("claude", "2.1.300")
        self.install("http-muse", "1.4.8")
        found = self.listed()
        self.assertTrue(found["claude"].installed)
        self.assertEqual(found["claude"].version, "2.1.300")
        self.assertEqual(found["muse"].version, "1.4.8")
        self.assertEqual(found["claude"].source, "mise")

    def test_own_install_symlink(self):
        (self.bin / "cursor-agent").unlink()
        target = self.bin / "real-cursor"
        target.write_text("#!/bin/sh\n")
        os.symlink(target, self.bin / "cursor-agent")
        found = self.listed()["cursor-agent"]
        self.assertTrue(found.installed)
        self.assertEqual(found.source, "own")

    def test_openclaw_package(self):
        (self.db / "openclaw-desktop-1.0-1").mkdir()
        self.assertFalse(self.listed()["openclaw"].installed)
        (self.db / "openclaw-2026.10.1-2").mkdir()
        found = self.listed()["openclaw"]
        self.assertTrue(found.installed)
        self.assertEqual(found.version, "2026.10.1")

    def test_order_installed_first_default_leading(self):
        self.install("claude", "1")
        self.install("codex", "1")
        self.agent_file.write_text("codex\n")
        order = [a.command for a in agents.list_agents(self.menu, self.agent_file, self.bin, self.mise, self.db)]
        self.assertEqual(order[:2], ["codex", "claude"])
        self.assertTrue(self.listed()["codex"].default)

    def test_unsafe_default_agent_ignored(self):
        self.agent_file.write_text("../evil\n")
        self.assertEqual(agents.default_agent(self.agent_file), "")

    def test_install_dir_never_leaves_installs(self):
        for name in agents.install_dir_candidates("npm:../../etc") + agents.install_dir_candidates(".."):
            self.assertNotIn("/", name)
            self.assertNotIn("..", name)
            self.assertNotEqual(name, ".")



class MiseLeftoverTest(unittest.TestCase):
    def test_only_links_left_is_not_installed(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "installs" / "opencode"
            folder.mkdir(parents=True)
            (folder / "latest").symlink_to(folder / "2.0.32")           # mise uninstall --all leaves this
            self.assertIsNone(agents.mise_install("opencode", Path(tmp)))
            (folder / "2.0.32").mkdir()
            self.assertEqual(agents.mise_install("opencode", Path(tmp)), folder)


if __name__ == "__main__":
    unittest.main()
