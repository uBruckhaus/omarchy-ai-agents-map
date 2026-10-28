import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ai_core  # noqa: E402
import ai_helper  # noqa: E402
import ai_protocol  # noqa: E402


class ProtocolTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name) / "ai-help"

    def test_an_entry_keeps_what_was_asked_and_answered(self):
        entry = ai_protocol.Entry("Model advice", "look-up", "codex", "gpt-x",
                                  ["/bin/codex", "--disable", "apps", "PROMPT"], folder=self.folder)
        entry.look_up("model-advice", "Which model?", '{"models": []}', argv=["/bin/codex", "--search", "exec"])
        entry.note_change("MCP server figcraft added for Claude Code", "/backups/1")
        entry.end("ok", seconds=12.7, tokens=900)
        [saved] = ai_protocol.entries(self.folder)
        self.assertEqual((saved["result"], saved["seconds"], saved["tokens"]), ("ok", 12, 900))
        self.assertEqual(saved["options"], ["--disable"])            # the prompt is not an option
        self.assertEqual(saved["look_ups"][0]["prompt"], "Which model?")
        text = ai_protocol.as_text(saved)
        self.assertIn('{"models": []}', text)
        self.assertIn("backup: /backups/1", text)
        self.assertEqual(os.stat(self.folder).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(next(self.folder.iterdir())).st_mode & 0o777, 0o600)

    def test_long_texts_are_cut_and_old_entries_go(self):
        for number in range(5):
            entry = ai_protocol.Entry(f"run {number}", "task", "claude", folder=self.folder,
                                      now=ai_protocol.dt.datetime(2026, 10, 27, 12, 0, number))
            entry.end("ok", transcript="x" * (ai_protocol.MAX_TRANSCRIPT + 10))
        ai_protocol.prune(self.folder, keep=3)
        found = ai_protocol.entries(self.folder)
        self.assertEqual([e["title"] for e in found], ["run 4", "run 3", "run 2"])     # the newest first
        self.assertTrue(found[0]["transcript"].endswith("… (cut)"))

    def test_a_link_is_not_read(self):
        self.folder.mkdir()
        outside = Path(self.tmp.name) / "secret.json"
        outside.write_text('{"title": "not ours"}')
        (self.folder / "20261027-120000-000000-x.json").symlink_to(outside)
        self.assertEqual(ai_protocol.entries(self.folder), [])

    def test_look_ups_inside_watched_go_into_the_entry(self):
        entry = ai_protocol.Entry("Smart Check", "look-up", "codex", folder=self.folder)
        with mock.patch("plugin_ai.look_up_argv", return_value="the answer"), \
                mock.patch("agent_profiles.research_command", return_value=["/bin/codex", "-x", "-"]), \
                mock.patch("agent_profiles.research_input", return_value=b"q"), \
                mock.patch("agent_profiles.WORK_DIR", Path(self.tmp.name) / "work"), \
                mock.patch("harnesses.get", return_value=None):
            with ai_helper.watched(threading.Event(), entry):
                ai_helper.look_up("codex", "setup-analysis", "What is wrong?", {})
            ai_helper.look_up("codex", "setup-analysis", "not watched", {})          # outside: not noted
        self.assertEqual([(l["prompt"], l["answer"]) for l in entry.data["look_ups"]],
                         [("What is wrong?", "the answer")])


class CancelTest(unittest.TestCase):
    def test_cancel_ends_the_program_at_once(self):
        cancel = threading.Event()
        threading.Timer(0.3, cancel.set).start()
        started = time.monotonic()
        out, ok = ai_core.read_bounded_output(["sleep", "30"], timeout=20, cancel=cancel)
        self.assertFalse(ok)
        self.assertLess(time.monotonic() - started, 5)


if __name__ == "__main__":
    unittest.main()
