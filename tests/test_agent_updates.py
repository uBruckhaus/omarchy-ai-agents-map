import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import agent_updates as au  # noqa: E402
import agents  # noqa: E402
import install_ctl  # noqa: E402
import netfetch  # noqa: E402

MISE_OUTDATED = {
    "cursor-agent": {"name": "cursor-agent", "current": "2026.10.14-941492e", "latest": "2026.10.16-e3cb207"},
    "http:muse": {"name": "http:muse", "current": "1.4.9-R6297.1", "latest": "1.4.10-R6427.1"},
    "node": {"name": "node", "current": "24.1.0", "latest": "24.2.0"},          # not an agent
    "evil;rm": {"current": "1", "latest": "2"},                                    # not a tool name
    "codex": {"current": "0.1.0", "latest": "0.2.0 && curl x"},                    # not a version
}


def agent(command, version, source, installed=True):
    return agents.Agent(command, command.title(), "", installed=installed, version=version, source=source)


class FakeInstaller:
    can_install = True
    builds = (("vulkan", "Vulkan"), ("rocm", "ROCm"))

    def __init__(self, current, folder=True):
        self.current = current
        self.folder = folder

    def installed_folder(self):
        return Path("/x") if self.folder else None

    def current_version(self):
        return self.current

    def latest(self, gpu, build=""):
        return install_ctl.Release(f"b7000-{build}" if build else "b7000")


class AgentUpdatesTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.dir = Path(folder.name)
        self.cache = self.dir / "updates.json"

    def run_mise(self, data=MISE_OUTDATED):
        return lambda argv, **kw: (json.dumps(data).encode(), argv == ["mise", "outdated", "--json"])

    def test_newer(self):
        self.assertTrue(au.newer("2.1.302", "2.1.301"))
        self.assertTrue(au.newer("2.10.0", "2.9.9"))
        self.assertFalse(au.newer("2.1.301", "2.1.301"))
        self.assertFalse(au.newer("<script>", "2.1.301"))

    def test_claude_channel_from_its_settings(self):
        settings = self.dir / "settings.json"
        self.assertEqual(au.channel(settings), "latest")
        settings.write_text(json.dumps({"autoUpdatesChannel": "stable"}))
        self.assertEqual(au.channel(settings), "stable")
        settings.write_text(json.dumps({"autoUpdatesChannel": "../evil"}))
        self.assertEqual(au.channel(settings), "latest")

    def test_claude_channel_answer_checked(self):
        self.assertEqual(au.claude_channel_latest("latest", mock.Mock(return_value={"latest": "2.1.302"})), "2.1.302")
        for answer in ({"latest": "1.0; rm -rf ~"}, ["2.1.302"], {}):
            self.assertIsNone(au.claude_channel_latest("latest", mock.Mock(return_value=answer)))
        self.assertIsNone(au.claude_channel_latest("latest", mock.Mock(side_effect=netfetch.FetchError("x"))))

    def test_mise_outdated_keeps_only_clean_entries(self):
        with mock.patch.object(au.shutil, "which", return_value="/usr/bin/mise"):
            found = au.mise_outdated(self.run_mise())
        self.assertEqual(set(found), {"cursor-agent", "http:muse", "node"})
        self.assertEqual(au.mise_command_of("http:muse"), "muse")
        self.assertEqual(au.mise_command_of("aqua:anthropics/claude-code"), "claude-code")

    def test_check_mise_agents(self):
        agent_list = [agent("cursor-agent", "2026.10.14-941492e", "mise"), agent("muse", "1.4.9-R6297.1", "mise"),
                      agent("codex", "0.1.0", "mise"), agent("hermes", "1.0", "package")]
        with mock.patch.object(au.shutil, "which", return_value="/usr/bin/mise"):
            found = au.check(agent_list, [], now=1000, cache=self.cache, sources={"mise": lambda: au.mise_outdated(self.run_mise())})
        self.assertEqual([(u.key, u.available, u.command) for u in found],
                         [("cursor-agent", "2026.10.16-e3cb207", ["mise", "upgrade", "cursor-agent"]),
                          ("muse", "1.4.10-R6427.1", ["mise", "upgrade", "http:muse"])])
        self.assertEqual(self.cache.stat().st_mode & 0o777, 0o600)

    def test_check_is_cached_and_drops_what_was_installed_since(self):
        mise = mock.Mock(return_value={"cursor-agent": ("1.0", "2.0")})
        cursor = agent("cursor-agent", "1.0", "mise")
        au.check([cursor], [], now=1000, cache=self.cache, sources={"mise": mise})
        self.assertEqual(len(au.check([cursor], [], now=2000, cache=self.cache, sources={"mise": mise})), 1)
        self.assertEqual(mise.call_count, 1)
        cursor.version = "2.0"                      # updated in the terminal
        self.assertEqual(au.check([cursor], [], now=3000, cache=self.cache, sources={"mise": mise}), [])
        au.check([cursor], [], now=1000 + au.CHECK_EVERY, cache=self.cache, sources={"mise": mise})
        self.assertEqual(mise.call_count, 2)

    def test_claude_own_installer(self):
        claude = agent("claude", "2.1.301", "own")
        with mock.patch.object(au.shutil, "which", return_value="/home/u/.local/bin/claude"), \
                mock.patch.object(au, "channel", return_value="latest"):
            found = au.check([claude], [], now=1, cache=self.cache, sources={"claude": lambda chan: "2.1.302"})
        self.assertEqual([(u.name, u.current, u.available, u.command) for u in found],
                         [("Claude", "2.1.301", "2.1.302", ["claude", "update"])])

    def test_claude_from_mise_follows_its_channel_when_mise_lags(self):
        claude = agent("claude", "2.1.301", "mise")
        with mock.patch.object(au, "channel", return_value="latest"):
            found = au.check([claude], [], now=1, cache=self.cache,
                             sources={"mise": lambda: {}, "claude": lambda chan: "2.1.302"})
        self.assertEqual([(u.available, u.command) for u in found], [("2.1.302", ["mise", "upgrade", "claude"])])

    def test_mise_upgrade_reads_the_tool_releases(self):
        with mock.patch.object(au.subprocess, "Popen") as popen, \
                mock.patch.object(au.shutil, "which", side_effect=lambda n: None if n.startswith("omarchy") else f"/usr/bin/{n}"):
            self.assertTrue(au.run_in_terminal(["mise", "upgrade", "claude"]))
        self.assertEqual(popen.call_args[0][0][1:], ["env", "MISE_USE_VERSIONS_HOST=0", "mise", "upgrade", "claude"])

    def test_runtimes_we_installed_open_the_update_page(self):
        rt = types.SimpleNamespace(key="llamacpp", name="llama.cpp", installed=True)
        other = types.SimpleNamespace(key="ollama", name="Ollama", installed=True)
        fakes = {"llamacpp": FakeInstaller("b6500-vulkan"), "ollama": FakeInstaller("0.9", folder=False)}
        with mock.patch.object(install_ctl, "installer", side_effect=fakes.get):
            found = au.check([], [rt, other], now=1, cache=self.cache,
                             sources={"gpu": lambda: None})
        self.assertEqual([(u.key, u.current, u.available, u.page) for u in found],
                         [("llamacpp", "b6500-vulkan", "b7000-vulkan", True)])

    def test_cache_cannot_smuggle_a_command(self):
        self.cache.write_text(json.dumps({"checked": 1, "updates": [
            {"kind": "agent", "key": "x", "name": "X", "current": "1", "available": "2",
             "command": ["bash", "-c", "evil"], "page": False}]}))
        self.assertEqual(au.check([agent("x", "1", "mise")], [], now=2, cache=self.cache), [])

    def test_run_in_terminal_only_update_commands(self):
        with mock.patch.object(au.subprocess, "Popen") as popen, \
                mock.patch.object(au.shutil, "which", return_value="/usr/bin/omarchy-launch-floating-terminal-with-presentation"):
            self.assertFalse(au.run_in_terminal(["bash", "-c", "x"]))
            self.assertFalse(au.run_in_terminal(["mise", "upgrade", "a;rm"]))
            popen.assert_not_called()
            self.assertTrue(au.run_in_terminal(["mise", "upgrade", "http:muse"]))
        self.assertEqual(popen.call_args[0][0][1], "MISE_USE_VERSIONS_HOST=0 mise upgrade http:muse")

    def test_checking_runs_nothing_but_mise_outdated(self):
        with mock.patch.object(au.subprocess, "Popen") as popen, mock.patch.object(au.shutil, "which", return_value="/x"):
            au.check([agent("cursor-agent", "1.0", "mise")], [], now=1, cache=self.cache,
                     sources={"mise": lambda: {"cursor-agent": ("1.0", "2.0")}})
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
