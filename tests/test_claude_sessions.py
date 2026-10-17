import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import claude_sessions as cs  # noqa: E402


def reply(sid, stamp, out=100, cwd="/home/u/project", model="claude-opus-5-5", text="hello"):
    return {"type": "assistant", "sessionId": sid, "cwd": cwd, "timestamp": stamp,
            "message": {"model": model, "content": [{"type": "text", "text": text}],
                        "usage": {"input_tokens": 10, "output_tokens": out, "cache_read_input_tokens": 1000,
                                  "cache_creation_input_tokens": 50}}}


def line(entry) -> str:
    return json.dumps(entry, separators=(",", ":")) + "\n"


class SessionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "projects"
        (self.root / "-home-u-project").mkdir(parents=True)
        self.file = self.root / "-home-u-project" / "s1.jsonl"
        self.cache = Path(self.tmp.name) / "cache.json"

    def collect(self):
        return cs.collect(self.root, self.cache)

    def test_numbers_only(self):
        self.file.write_text(
            line({"type": "user", "sessionId": "s1", "message": {"content": "secret prompt"}})
            + line(reply("s1", "2026-10-16T10:00:00Z", text="secret answer"))
            + line(reply("s1", "2026-10-16T10:02:00Z"))
            + line({"type": "ai-title", "sessionId": "s1", "aiTitle": "Secret topic"})
            + line({"type": "cost-state", "sessionId": "s1", "totalLinesAdded": 12, "totalLinesRemoved": 3,
                    "totalCostUSD": 1.5, "totalAPIDuration": 60000}))
        [s] = self.collect()
        self.assertEqual((s.replies, s.active, s.lines_added, s.lines_removed, s.cost), (2, 120, 12, 3, 1.5))
        self.assertEqual(s.tokens, {"claude-opus-5-5": [20, 200, 2000, 100]})
        self.assertEqual(s.cwd, "/home/u/project")
        stored = self.cache.read_text()
        for word in ("secret", "Secret", "hello"):
            self.assertNotIn(word, stored)
        self.assertEqual(self.cache.stat().st_mode & 0o777, 0o600)

    def test_assistant_text_inside_a_user_line_is_not_a_reply(self):
        fake = json.dumps(reply("s1", "2026-10-16T10:00:00Z", out=10 ** 9))
        self.file.write_text(line({"type": "user", "sessionId": "s1", "message": {"content": fake}}))
        self.assertEqual(self.collect(), [])

    def test_gaps_longer_than_five_minutes_are_not_working_time(self):
        self.file.write_text(line(reply("s1", "2026-10-16T10:00:00Z")) + line(reply("s1", "2026-10-16T11:00:00Z")))
        [s] = self.collect()
        self.assertEqual(s.active, 0)

    def test_incremental_and_partial_lines(self):
        self.file.write_text(line(reply("s1", "2026-10-16T10:00:00Z")))
        self.assertEqual(self.collect()[0].replies, 1)
        with open(self.file, "a") as handle:
            handle.write(line(reply("s1", "2026-10-16T10:01:00Z"))[:-20])      # still being written
        self.assertEqual(self.collect()[0].replies, 1)
        with open(self.file, "a") as handle:
            handle.write(line(reply("s1", "2026-10-16T10:01:00Z"))[-20:])
        [s] = self.collect()
        self.assertEqual((s.replies, s.active), (2, 60))

    def test_long_lines_are_skipped(self):
        with mock.patch.object(cs, "MAX_LINE", 1000):
            self.file.write_text(line(reply("s1", "2026-10-16T10:00:00Z", text="x" * 5000))
                                 + line(reply("s1", "2026-10-16T10:01:00Z")))
            [s] = self.collect()
        self.assertEqual(s.replies, 1)

    def test_symlinked_session_file_is_not_read(self):
        outside = Path(self.tmp.name) / "outside.jsonl"
        outside.write_text(line(reply("s9", "2026-10-16T10:00:00Z")))
        (self.root / "-home-u-project" / "link.jsonl").symlink_to(outside)
        self.assertEqual(self.collect(), [])

    def test_strange_values_are_dropped(self):
        bad = reply("s1", "not a time", out=-5, cwd="relative\\npath", model="<b>x</b>")
        bad["message"]["usage"]["input_tokens"] = True
        self.file.write_text(line(bad))
        [s] = self.collect()
        self.assertEqual((s.cwd, s.first), ("", 0.0))
        self.assertEqual(s.tokens, {"unknown": [0, 0, 1000, 50]})

    def test_a_tampered_cache_is_ignored(self):
        self.file.write_text(line(reply("s1", "2026-10-16T10:00:00Z")))
        self.cache.write_text(json.dumps({"version": 1, "files": {str(self.file): {
            "inode": os.stat(self.file).st_ino, "offset": 10 ** 9, "sessions": {"s1": {"tokens": "x"}}}}}))
        [s] = self.collect()            # an offset past the end: the file is read again from the start
        self.assertEqual(s.replies, 1)

    def test_projects_and_busiest(self):
        now = time.time()
        a = cs.Session(cwd="/p/a", last=now, active=3600, replies=3, tokens={"m": [1, 2, 3, 4]}, hours={"2": 3})
        b = cs.Session(cwd="/p/b", last=now - 90 * 86400, active=7200, replies=1, tokens={"m": [1, 1, 1, 1]})
        rows = cs.projects([a, b], since=now - 30 * 86400)
        self.assertEqual([(p.cwd, p.tokens, p.cache_read) for p in rows], [("/p/a", 7, 3)])
        self.assertEqual(cs.busiest([a, b], now - 30 * 86400), (0, 2))


