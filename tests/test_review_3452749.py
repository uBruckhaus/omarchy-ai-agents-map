"""HANCORE review of 3452749: FIFO model files, scan budgets, session read budgets."""

import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import agent_sessions  # noqa: E402
import claude_sessions  # noqa: E402
import context_ctl  # noqa: E402
import inventory  # noqa: E402
import model_layout  # noqa: E402
import models_ctl  # noqa: E402
import scan_budget  # noqa: E402


class FifoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_fifo_gguf_returns_at_once(self):
        fifo = self.dir / "blocked.gguf"
        os.mkfifo(fifo)
        started = time.monotonic()
        with self.assertRaises(ValueError):
            context_ctl.read_gguf(fifo)
        self.assertLess(time.monotonic() - started, 1)
        self.assertIsNone(context_ctl.llama_model_file(self.dir, "blocked"))
        self.assertEqual(inventory.gguf_files(self.dir), [])

    def test_regular_file_and_link_still_read(self):
        model = self.dir / "m.gguf"
        model.write_bytes(b"GGUF")              # too short: a ValueError from the header, not the type
        link = self.dir / "l.gguf"
        link.symlink_to(model)
        for path in (model, link):
            with context_ctl.open_regular(path) as handle:
                self.assertEqual(handle.read(4), b"GGUF")
        self.assertEqual(sorted(p.name for p in inventory.gguf_files(self.dir)), ["l.gguf", "m.gguf"])


class ScanBudgetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_flat_folder_stops_at_the_result_limit(self):
        for number in range(60):
            (self.dir / f"m{number:03}.gguf").write_bytes(b"x")
        old = model_layout.MAX_FILES
        model_layout.MAX_FILES = 20
        try:
            budget = scan_budget.Budget()
            files, links = model_layout.walk_gguf(self.dir, set(), budget=budget)
        finally:
            model_layout.MAX_FILES = old
        self.assertEqual(len(files) + len(links), 20)
        self.assertTrue(budget.stopped)

    def test_empty_folders_stop_at_the_visit_budget(self):
        for number in range(30):
            (self.dir / f"d{number}" / "e").mkdir(parents=True)
        budget = scan_budget.Budget(visits=10)
        self.assertEqual(models_ctl.local_ggufs([self.dir, self.dir], set(), budget), [])
        self.assertTrue(budget.stopped)
        self.assertEqual(budget.left, 0)

    def test_cancel_stops_a_scan(self):
        (self.dir / "a.gguf").write_bytes(b"x")
        cancel = threading.Event()
        cancel.set()
        budget = scan_budget.Budget(cancel=cancel)
        self.assertEqual(model_layout.walk_gguf(self.dir, set(), budget=budget), ([], []))
        self.assertTrue(budget.stopped)

    def test_entries_never_more_than_the_limit(self):
        for number in range(50):
            (self.dir / f"f{number}").write_bytes(b"")
        self.assertEqual(len(scan_budget.entries(self.dir, 7)), 7)


class ClaudeSkipTest(unittest.TestCase):
    def test_overlong_line_charged_and_never_reread(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "s.jsonl"
        long = claude_sessions.MAX_LINE * 3
        path.write_bytes(b"x" * long)                    # unfinished, longer than MAX_LINE
        state = {}
        budget = claude_sessions.MAX_LINE + 100
        read = claude_sessions.read_file(path, state, budget)
        self.assertLessEqual(read, budget + claude_sessions.MAX_LINE + 1)
        self.assertLess(read, long)
        first = state["offset"]
        self.assertGreater(first, 0)                     # forward progress
        self.assertTrue(state["skipping"])
        total = first
        for _ in range(10):
            total += claude_sessions.read_file(path, state, budget)
        self.assertEqual(state["offset"], long)          # the whole line skipped, once
        with path.open("ab") as handle:
            handle.write(b"\n" + b'{"type": "user"}\n')
        claude_sessions.read_file(path, state, budget)
        self.assertFalse(state["skipping"])
        self.assertEqual(state["offset"], path.stat().st_size)


class OpenclawSelectTest(unittest.TestCase):
    def test_only_the_used_small_representation(self):
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE transcript_events (session_id, event_json, event_zstd)")
        big = "x" * (agent_sessions.MAX_EVENT + 1)
        con.executemany("INSERT INTO transcript_events VALUES (?, ?, ?)", [
            ("big-json-empty-zstd", big, b""),
            ("small-json-big-zstd", '{"a": 1}', b"z" * (agent_sessions.MAX_EVENT + 1)),
            ("packed", None, b"zz")])
        rows = {r[0]: r for r in con.execute(agent_sessions.OPENCLAW_EVENTS,
                                             (agent_sessions.MAX_EVENT, agent_sessions.MAX_EVENT, 100))}
        self.assertNotIn("big-json-empty-zstd", rows)
        self.assertIsNone(rows["small-json-big-zstd"][2])   # the unused packed copy is not fetched
        self.assertEqual(rows["packed"][2], b"zz")


if __name__ == "__main__":
    unittest.main()
