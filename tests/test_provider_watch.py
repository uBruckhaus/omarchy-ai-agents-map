import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import provider_watch as pw  # noqa: E402


class FakeRuntime:
    def __init__(self, key, service, address, agents, installed=True, active=None):
        self.key, self.service, self.address, self.agents, self.installed = key, service, address, agents, installed
        self.active_agents = agents if active is None else active


class FakeInventory:
    def __init__(self, runtimes):
        self.runtimes = runtimes


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "providers-with-agents.json"

    def tearDown(self):
        self.dir.cleanup()

    def test_write_and_read(self):
        inv = FakeInventory([FakeRuntime("llamacpp", "llama-server", "127.0.0.1:8080", ["opencode", "pi"]),
                             FakeRuntime("ollama", "ollama", "127.0.0.1:11434", []),
                             FakeRuntime("vllm", "", "127.0.0.1:8000", ["pi"])])
        self.assertTrue(pw.write_config(inv, self.path))
        self.assertFalse(pw.write_config(inv, self.path))       # unchanged
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        agents, runtimes = pw.read_config(self.path)
        self.assertEqual(agents, {"opencode": ["llamacpp"], "pi": ["llamacpp"]})
        self.assertEqual(runtimes, {"llamacpp": {"service": "llama-server", "port": 8080}})

    def test_only_the_provider_in_use_starts(self):
        inv = FakeInventory([FakeRuntime("llamacpp", "llama-server", "127.0.0.1:8080", ["opencode"], active=[]),
                             FakeRuntime("ollama", "ollama", "127.0.0.1:11434", ["opencode"], active=["opencode"])])
        pw.write_config(inv, self.path)
        agents, runtimes = pw.read_config(self.path)
        self.assertEqual(agents, {"opencode": ["ollama"]})
        self.assertEqual(set(runtimes), {"llamacpp", "ollama"})      # both known, so both can be stopped

    def test_bad_entries_dropped(self):
        self.path.write_text(json.dumps({
            "agents": {"opencode": ["llamacpp"], "../x": ["llamacpp"], "pi": ["nope"]},
            "runtimes": {"llamacpp": {"service": "llama; rm -rf ~", "port": 8080},
                         "ollama": {"service": "ollama", "port": "x"}, "../evil": {"service": "a"},
                         "kobold": {"service": "k", "port": 5001, "stays_up": True,
                                    "unload": {"path": "http://elsewhere/x", "body": {}}}}}))
        agents, runtimes = pw.read_config(self.path)
        # Any well-formed provider key is accepted (local LLMs from add-ons too); a bad unload request
        # means it is simply stopped.
        self.assertEqual(runtimes, {"ollama": {"service": "ollama", "port": 0}, "kobold": {"service": "k", "port": 5001}})
        self.assertEqual(agents, {})

    def test_a_controller_only_by_name(self):
        """Without a user service, only a controller the watcher has (never a command line)."""
        self.path.write_text(json.dumps({"agents": {}, "runtimes": {
            "lmstudio": {"service": "", "port": 1234, "control": "lms"},
            "other": {"service": "", "port": 1, "control": "sh -c reboot"},
            "plain": {"service": "", "port": 2}}}))
        _agents, runtimes = pw.read_config(self.path)
        self.assertEqual(runtimes, {"lmstudio": {"service": "", "port": 1234, "control": "lms"}})

    def test_the_engine_names_the_controller(self):
        inv = FakeInventory([FakeRuntime("lmstudio", "", "127.0.0.1:1234", ["hermes"])])
        pw.write_config(inv, self.path)
        _agents, runtimes = pw.read_config(self.path)
        self.assertEqual(runtimes["lmstudio"]["control"], "lms")


class WatcherTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "c.json"
        self.path.write_text(json.dumps({"agents": {"opencode": ["llamacpp"], "pi": ["llamacpp", "ollama"]},
                                         "runtimes": {"llamacpp": {"service": "llama-server", "port": 8080},
                                                      "ollama": {"service": "ollama", "port": 11434}}}))
        self.now, self.running, self.busy, self.calls = 0.0, set(), set(), []
        self.watcher = pw.Watcher(self.path, runner=lambda argv: self.calls.append(argv[-2:]) or True,
                                  clock=lambda: self.now, agents_now=lambda _names: (set(self.running), set()),
                                  busy=lambda port, _ignore: port in self.busy, state=None)

    def tearDown(self):
        self.dir.cleanup()

    def step(self, seconds=1.0):
        self.now += seconds
        self.watcher.tick()

    def test_start_with_agent_stop_after_last_closes(self):
        self.step()
        self.assertEqual(self.calls, [])
        self.running = {"opencode"}
        self.step()
        self.assertEqual(self.calls, [["start", "llama-server.service"]])
        self.step()
        self.assertEqual(len(self.calls), 1)          # once, not every tick
        self.running = {"opencode", "pi"}
        self.step()
        self.assertEqual(self.calls[-1], ["start", "ollama.service"])
        self.running = {"pi"}
        for _ in range(10):
            self.step()
        self.assertNotIn(["stop", "llama-server.service"], self.calls)   # pi still uses it
        self.running = set()
        self.step(1)
        self.assertFalse(any(c[0] == "stop" for c in self.calls))       # grace time
        self.step(pw.GRACE + 1)
        self.assertIn(["stop", "llama-server.service"], self.calls)
        self.assertIn(["stop", "ollama.service"], self.calls)

    def test_restart_within_grace_keeps_it(self):
        self.running = {"opencode"}
        self.step()
        self.running = set()
        self.step()
        self.running = {"opencode"}
        self.step(pw.GRACE + 1)
        self.running = {"opencode"}
        self.step()
        self.assertEqual([c[0] for c in self.calls], ["start", "start"])   # started again, never stopped

    def test_not_stopped_while_another_program_uses_it(self):
        self.running = {"opencode"}
        self.step()
        self.running = set()
        self.busy = {8080}
        self.step()
        self.step(pw.GRACE + 1)
        self.step()
        self.assertNotIn(["stop", "llama-server.service"], self.calls)
        self.busy = set()
        self.step()
        self.assertIn(["stop", "llama-server.service"], self.calls)


