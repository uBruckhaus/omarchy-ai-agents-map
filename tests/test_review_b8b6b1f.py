"""HANCORE review of b8b6b1f: Shift+Insert / PRIMARY paste bounded, unfinished Claude lines
charged against the run budget."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ai_core  # noqa: E402
import claude_sessions as cs  # noqa: E402

try:
    import terminal_pane  # noqa: E402
    from gi.repository import Gdk  # noqa: E402
except Exception:            # no GTK here
    terminal_pane = None


class PrimaryPasteTest(unittest.TestCase):
    def test_primary_is_read_with_the_same_bounds(self):
        calls = []

        def fake(argv, max_bytes=0, timeout=0.0, **_kw):
            calls.append((argv, max_bytes, timeout))
            if "--list-types" in argv:
                return b"text/plain;charset=utf-8\n", True
            return b"line one\nline two\x1b[31m", True
        with mock.patch.object(ai_core, "read_bounded_output", fake):
            text = ai_core.clipboard_text(max_bytes=ai_core.MAX_TERMINAL_PASTE, terminal=True, primary=True)
        self.assertEqual(text, "line one\nline two[31m")
        self.assertEqual(len(calls), 2)
        for argv, _max, timeout in calls:
            self.assertEqual(argv[:2], ["wl-paste", "--primary"])
            self.assertLessEqual(timeout, 2.0)
        self.assertEqual(calls[1][1], ai_core.MAX_TERMINAL_PASTE)

    def test_an_oversized_primary_offer_is_refused(self):
        def fake(argv, **_kw):
            return (b"text/plain\n", True) if "--list-types" in argv else (b"x" * 10, False)
        with mock.patch.object(ai_core, "read_bounded_output", fake):
            self.assertIsNone(ai_core.clipboard_text(terminal=True, primary=True))

    def test_the_clipboard_stays_the_default(self):
        seen = []
        with mock.patch.object(ai_core, "read_bounded_output",
                               lambda argv, **_kw: (seen.append(argv), (b"", True))[1]):
            ai_core.clipboard_text(terminal=True)
        self.assertNotIn("--primary", seen[0])


@unittest.skipIf(terminal_pane is None, "GTK not available")
class ShiftInsertTest(unittest.TestCase):
    """VTE's own Shift+Insert reads PRIMARY unbounded and does not emit paste-clipboard: the
    pane's key handler takes it first."""

    def press(self, keyval, state):
        pane = mock.Mock()
        handled = terminal_pane.TerminalPane.on_key(pane, None, keyval, 0, state)
        return handled, pane

    def test_shift_insert_is_a_bounded_primary_paste(self):
        for keyval in (Gdk.KEY_Insert, Gdk.KEY_KP_Insert):
            handled, pane = self.press(keyval, Gdk.ModifierType.SHIFT_MASK)
            self.assertTrue(handled)
            pane.bounded_paste.assert_called_once_with(primary=True)

    def test_ctrl_shift_v_stays_the_clipboard(self):
        handled, pane = self.press(Gdk.KEY_V, Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK)
        self.assertTrue(handled)
        pane.bounded_paste.assert_called_once_with()

    def test_other_insert_keys_pass_through(self):
        for state in (0, Gdk.ModifierType.CONTROL_MASK,
                      Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK):
            handled, pane = self.press(Gdk.KEY_Insert, state)
            self.assertFalse(handled)
            pane.bounded_paste.assert_not_called()


class UnfinishedLineBudgetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name) / "projects" / "-home-u-project"
        self.project.mkdir(parents=True)
        self.cache = Path(self.tmp.name) / "cache.json"

    def test_a_short_unfinished_line_is_charged_and_kept(self):
        path = self.project / "s1.jsonl"
        path.write_bytes(b'{"type":"assistant","sessionId":"s1"' + b"x" * 200)   # no newline yet
        state = {}
        self.assertEqual(cs.read_file(path, state, 10_000), path.stat().st_size)
        self.assertEqual(state["offset"], 0)          # kept for when it is finished
        self.assertFalse(state["skipping"])

    def test_a_longer_line_cut_by_a_small_remaining_budget(self):
        with mock.patch.object(cs, "MAX_LINE", 1000):
            path = self.project / "s1.jsonl"
            path.write_bytes(b"y" * 3000)
            state = {}
            self.assertEqual(cs.read_file(path, state, 50), 50)
            self.assertEqual(state["offset"], 0)

    def test_many_unfinished_files_stay_within_the_run_budget(self):
        """Bytes actually read, measured at the file, never pass MAX_BYTES across files."""
        for i in range(8):
            (self.project / f"s{i}.jsonl").write_bytes(b"z" * 100)        # 8 unfinished lines
        consumed = []
        real_fdopen = os.fdopen

        class Counting:
            def __init__(self, handle):
                self.handle = handle

            def readline(self, size=-1):
                data = self.handle.readline(size)
                consumed.append(len(data))
                return data

            def __getattr__(self, name):
                return getattr(self.handle, name)

            def __enter__(self):
                self.handle.__enter__()
                return self

            def __exit__(self, *exc):
                return self.handle.__exit__(*exc)

        def counting_fdopen(fd, mode="r", *args, **kwargs):
            handle = real_fdopen(fd, mode, *args, **kwargs)
            return Counting(handle) if mode == "rb" else handle
        with mock.patch.object(cs, "MAX_BYTES", 250), \
                mock.patch.object(cs.os, "fdopen", counting_fdopen), \
                mock.patch.object(cs, "_load_cache", lambda _c: {}), \
                mock.patch.object(cs, "_save_cache", lambda *_a: None):
            cs.collect_by_id(self.project.parent, self.cache)
            first = sum(consumed)
            consumed.clear()
            cs.collect_by_id(self.project.parent, self.cache)     # a later run: a fresh budget
            second = sum(consumed)
        self.assertLessEqual(first, 250)
        self.assertGreater(first, 0)
        self.assertLessEqual(second, 250)


if __name__ == "__main__":
    unittest.main()
