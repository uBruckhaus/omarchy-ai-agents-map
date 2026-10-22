import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import usage_box as t  # noqa: E402


def ev(*items):
    return [t.Event(when, kind, key, tokens) for when, kind, key, tokens in items]


def stamp(seconds: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(seconds)) + f".{int(seconds * 1000) % 1000:03d}Z"


class LastReplyTest(unittest.TestCase):
    def test_tokens_over_the_time_since_its_input(self):
        events = ev((100.0, "in", "", 0), (102.0, "out", "a", 200), (104.0, "out", "a", 200))
        rate, end = t.last_reply(events)
        self.assertAlmostEqual(rate, 50.0)          # 200 tokens from 100 s to 104 s
        self.assertEqual(end, 104.0)

    def test_newest_reply_counts(self):
        events = ev((0.0, "in", "", 0), (1.0, "out", "a", 10), (10.0, "in", "", 0), (12.0, "out", "b", 100))
        self.assertAlmostEqual(t.last_reply(events)[0], 50.0)

    def test_replies_in_a_row_start_where_the_last_ended(self):
        events = ev((0.0, "in", "", 0), (2.0, "out", "a", 10), (6.0, "out", "b", 40))
        self.assertAlmostEqual(t.last_reply(events)[0], 10.0)

    def test_no_input_or_no_time_gives_nothing(self):
        self.assertIsNone(t.last_reply(ev((5.0, "out", "a", 10))))
        self.assertIsNone(t.last_reply(ev((5.0, "in", "", 0), (5.1, "out", "a", 10))))
        self.assertIsNone(t.last_reply([]))

    def test_texts(self):
        self.assertEqual(t.rate_text(4.23), "4.2 t/s")
        self.assertEqual(t.rate_text(72.6), "73 t/s")
        self.assertEqual(t.size_text(1_000_000), "1M")
        self.assertEqual(t.size_text(1_048_576), "1M")
        self.assertEqual(t.size_text(1_500_000), "1.5M")
        self.assertEqual(t.size_text(272_000), "272k")
        self.assertEqual(t.size_text(32768), "33k")


class BoxTest(unittest.TestCase):
    def test_parts_in_the_line_colour_until_the_battery_thresholds(self):
        reading = t.Reading(72.6, 190_000, 1_000_000, "m")
        self.assertEqual(t.parts(reading, t.ITEMS), [("73 t/s", "line"), ("1M", "line"), ("19%", "line")])
        self.assertEqual(t.parts(t.Reading(None, 80, 100), t.ITEMS), [("100", "line"), ("80%", "yellow")])
        self.assertEqual(t.parts(t.Reading(None, 95, 100), ("used",)), [("95%", "red")])
        self.assertEqual(t.parts(reading, ("rate",)), [("73 t/s", "line")])
        self.assertEqual(t.parts(reading, ()), [])

    def test_unknown_size_shows_no_share(self):
        self.assertEqual(t.parts(t.Reading(5.0, 1000, 0), t.ITEMS), [("5.0 t/s", "line")])

    def test_context_size_from_the_adapter(self):
        contexts = {"claude-opus-5": 1_000_000, "": 200_000}
        self.assertEqual(t.window_for("claude-opus-5-5", contexts), 1_000_000)
        self.assertEqual(t.window_for("claude-haiku-5-5", contexts), 200_000)
        self.assertEqual(t.window_for("x", {}), 0)


class FormatsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, lines) -> Path:
        path = self.dir / "s.jsonl"
        path.write_text("".join(json.dumps(line) + "\n" for line in lines))
        return path

    def test_claude_reply_lines_count_once(self):
        usage = {"output_tokens": 300, "input_tokens": 2, "cache_read_input_tokens": 189000,
                 "cache_creation_input_tokens": 698}
        message = {"id": "m1", "model": "claude-opus-5-5", "usage": usage, "content": []}
        path = self.write([
            {"type": "user", "timestamp": stamp(1000)},
            {"type": "assistant", "timestamp": stamp(1001), "message": message},
            {"type": "assistant", "timestamp": stamp(1003), "message": message},
        ])
        reading, end = t.read("claude-jsonl", path, {"claude-opus-5": 1_000_000})
        self.assertAlmostEqual(reading.rate, 100.0)
        self.assertEqual((reading.used, reading.window, reading.model), (190_000, 1_000_000, "claude-opus-5-5"))
        self.assertAlmostEqual(reading.share, 0.19)

    def test_codex_token_count(self):
        path = self.write([
            {"timestamp": stamp(1000), "type": "event_msg", "payload": {"type": "user_message"}},
            {"timestamp": stamp(1001), "type": "response_item", "payload": {"type": "reasoning"}},
            {"timestamp": stamp(1004), "type": "event_msg", "payload": {
                "type": "token_count", "info": {"last_token_usage": {"output_tokens": 80, "total_tokens": 27200},
                                                "model_context_window": 272000}}},
        ])
        reading = t.read("codex-jsonl", path)[0]
        self.assertAlmostEqual(reading.rate, 20.0)
        self.assertEqual((reading.used, reading.window), (27200, 272000))

    def test_pi_messages(self):
        path = self.write([
            {"type": "model_change", "timestamp": stamp(999), "modelId": "gemma"},
            {"type": "message", "id": "a", "timestamp": stamp(1000), "message": {"role": "toolResult"}},
            {"type": "message", "id": "b", "timestamp": stamp(1002),
             "message": {"role": "assistant", "usage": {"output": 90, "totalTokens": 5000}}},
        ])
        reading = t.read("pi-jsonl", path)[0]
        self.assertAlmostEqual(reading.rate, 45.0)
        self.assertEqual((reading.used, reading.window, reading.model), (5000, 0, "gemma"))

    def test_unknown_format_and_bad_lines(self):
        path = self.dir / "s.jsonl"
        path.write_bytes(b"not json\n{\"type\": \"assistant\"}\n")
        self.assertIsNone(t.read("claude-jsonl", path))
        self.assertIsNone(t.read("nothing", path))

    def test_link_is_not_read(self):
        real = self.write([{"type": "user", "timestamp": stamp(1)}])
        link = self.dir / "l.jsonl"
        link.symlink_to(real)
        self.assertEqual(t.tail_lines(link), [])

    def test_only_the_tail_is_read(self):
        path = self.dir / "s.jsonl"
        path.write_bytes(b"x" * (t.TAIL + 100) + b"\n" + b'{"a": 1}\n')
        self.assertEqual(t.tail_lines(path), [b'{"a": 1}'])


class NewestFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_newest_at_its_depth(self):
        (self.root / "p1").mkdir()
        (self.root / "p2").mkdir()
        old, new = self.root / "p1/a.jsonl", self.root / "p2/b.jsonl"
        old.write_text("")
        new.write_text("")
        os.utime(old, (time.time() - 30, time.time() - 30))
        (self.root / "top.jsonl").write_text("")                  # not at depth 1
        self.assertEqual(t.newest_file(self.root, 1, time.time() - 60), new)

    def test_nothing_recent(self):
        (self.root / "p").mkdir()
        f = self.root / "p/a.jsonl"
        f.write_text("")
        os.utime(f, (time.time() - 300, time.time() - 300))
        self.assertIsNone(t.newest_file(self.root, 1, time.time() - 60))


class RatesTest(unittest.TestCase):
    def test_from_the_adapters_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "p").mkdir()
            now = time.time()
            usage = {"output_tokens": 120}
            (root / "p/s.jsonl").write_text("".join(json.dumps(x) + "\n" for x in (
                {"type": "user", "timestamp": stamp(now - 5)},
                {"type": "assistant", "timestamp": stamp(now - 2), "message": {"id": "m", "usage": usage}})))
            with mock.patch.object(t, "roots", lambda c: (root, 1, "claude-jsonl", {}) if c == "x" else ()):
                readings = t.Rates().readings(["x", "y"], wall=now)
        self.assertEqual(list(readings), ["x"])
        self.assertAlmostEqual(readings["x"].rate, 40.0, places=0)

    def test_old_reply_shows_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "p").mkdir()
            now = time.time()
            (root / "p/s.jsonl").write_text("".join(json.dumps(x) + "\n" for x in (
                {"type": "user", "timestamp": stamp(now - 300)},
                {"type": "assistant", "timestamp": stamp(now - 290), "message": {"id": "m", "usage": {"output_tokens": 5}}})))
            with mock.patch.object(t, "roots", lambda c: (root, 1, "claude-jsonl", {})):
                self.assertEqual(t.Rates().readings(["x"], wall=now), {})

    def test_adapters_name_the_folders(self):
        self.assertEqual(t.roots("claude")[1:3], (1, "claude-jsonl"))
        self.assertEqual(t.roots("claude")[3][""], 200_000)
        self.assertEqual(t.roots("codex")[1:], (3, "codex-jsonl", {}))
        self.assertEqual(t.roots("pi")[1:], (1, "pi-jsonl", {}))
        self.assertEqual(t.roots("no-such-agent"), ())


if __name__ == "__main__":
    unittest.main()