class StateTest(unittest.TestCase):
    """What the watcher must still stop survives a restart and losing the last connection."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        root = Path(self.dir.name)
        self.config, self.state = root / "c.json", root / "state.json"
        self.config.write_text(json.dumps({"agents": {"opencode": ["ollama"]},
                                           "runtimes": {"ollama": {"service": "ollama", "port": 11434}}}))
        self.now, self.running, self.calls = 0.0, set(), []

    def tearDown(self):
        self.dir.cleanup()

    def watcher(self):
        return pw.Watcher(self.config, runner=lambda argv: self.calls.append(argv[-2:]) or True,
                          clock=lambda: self.now, agents_now=lambda names: ({a for a in self.running if a in names},
                                                                           set()),
                          busy=lambda _p, _i: False, state=self.state)

    def test_last_connection_removed_still_stops(self):
        watcher = self.watcher()
        self.running = {"opencode"}
        watcher.tick()
        self.config.write_text(json.dumps({"agents": {}, "runtimes": {}}))   # disconnected; OpenCode still open
        self.now += 1
        watcher.tick()
        self.now += pw.GRACE + 1
        watcher.tick()
        self.assertEqual(self.calls, [["start", "ollama.service"], ["stop", "ollama.service"]])

    def test_restart_stops_what_was_left(self):
        watcher = self.watcher()
        self.running = {"opencode"}
        watcher.tick()
        self.assertEqual(json.loads(self.state.read_text())["pending"], {"ollama": {"service": "ollama", "port": 11434}})
        del watcher                                  # killed, no shutdown (crash, power)
        self.running = set()
        again = self.watcher()
        again.tick()
        self.now += pw.GRACE + 1
        again.tick()
        self.assertEqual(self.calls, [["start", "ollama.service"], ["stop", "ollama.service"]])
        self.assertEqual(json.loads(self.state.read_text())["pending"], {})

    def test_restart_while_agent_open_keeps_it(self):
        watcher = self.watcher()
        self.running = {"opencode"}
        watcher.tick()
        watcher.shutdown()                           # plugin update restarts the service
        again = self.watcher()
        again.tick()
        self.now += pw.GRACE + 1
        again.tick()
        self.assertEqual(self.calls, [["start", "ollama.service"]])   # not stopped, not started twice

    def test_shutdown_after_last_connection_stops_at_once(self):
        watcher = self.watcher()
        self.running = {"opencode"}
        watcher.tick()
        self.config.write_text(json.dumps({"agents": {}, "runtimes": {}}))   # the service is removed right away
        watcher.shutdown()
        self.assertEqual(self.calls[-1], ["stop", "ollama.service"])
        self.assertEqual(json.loads(self.state.read_text())["pending"], {})

    def test_bad_state_ignored(self):
        self.state.write_text(json.dumps({"pending": {"ollama": {"service": "x; rm"}, "nope": {}}}))
        self.assertEqual(self.watcher().needed, set())


class LmStudioTest(unittest.TestCase):
    def test_daemon_down_only_when_we_started_it(self):
        for was_up, expect in ((False, True), (True, False)):
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "c.json"
                path.write_text(json.dumps({"agents": {"hermes": ["lmstudio"]},
                                            "runtimes": {"lmstudio": {"service": "", "port": 1234, "control": "lms"}}}))
                calls, state = [], {"now": 0.0, "running": {"hermes"}}
                watcher = pw.Watcher(path, runner=lambda argv: calls.append(argv[1:]) or True,
                                     clock=lambda: state["now"], agents_now=lambda _n: (set(state["running"]), set()),
                                     busy=lambda _p, _i: False, state=None)
                watcher.lms_up = lambda: was_up
                watcher.stop_lms = lambda binary: calls.append(["daemon", "down"])
                old = pw.lms_binary
                pw.lms_binary = lambda: "/x/lms"
                try:
                    watcher.tick()
                    state["running"] = set()
                    state["now"] += 1
                    watcher.tick()
                    state["now"] += pw.GRACE + 1
                    watcher.tick()
                finally:
                    pw.lms_binary = old
                self.assertEqual(calls[:2], [["server", "start"], ["server", "stop"]])
                self.assertEqual(["daemon", "down"] in calls, expect)


class LmStudioProcessesTest(unittest.TestCase):
    def make(self, root, pid, parent, argv):
        folder = Path(root, str(pid))
        folder.mkdir()
        (folder / "stat").write_text(f"{pid} (x) S {parent} 0 0\n")
        (folder / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")

    def test_tree_and_orphaned_engine(self):
        with tempfile.TemporaryDirectory() as root:
            self.make(root, 10, 1, ["/tmp/.mount_lm-stuX/lm-studio", "--run-as-service"])
            self.make(root, 11, 10, ["/tmp/.mount_lm-stuX/lm-studio", "--type=zygote"])
            self.make(root, 12, 11, ["/usr/bin/some-helper"])                  # child without the name
            self.make(root, 13, 1, ["/home/u/.lmstudio/extensions/backends/llama.cpp-x/llama-server", "--model", "m"])
            self.make(root, 14, 1, ["/mnt/ai/llama.cpp-rocm/current/llama-server"])   # the user's own llama.cpp
            self.make(root, 15, 1, ["bash"])
            self.assertEqual(pw.lmstudio_pids(root), [10, 11, 12, 13])
            self.assertTrue(pw.lmstudio_up(root))

    def test_roles(self):
        with tempfile.TemporaryDirectory() as root:
            cases = {20: (["/opt/lm-studio/lm-studio.AppImage", "--run-as-service"], "runtime"),
                     21: (["/tmp/.mount_lm-stuX/lm-studio", "--run-as-service"], "main"),
                     22: (["/tmp/.mount_lm-stuX/lm-studio", "--type=zygote"], "helper"),
                     23: (["/tmp/.mount_lm-stuX/chrome_crashpad_handler", "--database=/x"], "outlives"),
                     24: (["/home/u/.lmstudio/.internal/utils/node", "-e", "x"], "outlives"),
                     25: (["/home/u/.lmstudio/extensions/backends/llama.cpp-v/llama-server", "--model", "m"], "engine")}
            for pid, (argv, _role) in cases.items():
                self.make(root, pid, 1, argv)
            for pid, (_argv, role) in cases.items():
                self.assertEqual(pw._lms_role(pid, root), role, pid)

    def test_window_open_touches_nothing(self):
        with tempfile.TemporaryDirectory() as root:
            self.make(root, 10, 1, ["/opt/lm-studio/lm-studio"])                # a window the user opened
            self.make(root, 13, 10, ["/home/u/.lmstudio/extensions/backends/b/llama-server"])
            self.assertEqual(pw.lmstudio_pids(root), [])


class ProcessTest(unittest.TestCase):
    def test_running_agents(self):
        with tempfile.TemporaryDirectory() as root:
            def proc(pid, comm, argv, env=b""):
                folder = Path(root, str(pid))
                folder.mkdir()
                (folder / "comm").write_text(comm + "\n")
                (folder / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
                (folder / "environ").write_bytes(env)
            proc(10, "opencode.exe", ["/x/opencode.exe", "--auto"])
            proc(11, "python3", ["python3", "/home/u/.local/bin/hermes"])
            proc(12, "claude", ["claude", "-p"], b"AI_AGENTS_MAP_NO_PROVIDERS=1\0")
            proc(13, "bash", ["bash"])
            proc(14, "opencode.exe", ["/x/opencode.exe", "serve", "--service"])
            found, background = pw.running_agents({"opencode", "hermes", "claude", "pi"}, proc=root)
            self.assertEqual(found, {"opencode", "hermes"})
            self.assertEqual(background, {14})

    def test_only_background_server_is_not_running(self):
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root, "20")
            folder.mkdir()
            (folder / "comm").write_text("opencode.exe\n")
            (folder / "cmdline").write_bytes(b"/x/opencode.exe\0serve\0--service\0")
            (folder / "environ").write_bytes(b"")
            self.assertEqual(pw.running_agents({"opencode"}, proc=root), (set(), {20}))

    def test_in_use(self):
        with tempfile.TemporaryDirectory() as root:
            head = "  sl  local_address rem_address   st\n"
            Path(root, "tcp").write_text(head + "   0: 0100007F:1F90 00000000:0000 0A\n")
            self.assertFalse(pw.in_use(8080, proc=root))
            line = "   1: 0100007F:D431 0100007F:1F90 01 00000000:00000000 00:00000000 00000000  1000        0 4242\n"
            Path(root, "tcp").write_text(head + line)
            self.assertTrue(pw.in_use(8080, proc=root))
            self.assertFalse(pw.in_use(8080, {"4242"}, proc=root))      # a background server's idle connection


class UnitTest(unittest.TestCase):
    def test_unit_runs_isolated_python(self):
        text = pw.unit_text(Path("/p/provider_watch.py"))
        self.assertIn("ExecStart=/usr/bin/python3 -I /p/provider_watch.py", text)
        self.assertIn("WantedBy=default.target", text)


class InstanceModelTest(unittest.TestCase):
    def test_model_option(self):
        self.assertEqual(pw.instance_model("claude", ["--model", "sonnet"]), "sonnet")
        self.assertEqual(pw.instance_model("claude", ["--model=opus", "-p"]), "opus")
        self.assertEqual(pw.instance_model("codex", ["-m", "gpt-5"]), "gpt-5")
        self.assertEqual(pw.instance_model("claude", ["-m", "x"]), "")   # not a model option there
        self.assertEqual(pw.instance_model("claude", []), "")


if __name__ == "__main__":
    unittest.main()
