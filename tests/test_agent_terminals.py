import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import agent_terminals  # noqa: E402
import session_follow  # noqa: E402


def fake_proc(root: Path, pid: int, ppid: int, stdin: str = "/dev/null", cwd: str = "/tmp", start: int = 500):
    folder = root / str(pid)
    (folder / "fd").mkdir(parents=True)
    fields = ["S", str(ppid)] + ["0"] * 17 + [str(start)] + ["0"] * 10     # field 22: starttime
    (folder / "stat").write_text(f"{pid} (x y) " + " ".join(fields))
    os.symlink(stdin, folder / "fd" / "0")
    os.symlink(cwd, folder / "cwd")


class InstancesTest(unittest.TestCase):
    """Where an agent's instances can be seen: a terminal window, the background, or here."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.proc = Path(self.tmp.name)
        (self.proc / "stat").write_text("cpu 1 2 3\nbtime 1000\n")
        fake_proc(self.proc, 100, 1)                                   # foot (a window)
        fake_proc(self.proc, 110, 100, "/dev/pts/3")                   # bash in it
        fake_proc(self.proc, 120, 110, "/dev/pts/3", "/home/u/proj")   # pi, the TUI: in the window
        fake_proc(self.proc, 130, 110, "/dev/pts/3")                   # claude, in the same window
        fake_proc(self.proc, 140, 130)                                 # claude's shell tool
        fake_proc(self.proc, 150, 140, "/dev/null", "/home/u/other")   # `pi -p` it started: background
        fake_proc(self.proc, 160, 140, "/dev/pts/3")                   # a pi on claude's terminal: still claude's
        fake_proc(self.proc, 200, 1)                                   # this app
        fake_proc(self.proc, 210, 200, "/dev/pts/9")                   # pi in its embedded terminal

    def tearDown(self):
        self.tmp.cleanup()

    def test_window_background_and_embedded(self):
        found = agent_terminals.instances([120, 150, 160, 210], {100: "pi - proj"}, embedded=210,
                                          agent_pids=[120, 130, 150, 160, 210], proc=str(self.proc))
        kinds = {i.pid: i.kind for i in found}
        self.assertEqual(kinds, {120: "window", 150: "background", 160: "background"})
        window = next(i for i in found if i.pid == 120)
        self.assertEqual((window.window, window.title, window.cwd), (100, "pi - proj", "/home/u/proj"))
        self.assertEqual(window.started, 1000 + 500 / agent_terminals.TICKS)

    def test_no_window_known(self):
        found = agent_terminals.instances([120], {}, proc=str(self.proc))
        self.assertEqual(found[0].kind, "background")

    def test_ancestors(self):
        self.assertEqual(agent_terminals.ancestors(150, str(self.proc)), [140, 130, 110, 100])


class RenderTest(unittest.TestCase):
    """What a session line says, in Pi's and Claude Code's form; control characters taken out."""

    def test_pi_lines(self):
        self.assertEqual(session_follow.render({"type": "message", "message": {"role": "user", "content": [
            {"type": "text", "text": "Analyze it"}]}}), ["› Analyze it"])
        self.assertEqual(session_follow.render({"type": "message", "message": {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "hm"}, {"type": "toolCall", "name": "read",
                                                       "arguments": {"path": "ai_core.py"}}]}}),
                         ["⚙ read  ai_core.py"])
        self.assertEqual(session_follow.render({"type": "message", "message": {"role": "toolResult", "content": [
            {"type": "text", "text": "line one\nline two\n"}]}}), ["   ↳ line one (+1 lines)"])
        self.assertEqual(session_follow.render({"type": "session", "cwd": "/x"}), [])

    def test_claude_lines(self):
        self.assertEqual(session_follow.render({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "Done."}, {"type": "tool_use", "name": "Bash", "input": {"command": "ls -l"}}]}}),
                         ["● Done.", "⚙ Bash  ls -l"])
        self.assertEqual(session_follow.render({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "content": "ok"}]}}), ["   ↳ ok"])
        self.assertEqual(session_follow.render({"type": "api-request-blob", "message": {"role": "user",
                                                                                         "content": "x"}}), [])

    def test_escape_sequences_are_taken_out(self):
        line = session_follow.render({"type": "message", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "hi\x1b]52;c;bad\x07\x1b[2J there\x9b"}]}})[0]
        self.assertNotIn("\x1b", line)
        self.assertNotIn("\x07", line)
        self.assertNotIn("\x9b", line)


class FollowTest(unittest.TestCase):
    def test_follows_a_session_file_until_the_agent_ends(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp) / "s.jsonl"
            session.write_text(json.dumps({"type": "message", "message": {"role": "user", "content": [
                {"type": "text", "text": "go"}]}}) + "\n")
            out = io.StringIO()
            with mock.patch.object(session_follow, "output_file", return_value=None), \
                    mock.patch.object(session_follow, "session_file", return_value=session), \
                    mock.patch.object(session_follow, "_cwd", return_value=tmp), \
                    mock.patch.object(session_follow, "_started", return_value=0.0), \
                mock.patch.object(session_follow, "_identity", return_value=1.0), \
                    mock.patch.object(session_follow, "alive", return_value=False):
                self.assertEqual(session_follow.follow("pi-files", 4242, "Pi", out=out, sleep=lambda _s: None, grace=0), 0)
            text = out.getvalue()
            self.assertIn("› go", text)
            self.assertIn("Pi has ended.", text)

    def test_nothing_to_follow(self):
        out = io.StringIO()
        with mock.patch.object(session_follow, "output_file", return_value=None), \
                mock.patch.object(session_follow, "session_file", return_value=None), \
                mock.patch.object(session_follow, "_cwd", return_value="/tmp"), \
                mock.patch.object(session_follow, "_started", return_value=0.0), \
                mock.patch.object(session_follow, "_identity", return_value=1.0):
            self.assertEqual(session_follow.follow("-", 4242, "Codex", out=out), 1)
        self.assertIn("without a file the app can follow", out.getvalue())

    def test_session_folders(self):
        self.assertEqual(session_follow.session_folder("pi-files", "/home/u/a.b").name, "--home-u-a.b--")
        self.assertEqual(session_follow.session_folder("claude-files", "/home/u/a.b").name, "-home-u-a-b")
        self.assertIsNone(session_follow.session_folder("codex-files", "/home/u"))
        self.assertEqual(session_follow.main(["../x", "1"]), 2)



class StopTest(unittest.TestCase):
    """The stop button left of an agent's start button ends one instance, and only that one."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.proc = Path(self.tmp.name)
        (self.proc / "stat").write_text("cpu 1 2 3\nbtime 1000\n")
        fake_proc(self.proc, 150, 1, start=500)            # started at 1000 + 500 / TICKS
        self.since = agent_terminals.started(150, str(self.proc))
        self.sent = []

    def tearDown(self):
        self.tmp.cleanup()

    def kill_ending(self, ends_on):
        def kill(handle, sig):                       # handle: the "pidfd" (here the pid itself)
            self.sent.append(sig)
            if sig in ends_on:
                (self.proc / str(handle) / "stat").unlink()
        return kill

    def stop(self, pid, since, **kw):
        kw.setdefault("sleep", lambda s: None)
        return agent_terminals.stop(pid, since, proc=str(self.proc), open_pidfd=lambda p: p, **kw)

    def test_sigterm_ends_it(self):
        import signal
        self.assertTrue(self.stop(150, self.since, kill=self.kill_ending({signal.SIGTERM})))
        self.assertEqual(self.sent, [signal.SIGTERM])

    def test_sigkill_after_the_wait(self):
        import signal
        self.assertTrue(self.stop(150, self.since, wait=0.05, kill=self.kill_ending({signal.SIGKILL})))
        self.assertEqual(self.sent, [signal.SIGTERM, signal.SIGKILL])

    def test_a_later_process_with_that_number_is_left_alone(self):
        for later in (60, 0.5, 2.0 / agent_terminals.TICKS):     # also within the same second, one tick on
            self.assertFalse(self.stop(150, self.since + later, kill=self.kill_ending(set())))
        self.assertEqual(self.sent, [])

    def test_an_unknown_start_time_is_left_alone(self):
        self.assertFalse(self.stop(150, 0.0, kill=self.kill_ending(set())))
        self.assertEqual(self.sent, [])

    def test_signals_go_through_the_handle_opened_first(self):
        import signal
        opened, sent = [], []
        def open_pidfd(pid):
            opened.append(pid)
            return f"pidfd:{pid}"
        def kill(handle, sig):
            sent.append((handle, sig))
            (self.proc / "150" / "stat").unlink()
        self.assertTrue(agent_terminals.stop(150, self.since, proc=str(self.proc), kill=kill,
                                             sleep=lambda s: None, open_pidfd=open_pidfd))
        self.assertEqual((opened, sent), ([150], [("pidfd:150", signal.SIGTERM)]))

    def test_init_and_gone_processes_are_left_alone(self):
        for pid in (0, 1, 999):
            self.assertFalse(self.stop(pid, self.since, kill=self.kill_ending(set())))
        self.assertEqual(self.sent, [])

    def test_an_ended_unreaped_process_counts_as_gone(self):
        stat = self.proc / "150" / "stat"
        stat.write_text(stat.read_text().replace(") S ", ") Z "))
        self.assertFalse(agent_terminals.alive(150, self.since, str(self.proc)))

if __name__ == "__main__":
    unittest.main()
