import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import claude_sessions  # noqa: E402
import edition  # noqa: E402
import harness_hermes  # noqa: E402
import harness_opencode  # noqa: E402
import harnesses  # noqa: E402

T0 = 1_790_000_000.0            # a fixed moment (epoch seconds)


def opencode_db(path: Path):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE session_v2 (id TEXT, parent_id TEXT, agent TEXT, directory TEXT, title TEXT,"
               " time_created INTEGER, time_updated INTEGER, model TEXT, tokens_input INTEGER,"
               " tokens_output INTEGER, tokens_reasoning INTEGER, tokens_cache_read INTEGER,"
               " tokens_cache_write INTEGER, cost REAL, summary_additions INTEGER, summary_deletions INTEGER)")
    db.execute("CREATE TABLE session_message (session_id TEXT, time_created INTEGER, data TEXT)")
    ms = int(T0 * 1000)
    db.execute("INSERT INTO session_v2 VALUES ('a', NULL, 'build', '/home/u/proj', 'Fix the parser', ?, ?,"
               " '{\"id\":\"qwen3\",\"providerID\":\"llama\"}', 100, 20, 5, 300, 40, 0.5, 12, 3)", (ms, ms + 600000))
    db.execute("INSERT INTO session_v2 VALUES ('b', 'a', 'explore', '/home/u/proj', 'child', ?, ?, 'qwen3',"
               " 10, 2, 0, 0, 1, 0, 0, 0)", (ms + 1000, ms + 61000))
    db.execute("INSERT INTO session_v2 VALUES ('c', NULL, 'build', 'not a path', 'x', ?, ?, 'm', 1, 1, 0, 0, 0,"
               " 0, 0, 0)", (ms, ms))
    for sid, offsets in (("a", (0, 60, 120, 3600)), ("b", (1, 31))):
        for off in offsets:
            db.execute("INSERT INTO session_message VALUES (?, ?, 'secret text')", (sid, ms + off * 1000))
    db.commit()
    db.close()


def hermes_db(path: Path):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE sessions (id TEXT, parent_session_id TEXT, cwd TEXT, title TEXT, started_at REAL,"
               " ended_at REAL, last_activity_at REAL, model TEXT, input_tokens INTEGER, output_tokens INTEGER,"
               " reasoning_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER,"
               " estimated_cost_usd REAL, actual_cost_usd REAL)")
    db.execute("CREATE TABLE messages (session_id TEXT, timestamp REAL, content TEXT)")
    db.execute("INSERT INTO sessions VALUES ('h1', NULL, '/srv/app', 'Deploy', ?, NULL, ?, 'opus', 50, 10, 2, 0, 5,"
               " 0.2, NULL)", (T0, T0 + 200))
    db.execute("INSERT INTO sessions VALUES ('h2', 'h1', '/srv/app', NULL, ?, NULL, ?, 'opus', 5, 1, 0, 0, 0,"
               " 0, NULL)", (T0 + 10, T0 + 20))
    for off in (0, 100, 200):
        db.execute("INSERT INTO messages VALUES ('h1', ?, 'secret')", (T0 + off,))
    db.commit()
    db.close()


class OpenCodeTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / "opencode.db"
        opencode_db(self.db)

    def test_numbers_per_session(self):
        sessions = harness_opencode.collect(self.db)
        self.assertEqual(set(sessions), {"a", "c"})          # the child run is not a session of its own
        a = sessions["a"]
        self.assertEqual(a.cwd, "/home/u/proj")
        self.assertEqual(a.tokens, {"qwen3": [100, 25, 300, 40]})   # reasoning counts as output
        self.assertEqual(a.active, 120)                      # the gap of an hour is not working time
        self.assertEqual((a.lines_added, a.lines_removed, a.cost), (12, 3, 0.5))
        self.assertEqual(a.subagents, {"explore": [1, 13, 30.0]})
        self.assertEqual(sessions["c"].cwd, "")              # not an absolute folder: unknown

    def test_projects_and_busiest_work_on_it(self):
        rows = claude_sessions.projects(list(harness_opencode.collect(self.db).values()))
        self.assertEqual(rows[0].cwd, "/home/u/proj")
        self.assertEqual(rows[0].subagent_runs, 1)
        self.assertIsNotNone(claude_sessions.busiest(list(harness_opencode.collect(self.db).values())))

    def test_missing_or_foreign_db(self):
        self.assertEqual(harness_opencode.collect(self.db.with_name("none.db")), {})
        bad = self.db.with_name("bad.db")
        bad.write_bytes(b"not sqlite")
        self.assertEqual(harness_opencode.collect(bad), {})

    def test_never_writes(self):
        before = self.db.read_bytes()
        harness_opencode.collect(self.db)
        self.assertEqual(self.db.read_bytes(), before)
        self.assertFalse(self.db.with_name("opencode.db-journal").exists())


class HermesTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / "state.db"
        hermes_db(self.db)

    def test_numbers_per_session(self):
        sessions = harness_hermes.collect(self.db)
        self.assertEqual(set(sessions), {"h1"})
        h1 = sessions["h1"]
        self.assertEqual(h1.cwd, "/srv/app")
        self.assertEqual(h1.tokens, {"opus": [50, 12, 0, 5]})
        self.assertEqual(h1.active, 200)
        self.assertEqual(h1.replies, 3)
        self.assertAlmostEqual(h1.cost, 0.2)
        self.assertEqual(h1.subagents["?"][0], 1)            # Hermes names no type for child sessions

    def test_without_messages_the_session_times_count(self):
        db = sqlite3.connect(self.db)
        db.execute("DELETE FROM messages")
        db.commit()
        db.close()
        h1 = harness_hermes.collect(self.db)["h1"]
        self.assertEqual((h1.first, h1.last), (T0, T0 + 200))


class RegistryTest(unittest.TestCase):
    def test_fixed_list(self):
        commands = [h.command for h in harnesses.all_harnesses()]
        self.assertEqual(len(commands), len(set(commands)))
        self.assertEqual(harnesses.get("opencode").name, "OpenCode")
        self.assertIsNone(harnesses.get("nope"))
        self.assertEqual({h.command for h in harnesses.with_sessions()}, {"claude", "hermes", "opencode"})

    def test_notes_name_the_harness(self):
        self.assertIn("Hermes", harnesses.get("hermes").sessions.note())
        self.assertIn("Claude Code", harnesses.get("claude").sessions.note())

    def test_adapters_read_no_titles(self):
        """Numbers only: the adapters' SQL never names a title or message text column."""
        for module in (harness_opencode, harness_hermes):
            source = Path(module.__file__).read_text()
            sql = " ".join(part for part in source.split('"') if "SELECT" in part or "FROM" in part)
            for column in ("title", "content", "data", "system_prompt"):
                self.assertNotIn(column, sql.lower().replace("metadata", ""), (module.__name__, column))


class EditionTest(unittest.TestCase):
    def setUp(self):
        edition._maximum = None
        self.addCleanup(setattr, edition, "_maximum", None)

    def test_without_session_topics_numbers_only(self):
        with mock.patch.dict(sys.modules, {"session_topics": None}):
            self.assertEqual(edition.maximum(), edition.NUMBERS)
            self.assertTrue(edition.allows(edition.NUMBERS))
            self.assertFalse(edition.allows(edition.TITLES))

    def test_asterism_allows_titles_and_summary(self):
        try:
            import session_topics  # noqa: F401
        except ImportError:
            self.skipTest("the AI Agents Map plugin ships without session_topics.py")
        self.assertTrue(edition.allows(edition.SUMMARY))


if __name__ == "__main__":
    unittest.main()
