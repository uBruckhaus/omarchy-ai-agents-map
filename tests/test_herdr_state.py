import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import herdr_state  # noqa: E402

SNAPSHOT = {"id": "cli:api:snapshot", "result": {"type": "session_snapshot", "snapshot": {
    "version": "0.9.1", "protocol": 22, "tabs": [], "panes": [], "layouts": [],
    "workspaces": [{"workspace_id": "w1", "label": "asterism", "number": 1}],
    "agents": [
        {"agent": "claude", "agent_status": "idle", "cwd": "/home/u/a", "workspace_id": "w1",
         "terminal_title": "secret topic", "pane_id": "p1", "tab_id": "t1", "terminal_id": "x", "focused": False,
         "revision": 1},
        {"agent": "claude", "agent_status": "blocked", "foreground_cwd": "/home/u/b", "cwd": "/home/u",
         "workspace_id": "w1", "pane_id": "p2", "tab_id": "t1", "terminal_id": "y", "focused": True, "revision": 1},
        {"agent": "cursor", "agent_status": "working", "cwd": "relative", "workspace_id": "w9"},
        {"agent": "claude", "agent_status": "starting"},
        {"agent": "Bad Name!", "agent_status": "idle"},
        {"agent": None, "agent_status": "idle"},
        "junk",
    ]}}}


class HerdrTest(unittest.TestCase):
    def test_parse_keeps_kind_state_folder_label_only(self):
        panes = herdr_state.parse(SNAPSHOT)
        self.assertEqual([(p.agent, p.state) for p in panes],
                         [("claude", "idle"), ("claude", "blocked"), ("cursor-agent", "working")])
        self.assertEqual(herdr_state.parse({"agents": [{"agent": "pi", "agent_status": "unknown"}]})[0].state,
                         "unknown")                           # found, screen not readable: still listed
        self.assertEqual(panes[1].cwd, "/home/u/b")           # where it works now
        self.assertEqual(panes[2].cwd, "")                    # not an absolute path
        self.assertEqual(panes[0].workspace, "asterism")
        self.assertNotIn("secret", repr(panes))
        self.assertEqual([p.state for p in herdr_state.for_agent(panes, "claude")], ["blocked", "idle"])
        self.assertEqual(herdr_state.parse({"error": {"code": "x"}}), [])
        self.assertEqual(herdr_state.parse("nonsense"), [])

    def test_only_this_users_running_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp)
            path = config / "herdr.sock"
            self.assertFalse(herdr_state.server_running(path))
            path.write_text("")                               # a file, not a socket
            self.assertFalse(herdr_state.server_running(path))
            path.unlink()
            for sock in (path, config / "sessions/work/herdr.sock"):
                sock.parent.mkdir(parents=True, exist_ok=True)
                server = socket.socket(socket.AF_UNIX)
                server.bind(str(sock))
                self.addCleanup(server.close)
            (config / "sessions/stopped").mkdir()             # no socket: not running
            (config / "sessions/bad name").mkdir()
            self.assertTrue(herdr_state.server_running(path))
            with mock.patch.object(os, "getuid", return_value=os.getuid() + 1):
                self.assertFalse(herdr_state.server_running(path))
            env = {k: v for k, v in os.environ.items() if k != "HERDR_SOCKET_PATH"}
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch("shutil.which", return_value="/usr/bin/herdr"), \
                    mock.patch("ai_core.read_bounded_output", return_value=(json.dumps(SNAPSHOT).encode(), True)) as run:
                self.assertEqual([n for n, _s in herdr_state.sessions(config)], ["", "stopped", "work"])
                panes = herdr_state.snapshot(config)
                self.assertEqual(len(panes), 6)
                self.assertEqual({p.session for p in panes}, {"", "work"})
                self.assertEqual([c.args[0] for c in run.call_args_list],
                                 [["/usr/bin/herdr", "api", "snapshot"],
                                  ["/usr/bin/herdr", "--session", "work", "api", "snapshot"]])
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch("shutil.which", return_value="/usr/bin/herdr"), \
                    mock.patch("ai_core.read_bounded_output") as run:
                self.assertIsNone(herdr_state.snapshot(config / "nothing"))
                run.assert_not_called()                       # no server: herdr is not even run

    def test_focus_and_clients(self):
        panes = herdr_state.parse(SNAPSHOT, "work")
        self.assertEqual(panes[1].pane_id, "p2")
        with mock.patch("shutil.which", return_value="/usr/bin/herdr"), \
                mock.patch("ai_core.read_bounded_output", return_value=(b"{}", True)) as run:
            self.assertTrue(herdr_state.focus(panes[1]))
            self.assertEqual(run.call_args.args[0], ["/usr/bin/herdr", "--session", "work", "agent", "focus", "p2"])
            self.assertFalse(herdr_state.focus(herdr_state.Pane("claude", "idle", pane_id="")))
            self.assertFalse(herdr_state.focus(herdr_state.Pane("claude", "idle", pane_id="-x; rm")))
        processes = [(10, "herdr", ["herdr"]), (11, "herdr", ["herdr", "--session", "work"]),
                     (12, "herdr", ["/usr/bin/herdr", "server"]), (13, "herdr", ["herdr", "api", "snapshot"]),
                     (14, "herdr", ["herdr", "--session", "work", "api", "snapshot"]), (15, "bash", ["herdr"])]
        self.assertEqual(herdr_state.client_pids("", processes), [10])
        self.assertEqual(herdr_state.client_pids("work", processes), [11])

    def test_terminal_found_through_its_parents(self):
        import desktop_omarchy
        parents = {500: 400, 400: 300, 300: 1}
        with mock.patch.object(desktop_omarchy, "_clients", return_value=[{"pid": 300}, {"pid": 7}]), \
                mock.patch.object(desktop_omarchy, "_parent", side_effect=lambda pid: parents.get(pid, 0)), \
                mock.patch.object(desktop_omarchy, "_dispatch", return_value=True) as dispatch:
            self.assertTrue(desktop_omarchy.focus_process([500]))
            self.assertEqual(dispatch.call_args.args[0], 'hl.dsp.focus({ window = "pid:300" })')
            dispatch.reset_mock()
            self.assertFalse(desktop_omarchy.focus_process([999]))
            dispatch.assert_not_called()
        self.assertEqual(desktop_omarchy._parent(os.getpid()), os.getppid())
        with mock.patch.object(os, "getuid", return_value=os.getuid() + 1):
            self.assertEqual(desktop_omarchy._parent(os.getpid()), 0)   # another user's process: not followed

    def test_generic_desktop_focuses_through_hyprland(self):
        """Asterism outside Omarchy (generic mode) on Hyprland: the same window look-up; elsewhere none."""
        import desktop_generic
        import desktop_omarchy
        with mock.patch.object(desktop_generic, "_hyprland", return_value=desktop_omarchy), \
                mock.patch.object(desktop_omarchy, "focus_process", return_value=True) as focus:
            self.assertTrue(desktop_generic.focus_process([500]))
            focus.assert_called_once_with([500])
        with mock.patch.object(desktop_generic, "_hyprland", return_value=None):
            self.assertFalse(desktop_generic.focus_process([500]))


if __name__ == "__main__":
    unittest.main()
