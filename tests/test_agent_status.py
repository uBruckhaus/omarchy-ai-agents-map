import datetime as dt
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import agent_details as ad  # noqa: E402
import agent_status as st  # noqa: E402
import agents  # noqa: E402
import inventory as invmod  # noqa: E402

TODAY = dt.date(2026, 10, 16)


def at(day: dt.date, hour: int = 12) -> float:
    return dt.datetime(day.year, day.month, day.day, hour).timestamp()


class UsageTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())

    def test_omarchy_record(self):
        record = {"hasLocalStats": True, "totalSessions": 91, "todaySessions": 3, "todayPrompts": 5,
                  "recentDays": [{"date": "2026-10-15", "messageCount": 100}, {"date": "2026-10-16", "messageCount": 40},
                                 {"date": "bad"}],
                  "modelUsage": {"opus": {"inputTokens": 1, "outputTokens": 2, "cacheReadInputTokens": 3,
                                          "cacheCreationInputTokens": 4}}}
        usage = st.from_record(record, TODAY)
        self.assertEqual(len(usage.days), 7)
        self.assertEqual(usage.today_tokens, 40)
        self.assertEqual(usage.week_tokens, 140)
        self.assertEqual(usage.by_model, {"opus": [1, 2, 7]})
        self.assertEqual((usage.sessions_total, usage.sessions_today), (91, 3))
        self.assertIsNone(st.from_record({"hasLocalStats": False}, TODAY))

    def test_hermes_numbers_only(self):
        db_path = self.home / ".hermes/state.db"
        db_path.parent.mkdir()
        db = sqlite3.connect(db_path)
        db.execute("CREATE TABLE sessions (id TEXT, started_at REAL, model TEXT, input_tokens INT, output_tokens INT,"
                   " cache_read_tokens INT, cache_write_tokens INT, estimated_cost_usd REAL, system_prompt TEXT)")
        db.execute("INSERT INTO sessions VALUES ('a', ?, 'gemma', 100, 10, 5, 0, 0.5, 'secret prompt')", (at(TODAY),))
        db.execute("INSERT INTO sessions VALUES ('b', ?, 'qwen', 50, 5, 0, 0, NULL, '')", (at(TODAY - dt.timedelta(days=2)),))
        db.execute("INSERT INTO sessions VALUES ('c', ?, 'qwen', 9, 9, 0, 0, NULL, '')", (at(TODAY - dt.timedelta(days=30)),))
        db.commit()
        db.close()
        usage = st.hermes(TODAY, self.home)
        self.assertEqual(usage.today_tokens, 115)
        self.assertEqual(usage.week_tokens, 170)
        self.assertEqual((usage.sessions_week, usage.sessions_total, usage.sessions_today), (2, 3, 1))
        self.assertEqual(usage.by_model["qwen"], [59, 14, 0])
        self.assertAlmostEqual(usage.cost, 0.5)
        # Read-only: nothing new appears beside the database.
        self.assertEqual(sorted(p.name for p in db_path.parent.iterdir()), ["state.db"])

    def test_opencode_model_json(self):
        db_path = self.home / ".local/share/opencode/opencode.db"
        db_path.parent.mkdir(parents=True)
        db = sqlite3.connect(db_path)
        db.execute("CREATE TABLE session_v2 (id TEXT, parent_id TEXT, model TEXT, tokens_input INT, tokens_output INT,"
                   " tokens_cache_read INT, tokens_cache_write INT, cost REAL, time_created INT)")
        db.execute("INSERT INTO session_v2 VALUES ('a', NULL, '{\"id\":\"qwen\",\"providerID\":\"llama\"}', 10, 1, 0, 0, 0, ?)",
                   (int(at(TODAY) * 1000),))
        db.execute("INSERT INTO session_v2 VALUES ('b', 'a', NULL, 99, 99, 0, 0, 0, ?)", (int(at(TODAY) * 1000),))
        db.commit()
        db.close()
        usage = st.opencode(TODAY, self.home)
        self.assertEqual(usage.by_model, {"qwen": [10, 1, 0]})
        self.assertEqual(usage.sessions_total, 1)          # sub-sessions are part of their parent

    def test_pi_counts_files_never_opens_them(self):
        folder = self.home / ".pi/agent/sessions/--home--"
        folder.mkdir(parents=True)
        for name in ("2026-10-16T08-00-00-000Z_a.jsonl", "2026-10-14T08-00-00-000Z_b.jsonl"):
            path = folder / name
            path.write_text("x")
            path.chmod(0)                                   # unreadable: counting must still work
        usage = st.pi(TODAY, self.home)
        self.assertEqual((usage.sessions_total, usage.sessions_week), (2, 2))
        self.assertFalse(usage.has_tokens)

    def test_broken_or_missing_store(self):
        self.assertIsNone(st.hermes(TODAY, self.home))
        bad = self.home / ".hermes/state.db"
        bad.parent.mkdir()
        bad.write_bytes(b"not a database")
        self.assertIsNone(st.hermes(TODAY, self.home))


class ChecksTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.inv = invmod.Inventory(runtimes=[invmod.Runtime("llamacpp", "llama.cpp", True, False, "llama-server",
                                                             models=[], agents=["pi"])])

    def test_credential_file_only_stat(self):
        access = st.access_table()["claude"]
        self.assertFalse(st.credentials_present(access, self.home, {}))
        secret = self.home / ".claude/.credentials.json"
        secret.parent.mkdir()
        secret.write_text("{}")
        secret.chmod(0)                                     # never opened, so unreadable is fine
        self.assertTrue(st.credentials_present(access, self.home, {}))
        self.assertTrue(st.credentials_present(access, Path("/nonexistent"), {"ANTHROPIC_API_KEY": "x"}))

    def test_not_signed_in_says_how(self):
        claude = agents.Agent(command="claude", name="Claude Code", icon="x", installed=True)
        checks = st.agent_checks(claude, ad.Details(model="opus"), {}, self.inv, {}, root=self.home, environ={})
        missing = [c for c in checks if c.state == "missing"]
        self.assertEqual(missing[0].title, "Not signed in")
        self.assertIn("claude auth login", missing[0].step)
        self.assertEqual(st.summary_line(checks)[0], "missing")

    def test_local_agent_needs_models(self):
        pi = agents.Agent(command="pi", name="Pi", icon="x", installed=True)
        info = ad.Details(model="qwen (local)", providers=[ad.Provider("local", True, "llama.cpp", 0)])
        checks = st.agent_checks(pi, info, {}, self.inv, {}, root=self.home, environ={})
        titles = {c.title: c for c in checks}
        self.assertEqual(titles["No account needed"].state, "ok")
        self.assertEqual(titles["llama.cpp has no models"].state, "missing")

    def test_failed_link_shows_up(self):
        pi = agents.Agent(command="pi", name="Pi", icon="x", installed=True)
        hints = {"agent:pi|mcp:photo": {"kind": "mcp", "hints": [{"level": "error", "text": "handshake failed"}]}}
        checks = st.agent_checks(pi, ad.Details(), {}, self.inv, hints, root=self.home, environ={})
        bad = [c for c in checks if "photo" in c.title]
        self.assertEqual(bad[0].state, "missing")
        self.assertEqual(bad[0].detail, "handshake failed")

    def test_runtime(self):
        rt = self.inv.runtimes[0]
        checks = st.runtime_checks(rt, {}, {"pi": "Pi"}, False)
        self.assertEqual([c.state for c in checks if c.title == "No models"], ["missing"])
        self.assertEqual(st.runtime_checks(invmod.Runtime("jan", "Jan"), {}, {}, None)[0].title, "Not installed")


class IssueTest(unittest.TestCase):
    def test_remember_show_forget(self):
        path = Path(tempfile.mkdtemp()) / "state/start-issues.json"
        st.remember_issue("claude", "vte4", path)
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
        issue = st.load_issues(path)["claude"]
        claude = agents.Agent(command="claude", name="Claude Code", icon="x", installed=True)
        checks = st.agent_checks(claude, ad.Details(model="opus"), {"ready": True, "tierLabel": "Pro"},
                                 invmod.Inventory(), {}, issue=issue)
        warn = next(c for c in checks if c.state == "warn")
        self.assertEqual(warn.command, "sudo pacman -S vte4")
        self.assertTrue(st.forget_issue("claude", path))
        self.assertEqual(st.load_issues(path), {})
        self.assertFalse(st.forget_issue("claude", path))
        path.write_text('{"x": {"kind": "rm -rf"}, "y": "bad"}')
        self.assertEqual(st.load_issues(path), {})         # only known kinds are shown


if __name__ == "__main__":
    unittest.main()
