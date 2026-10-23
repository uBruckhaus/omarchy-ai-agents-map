import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import agent_activity  # noqa: E402


class ActivityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.proc = Path(self.tmp.name)
        self.ticks = {}

    def stat(self, pid, ticks):
        folder = self.proc / str(pid)
        folder.mkdir(exist_ok=True)
        # pid (comm with ) inside) state ppid ... utime stime cutime cstime at fields 14-17
        fields = ["S", "1"] + ["0"] * 9 + [str(ticks), "0", "0", "0"] + ["0"] * 10
        (folder / "stat").write_text(f"{pid} (node (x)) " + " ".join(fields))

    def test_cpu_share_since_last_reading(self):
        self.stat(10, 0)
        activity = agent_activity.Activity()
        with mock.patch.object(agent_activity, "TICKS", 100):
            self.assertEqual(activity.cpu({"claude": [10]}, str(self.proc), now=0.0), {"claude": 0.0})
            self.stat(10, 50)                                     # 0.5 s of CPU in 2 s
            self.assertAlmostEqual(activity.cpu({"claude": [10]}, str(self.proc), now=2.0)["claude"], 0.25)
            self.assertEqual(activity.cpu({"claude": [99]}, str(self.proc), now=4.0), {"claude": 0.0})   # gone

    def test_in_turn_from_the_open_session_file(self):
        """Codex: task_started … task_complete in the rollout file it keeps open."""
        self.stat(20, 0)
        (self.proc / "20" / "fd").mkdir()
        rollout = self.proc / "rollout.jsonl"
        os.symlink(rollout, self.proc / "20" / "fd" / "7")
        os.symlink(self.proc / "other.sqlite", self.proc / "20" / "fd" / "8")
        rollout.write_text('{"type":"event_msg","payload":{"type":"task_started"}}\n'
                           '{"type":"response_item","payload":{"type":"reasoning"}}\n')
        now = rollout.stat().st_mtime
        self.assertTrue(agent_activity.in_turn(20, str(self.proc), wall=now + 60))       # thinking, quiet for a minute
        self.assertFalse(agent_activity.in_turn(20, str(self.proc), wall=now + agent_activity.TURN_STALE + 1))
        with rollout.open("a") as handle:
            handle.write('{"type":"event_msg","payload":{"type":"task_complete"}}\n')
        self.assertFalse(agent_activity.in_turn(20, str(self.proc), wall=now))            # at its prompt
        self.assertFalse(agent_activity.in_turn(99, str(self.proc), wall=now))            # gone
        activity = agent_activity.Activity()
        with rollout.open("a") as handle:
            handle.write('{"type":"event_msg","payload":{"type":"task_started"}}\n')
        with mock.patch.object(agent_activity.agent_details, "newest_mtime", return_value=None):
            self.assertEqual(activity.working({"codex": [20]}, {}, set(), str(self.proc), now=0.0,
                                              wall=rollout.stat().st_mtime + 20), {"codex"})

    def test_working_from_any_signal(self):
        activity = agent_activity.Activity()
        idle = {"claude": [], "pi": [], "codex": [], "hermes": []}
        with mock.patch.object(agent_activity.agent_details, "newest_mtime", return_value=None):
            self.assertEqual(activity.working(idle, {}, set(), str(self.proc), now=0.0, wall=1000.0), set())
            self.assertEqual(activity.working(idle, {"pi": {"working"}, "codex": {"idle"}}, {"hermes"},
                                              str(self.proc), now=2.0, wall=1000.0), {"pi", "hermes"})
        import datetime as dt
        recent = dt.datetime.fromtimestamp(995.0)
        with mock.patch.object(agent_activity.agent_details, "newest_mtime", return_value=recent):
            self.assertEqual(activity.working({"claude": []}, {}, set(), str(self.proc), now=4.0, wall=1000.0),
                             {"claude"})                         # its session file was written 5 s ago
            self.assertEqual(activity.working({"claude": []}, {}, set(), str(self.proc), now=6.0, wall=1020.0),
                             set())                              # 25 s ago: waiting at its prompt

    def test_other_users_processes_are_not_read(self):
        self.stat(10, 500)
        with mock.patch.object(agent_activity.os, "getuid", return_value=os.getuid() + 1):
            self.assertIsNone(agent_activity.cpu_ticks(10, str(self.proc)))


if __name__ == "__main__":
    unittest.main()
