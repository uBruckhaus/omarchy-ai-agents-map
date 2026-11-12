"""process_group: a program's whole group ends, also when the program itself ends first
(HANCORE review of 3452749: a child that ignores SIGTERM outlived a prompt parent)."""

import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ai_core  # noqa: E402
import mcp_tools  # noqa: E402
import process_group  # noqa: E402

# A parent that writes its child's pid and ends on SIGTERM; the child ignores SIGTERM and has
# output of its own (not the parent's stdout), so the parent's EOF says nothing about it.
STUBBORN = r'''
import os, signal, subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c",
    "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(120)"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
open(sys.argv[1], "w").write(str(child.pid))
time.sleep(0.3)                       # the child has set SIG_IGN by now
mode = sys.argv[2]
if mode == "exit":
    print("done", flush=True)
    sys.exit(0)
time.sleep(120)                       # "wait": runs until it is ended
'''


def running(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as handle:
            return handle.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


class EndChildTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.script = Path(self.tmp.name) / "stubborn.py"
        self.script.write_text(STUBBORN)
        self.pidfile = Path(self.tmp.name) / "child.pid"

    def child_pid(self) -> int:
        for _ in range(100):
            if self.pidfile.exists() and self.pidfile.read_text():
                return int(self.pidfile.read_text())
            time.sleep(0.05)
        self.fail("no child pid")

    def assertGone(self, pid: int):
        for _ in range(60):
            if not running(pid):
                return
            time.sleep(0.05)
        os.kill(pid, signal.SIGKILL)
        self.fail("a child of the group is still running")

    def test_parent_ended_first_child_ignoring_sigterm(self):
        proc = subprocess.Popen([sys.executable, str(self.script), str(self.pidfile), "exit"],
                                stdout=subprocess.DEVNULL, start_new_session=True)
        child = self.child_pid()
        time.sleep(0.5)                                    # the parent has ended (a zombie)
        self.assertEqual(process_group.end_child(proc, grace=0.3), 0)
        self.assertIsNotNone(proc.returncode)              # reaped
        self.assertGone(child)

    def test_parent_running_gets_term_then_rest_kill(self):
        proc = subprocess.Popen([sys.executable, str(self.script), str(self.pidfile), "wait"],
                                stdout=subprocess.DEVNULL, start_new_session=True)
        child = self.child_pid()
        process_group.end_child(proc, grace=0.3)
        self.assertIsNotNone(proc.returncode)
        self.assertGone(child)

    def test_read_bounded_output_success_cleans_the_group(self):
        out, ok = ai_core.read_bounded_output([sys.executable, str(self.script), str(self.pidfile), "exit"],
                                              timeout=10)
        self.assertTrue(ok)
        self.assertEqual(out.strip(), b"done")
        self.assertGone(self.child_pid())

    def test_read_bounded_output_deadline_cleans_the_group(self):
        _out, ok = ai_core.read_bounded_output([sys.executable, str(self.script), str(self.pidfile), "wait"],
                                               timeout=1)
        self.assertFalse(ok)
        self.assertGone(self.child_pid())

    def test_keep_children_leaves_a_launched_server(self):
        _out, ok = ai_core.read_bounded_output([sys.executable, str(self.script), str(self.pidfile), "exit"],
                                               timeout=10, keep_children=True)
        child = self.child_pid()
        try:
            self.assertTrue(ok)
            self.assertTrue(running(child))
        finally:
            os.kill(child, signal.SIGKILL)

    def test_mcp_stdio_close(self):
        stdio = mcp_tools._Stdio({"command": sys.executable, "args": [str(self.script), str(self.pidfile), "exit"],
                                  "env": {}})
        child = self.child_pid()
        time.sleep(0.5)
        stdio.close()
        self.assertIsNotNone(stdio.proc.returncode)
        self.assertGone(child)


class GroupTest(unittest.TestCase):
    """The embedded terminal's case: the leader is reaped by somebody else (VTE) on SIGHUP."""

    def test_members_held_after_the_leader_is_reaped(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        pidfile = Path(tmp.name) / "child.pid"
        code = ("import signal, subprocess, sys, time\n"
                "c = subprocess.Popen([sys.executable, '-c', 'import signal, time\\n"
                "signal.signal(signal.SIGHUP, signal.SIG_IGN)\\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\\n"
                "time.sleep(120)'])\n"
                f"open({str(pidfile)!r}, 'w').write(str(c.pid))\n"
                "time.sleep(120)\n")
        leader = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
        for _ in range(100):
            if pidfile.exists() and pidfile.read_text():
                break
            time.sleep(0.05)
        child = int(pidfile.read_text())
        time.sleep(0.3)
        group = process_group.Group(leader.pid, os.pidfd_open(leader.pid))
        self.addCleanup(group.close)
        self.assertIn(child, group.fds)
        group.signal(signal.SIGHUP)
        leader.wait(timeout=5)                         # "VTE" reaps the leader
        self.assertTrue(group.alive())                 # the child ignored SIGHUP
        group.signal(signal.SIGTERM)
        time.sleep(0.2)
        self.assertTrue(running(child))                # … and SIGTERM
        group.signal(signal.SIGKILL)
        for _ in range(60):
            if not running(child):
                break
            time.sleep(0.05)
        self.assertFalse(running(child))
        self.assertFalse(group.alive())

    def test_child_started_by_the_sighup_handler_after_the_snapshot(self):
        """HANCORE at b8b6b1f: the leader starts a child in its SIGHUP handler and exits at once;
        "VTE" reaps it before any rescan, so no held member anchors the group. SIGTERM/SIGKILL
        must still reach that child (through the leader's pidfd, PIDFD_SIGNAL_PROCESS_GROUP)."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        pidfile = Path(tmp.name) / "child.pid"
        ready = Path(tmp.name) / "ready"
        code = ("import os, signal, subprocess, sys, time\n"
                "def hup(*_):\n"
                "    c = subprocess.Popen([sys.executable, '-c', 'import signal, time\\n"
                "signal.signal(signal.SIGHUP, signal.SIG_IGN)\\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\\n"
                "time.sleep(120)'])\n"
                f"    open({str(pidfile)!r}, 'w').write(str(c.pid))\n"
                "    os._exit(0)\n"
                "signal.signal(signal.SIGHUP, hup)\n"
                f"open({str(ready)!r}, 'w').write('1')\n"
                "time.sleep(120)\n")
        leader = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
        self.addCleanup(lambda: leader.poll() is None and leader.kill())
        for _ in range(100):
            if ready.exists():
                break
            time.sleep(0.05)
        group = process_group.Group(leader.pid, os.pidfd_open(leader.pid))
        self.addCleanup(group.close)
        self.assertEqual(list(group.fds), [leader.pid])   # the snapshot: only the leader
        group.signal(signal.SIGHUP)
        leader.wait(timeout=5)                             # "VTE" reaps it before any rescan
        for _ in range(100):
            if pidfile.exists() and pidfile.read_text():
                break
            time.sleep(0.05)
        child = int(pidfile.read_text())
        self.addCleanup(lambda: running(child) and os.kill(child, signal.SIGKILL))
        time.sleep(0.3)                                    # the child has set SIG_IGN by now
        self.assertNotIn(child, group.fds)
        self.assertTrue(group.alive())                     # the group still has a running member
        group.signal(signal.SIGTERM)
        time.sleep(0.2)
        self.assertTrue(running(child))                    # it ignores SIGTERM …
        group.signal(signal.SIGKILL)                       # … not SIGKILL
        for _ in range(60):
            if not running(child):
                break
            time.sleep(0.05)
        self.assertFalse(running(child))
        for _ in range(60):
            if not group.alive():
                break
            time.sleep(0.05)
        self.assertFalse(group.alive())

    def test_each_member_gets_a_signal_once(self):
        """Codex: the whole-group signal must not be followed by a second copy to each held
        member (a SIGHUP handler that is still running would run twice)."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        count = Path(tmp.name) / "count"
        ready = Path(tmp.name) / "ready"
        code = ("import signal, time\n"
                "def hup(*_):\n"
                f"    open({str(count)!r}, 'a').write('x')\n"
                "signal.signal(signal.SIGHUP, hup)\n"
                f"open({str(ready)!r}, 'w').write('1')\n"
                "time.sleep(30)\n")
        leader = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
        self.addCleanup(lambda: leader.poll() is None and leader.kill())
        for _ in range(100):
            if ready.exists():
                break
            time.sleep(0.05)
        group = process_group.Group(leader.pid, os.pidfd_open(leader.pid))
        self.addCleanup(group.close)
        group.signal(signal.SIGHUP)
        time.sleep(0.5)
        self.assertEqual(count.read_text(), "x")

    def test_whole_group_signal_after_the_group_is_gone(self):
        """After the group is gone its number may belong to another group; the leader's pidfd
        names the old group, so nothing is signalled."""
        leader = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
        fd = os.pidfd_open(leader.pid)
        leader.wait()
        group = process_group.Group(leader.pid, fd)
        self.addCleanup(group.close)
        self.assertIs(group._whole_group(signal.SIGKILL), False)

    def test_a_pid_reused_outside_the_group_is_not_held(self):
        leader = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
        self.addCleanup(leader.kill)
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])   # not in the group
        self.addCleanup(other.kill)
        group = process_group.Group(leader.pid, os.pidfd_open(leader.pid))
        self.addCleanup(group.close)
        fd = os.pidfd_open(other.pid)
        try:
            self.assertFalse(group._in_group(fd, other.pid))
        finally:
            os.close(fd)
        self.assertNotIn(other.pid, group.fds)

    def test_no_member_held_nothing_signalled(self):
        leader = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
        fd = os.pidfd_open(leader.pid)
        leader.wait()
        group = process_group.Group(leader.pid, fd)    # leader gone: no group to look up
        self.addCleanup(group.close)
        self.assertEqual(list(group.fds), [leader.pid])
        group.signal(signal.SIGKILL)                   # no error, nobody else signalled
        self.assertFalse(group.alive())


if __name__ == "__main__":
    unittest.main()