if __name__ == "__main__":
    unittest.main()


class SubagentsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name) / "projects" / "-home-u-project"
        self.project.mkdir(parents=True)
        self.cache = Path(self.tmp.name) / "cache.json"
        (self.project / "s1.jsonl").write_text(line(reply("s1", "2026-10-16T10:00:00Z"))
                                              + line(reply("s1", "2026-10-16T10:01:00Z")))

    def run_file(self, name, kind=None, sid="s1", meta_extra=None, folder=None):
        folder = folder or self.project / sid / "subagents"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"agent-{name}.jsonl").write_text(
            line(reply(sid, "2026-10-16T10:00:10Z", out=200, text="secret subagent text"))
            + line(reply(sid, "2026-10-16T10:00:40Z", out=200)))
        if kind is not None:
            (folder / f"agent-{name}.meta.json").write_text(
                json.dumps({"agentType": kind, "description": "secret task", **(meta_extra or {})}))

    def collect(self):
        return cs.collect_by_id(self.project.parent, self.cache)

    def test_runs_count_for_their_session_per_type(self):
        self.run_file("a1", "Explore")
        self.run_file("a2", "Explore")
        self.run_file("a3", "Plan", folder=self.project / "s1" / "subagents" / "workflows" / "w1")
        self.run_file("a4")                                   # no meta: unknown type
        sessions = self.collect()
        self.assertEqual(set(sessions), {"s1"})              # runs never replace their session
        s1 = sessions["s1"]
        self.assertEqual(s1.replies, 2)
        self.assertEqual(s1.subagents["Explore"], [2, 2 * 2 * (10 + 200 + 50), 60.0])
        self.assertEqual(s1.subagents["Plan"][0], 1)
        self.assertEqual(s1.subagents["?"][0], 1)
        rows = cs.subagents(list(sessions.values()))
        self.assertEqual([r[0] for r in rows][:1], ["Explore"])
        project = cs.projects(list(sessions.values()))[0]
        self.assertEqual(project.subagent_runs, 4)
        self.assertEqual(project.tokens, 2 * (10 + 100 + 50) + 4 * 2 * (10 + 200 + 50))
        cached = self.cache.read_text()
        self.assertNotIn("secret", cached)
        self.assertNotIn("subagents\": {\"Explore", cached)   # totals are not cached, only per file
        again = self.collect()["s1"]                          # from the cache: same, not doubled
        self.assertEqual(again.subagents["Explore"][0], 2)

    def test_old_layout_and_bad_meta(self):
        self.run_file("old", "Explore", folder=self.project)  # next to the sessions (older versions)
        self.run_file("bad", "x" * 200)                       # type not a plain short name
        self.run_file("big", "Plan", meta_extra={"pad": "y" * (cs.MAX_META + 10)})
        target = self.project / "s1" / "subagents" / "agent-link.jsonl"
        os.symlink(self.project / "s1.jsonl", target)         # links are not followed
        sessions = self.collect()
        self.assertEqual(set(sessions), {"s1"})
        self.assertEqual(sessions["s1"].replies, 2)
        self.assertEqual(sessions["s1"].subagents["Explore"][0], 1)
        self.assertEqual(sessions["s1"].subagents["?"][0], 2)
        self.assertEqual(sum(v[0] for v in sessions["s1"].subagents.values()), 3)
