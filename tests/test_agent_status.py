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


class ClaudeUsageTest(unittest.TestCase):
    """Claude Code's own session files when Omarchy keeps no record (Asterism on any desktop)."""

    def test_days_models_and_sessions(self):
        from claude_sessions import Session
        sessions = [
            Session(first=at(TODAY, 9), last=at(TODAY, 10), tokens={"claude-opus-4": [100, 50, 1000, 10],
                                                                   "claude-haiku-4": [5, 5, 0, 0]}, cost=1.5),
            Session(first=at(TODAY - dt.timedelta(days=2)), last=at(TODAY - dt.timedelta(days=2), 13),
                    tokens={"claude-opus-4": [10, 10, 0, 0]}),
            Session(first=at(TODAY - dt.timedelta(days=30)), last=at(TODAY - dt.timedelta(days=30)),
                    tokens={"claude-opus-4": [1, 1, 0, 0]}),
        ]
        usage = st.claude(TODAY, sessions)
        self.assertEqual(usage.today_tokens, 100 + 50 + 10 + 10)
        self.assertEqual(usage.week_tokens, 170 + 20)
        self.assertEqual((usage.sessions_today, usage.sessions_week, usage.sessions_total), (1, 2, 3))
        self.assertEqual(usage.by_model["claude-opus-4"], [111, 61, 10])
        self.assertEqual(usage.cost, 1.5)
        self.assertEqual(usage.source, "Claude Code")

    def test_no_sessions_no_card(self):
        self.assertIsNone(st.claude(TODAY, []))

    def test_derived_from_sessions_for_any_agent_without_a_reader(self):
        from claude_sessions import Session
        sessions = {"a": Session(first=at(TODAY), last=at(TODAY), tokens={"m": [1, 2, 0, 3]}, cost=0.5)}
        usage = st.usage_for("claude", {}, sessions)          # Claude Code has no reader of its own now
        self.assertEqual(usage.source, "Claude Code")
        self.assertTrue(usage.api_value)
        self.assertEqual(usage.sessions_total, 1)

    def test_record_and_own_reader_come_first(self):
        record = {"hasLocalStats": True, "recentDays": [{"date": TODAY.isoformat(), "messageCount": 5}]}
        found = st.usage_for("claude", record, {})
        self.assertIsNotNone(found)
        self.assertNotEqual(found.source, "Claude Code")


class LimitsFactTest(unittest.TestCase):
    """A harness's limits() fact (an adapter's or an extension's) when the desktop has no record."""

    def tearDown(self):
        import harnesses
        harnesses.remove_added()

    def test_used_only_without_record_limits(self):
        import harnesses
        resets = dt.datetime(2026, 10, 20, tzinfo=dt.timezone.utc)
        harnesses.extend("opencode", {"limits": lambda: ("Plus", [ad.Limit("5-hour", 0.3, resets), "junk"])}, "ext")
        with tempfile.TemporaryDirectory() as tmp:
            details = ad.details_for("opencode", Path(tmp))
        self.assertEqual(details.plan, "Plus")
        self.assertEqual([(x.label, x.percent) for x in details.limits], [("5-hour", 0.3)])


class RuntimeUsageTest(unittest.TestCase):
    """A local LLM's usage, derived from the agents' sessions."""

    def test_tokens_through_this_llm_only(self):
        from claude_sessions import Session
        sessions = {
            "Hermes": [Session(first=at(TODAY), last=at(TODAY), tokens={"Qwen3.8-27B-Q4_K_M": [10, 5, 0, 1]})],
            "OpenCode": [Session(first=at(TODAY), last=at(TODAY), tokens={"llama-local/Qwen3.8-27B-Q4_K_M": [1, 1, 0, 0],
                                                                         "lms-local/Qwen3.8-27B-Q4_K_M": [7, 7, 0, 0],
                                                                         "claude-opus": [100, 100, 0, 0]})],
        }
        usage = st.runtime_usage(["Qwen3.8-27B-Q4_K_M.gguf"], sessions, "llama-local", TODAY)
        self.assertEqual(usage.today_tokens, 16 + 2)
        self.assertEqual(sorted(usage.by_model), ["Hermes · Qwen3.8-27B-Q4_K_M", "OpenCode · llama-local/Qwen3.8-27B-Q4_K_M"])
        self.assertEqual(usage.sessions_today, 2)

    def test_nothing_without_matching_models(self):
        self.assertIsNone(st.runtime_usage([], {"Hermes": []}))
        self.assertIsNone(st.runtime_usage(["x"], {"Hermes": []}))


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

    def test_a_sign_in_the_service_turns_down_says_expired(self):
        """Omarchy's record waits for auth while Codex's sign-in file is there (an expired token,
        a changed plan): not "Signed in", not paid per token, and a button to sign in again."""
        codex = agents.Agent(command="codex", name="Codex", icon="x", installed=True)
        (self.home / ".codex").mkdir()
        (self.home / ".codex/auth.json").write_text("{}")
        record = {"ready": True, "tierLabel": "", "usageStatusText": "Waiting for auth", "limits": []}
        checks = st.agent_checks(codex, ad.Details(), record, self.inv, {}, root=self.home, environ={})
        expired = next(c for c in checks if c.title == "Sign-in expired")
        self.assertEqual(expired.state, "missing")
        self.assertEqual(expired.sign_in, "codex")
        self.assertIn("codex login", expired.command)
        self.assertNotIn("Signed in", [c.title for c in checks])
        details = ad.Details(has_usage=True, usage_note="Waiting for auth")
        self.assertFalse(st.pays_per_token("codex", details, environ={}))

    def test_every_collectors_words_for_a_sign_in(self):
        """Claude's and Grok's "Sign-in expired" too; "Limits paused" needs no new sign-in."""
        claude = agents.Agent(command="claude", name="Claude Code", icon="x", installed=True)
        record = {"ready": True, "tierLabel": "Max 5x", "usageStatusText": "Sign-in expired"}
        checks = st.agent_checks(claude, ad.Details(model="opus"), record, self.inv, {}, root=self.home, environ={})
        self.assertEqual(next(c for c in checks if c.title == "Sign-in expired").sign_in, "claude")
        record["usageStatusText"] = "Limits paused"
        checks = st.agent_checks(claude, ad.Details(model="opus"), record, self.inv, {}, root=self.home, environ={})
        titles = [c.title for c in checks]
        self.assertIn("Signed in", titles)
        self.assertIn("Limits paused", titles)
        self.assertNotIn("Sign-in expired", titles)

    def test_not_signed_in_offers_the_sign_in_button(self):
        claude = agents.Agent(command="claude", name="Claude Code", icon="x", installed=True)
        checks = st.agent_checks(claude, ad.Details(model="opus"), {}, self.inv, {}, root=self.home, environ={})
        self.assertEqual(next(c for c in checks if c.title == "Not signed in").sign_in, "claude")

    def test_waiting_for_auth_without_any_sign_in_is_not_signed_in(self):
        codex = agents.Agent(command="codex", name="Codex", icon="x", installed=True)
        record = {"ready": False, "usageStatusText": "Waiting for auth", "authHelpText": "Run `codex login`."}
        checks = st.agent_checks(codex, ad.Details(), record, self.inv, {}, root=self.home, environ={})
        self.assertIn("Not signed in", [c.title for c in checks])
        self.assertNotIn("Sign-in expired", [c.title for c in checks])

    def test_an_agent_without_a_model_service_needs_one_set_up(self):
        # OpenClaw brings no model service of its own: with nothing in its settings it cannot answer.
        claw = agents.Agent(command="openclaw", name="OpenClaw", icon="x", installed=True)
        checks = st.agent_checks(claw, ad.Details(), {}, self.inv, {}, root=self.home, environ={})
        missing = [c for c in checks if c.state == "missing"]
        self.assertEqual(missing[0].title, "No model set up")
        self.assertIn("openclaw onboard", missing[0].step)
        self.assertNotIn("Model", {c.title for c in checks})            # said once, with what to do
        # Set up (a local LLM in its settings): no longer.
        info = ad.Details(model="gemma (llama-local)", providers=[ad.Provider("llama-local", True, "llama.cpp", 1)])
        checks = st.agent_checks(claw, info, {}, self.inv, {}, root=self.home, environ={})
        self.assertNotIn("No model set up", {c.title for c in checks})

    def test_local_agent_needs_models(self):
        pi = agents.Agent(command="pi", name="Pi", icon="x", installed=True)
        info = ad.Details(model="qwen (local)", providers=[ad.Provider("local", True, "llama.cpp", 0)])
        checks = st.agent_checks(pi, info, {}, self.inv, {}, root=self.home, environ={})
        titles = {c.title: c for c in checks}
        self.assertEqual(titles["No account needed"].state, "ok")
        self.assertEqual(titles["llama.cpp has no models"].state, "missing")

    def test_a_provider_not_in_use_is_no_problem(self):
        """Hermes uses llama.cpp; Ollama is only set up (a line it could switch to): Ollama without
        models is a note, not a problem, and the agent shows no problem."""
        hermes = agents.Agent(command="hermes", name="Hermes", icon="x", installed=True)
        inv = invmod.Inventory(runtimes=[
            invmod.Runtime("llamacpp", "llama.cpp", True, True, "llama-server", models=["gemma"]),
            invmod.Runtime("ollama", "Ollama", True, False, "ollama", models=[])])
        info = ad.Details(model="gemma (llama-local)", providers=[ad.Provider("llama-local", True, "llama.cpp", 1),
                                                                    ad.Provider("ollama-local", True, "Ollama", 0,
                                                                                active=False)])
        checks = st.agent_checks(hermes, info, {}, inv, {}, root=self.home, environ={})
        titles = {c.title: c for c in checks}
        self.assertEqual(titles["Ollama has no models"].state, "info")
        self.assertIn("Not in use", titles["Ollama has no models"].detail)
        self.assertNotEqual(st.summary_line(checks)[0], "missing")
        # The one it uses still counts.
        info.providers[0] = ad.Provider("llama-local", True, "llama.cpp", 1)
        inv.runtimes[0].models = []
        checks = st.agent_checks(hermes, info, {}, inv, {}, root=self.home, environ={})
        self.assertEqual({c.title: c for c in checks}["llama.cpp has no models"].state, "missing")

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


class PaidPerTokenTest(unittest.TestCase):
    """An agent paid per token shows today's cost where a plan's battery would be."""

    def details(self, limits=(), plan="", has_usage=False):
        info = ad.Details()
        info.limits, info.plan, info.has_usage = list(limits), plan, has_usage
        return info

    def test_who_pays_per_token(self):
        limit = ad.Limit(label="5 hours", percent=0.3)
        with_key = {"ANTHROPIC_API_KEY": "x"}
        self.assertFalse(st.pays_per_token("claude", self.details([limit]), with_key))     # a plan's battery
        self.assertTrue(st.pays_per_token("claude", self.details(), with_key))             # an API key
        self.assertTrue(st.pays_per_token("claude", self.details(has_usage=True), {}))     # record, no plan
        self.assertFalse(st.pays_per_token("claude", self.details(plan="Max 5x", has_usage=True), {}))
        self.assertFalse(st.pays_per_token("claude", self.details(), {}))                  # nothing known
        self.assertFalse(st.pays_per_token("claude", None, with_key))

    def test_cost_of_the_sessions_active_today(self):
        today = dt.date(2026, 10, 23)
        noon = dt.datetime(2026, 10, 23, 12).timestamp()
        yesterday = dt.datetime(2026, 10, 22, 12).timestamp()
        rows = [(noon, "opus", 10, 20, 0, 1.25), (noon, "opus", 1, 2, 0, 0.75), (yesterday, "opus", 5, 5, 0, 9.0)]
        usage = st._from_rows(rows, today, "test")
        self.assertAlmostEqual(usage.cost_today, 2.0)
        self.assertAlmostEqual(usage.cost, 11.0)
        self.assertIsNone(st._from_rows([(noon, "x", 1, 1, 0, 0)], today, "t").cost_today)   # no costs kept


class CostsTest(unittest.TestCase):
    """Today, 7 and 30 days, per model split by kind; local models apart, as no API cost."""

    def test_days_models_and_local_models(self):
        from types import SimpleNamespace
        from unittest import mock
        today = dt.date(2026, 10, 23)
        at = lambda days: dt.datetime(2026, 10, 23 - days, 12).timestamp() if days < 23 else \
            (dt.datetime(2026, 10, 23, 12) - dt.timedelta(days=days)).timestamp()
        opus = "claude-opus-5-5"
        sessions = {
            "a": SimpleNamespace(first=at(0), last=at(0), tokens={opus: [1_000_000, 0, 0, 0]}),           # $4 today
            "b": SimpleNamespace(first=at(3), last=at(3), tokens={opus: [0, 1_000_000, 0, 0]}),           # $20 3 days ago
            "c": SimpleNamespace(first=at(20), last=at(20), tokens={opus: [0, 0, 10_000_000, 0], "qwen-local": [5, 5, 0, 0],
                                                                 "unknown": [0, 0, 0, 0]}),             # empty: not listed
            "d": SimpleNamespace(first=at(40), last=at(40), tokens={opus: [10 ** 9, 0, 0, 0]}),          # too old
        }
        harness = SimpleNamespace(sessions=SimpleNamespace(collect=lambda: sessions))
        with mock.patch.object(st.harnesses, "get", return_value=harness):
            spent = st.costs("claude", today)
        self.assertAlmostEqual(spent.today, 4)
        self.assertAlmostEqual(spent.week, 24)
        self.assertAlmostEqual(spent.month, 26)
        provider, counts, parts = spent.by_model[opus]
        self.assertEqual((provider, counts), ("anthropic", [1_000_000, 1_000_000, 10_000_000, 0]))
        self.assertEqual([round(x, 6) for x in parts], [4, 20, 2, 0])
        self.assertEqual(spent.unpriced, {"qwen-local": 10})
        with mock.patch.object(st.harnesses, "get", return_value=harness):
            self.assertAlmostEqual(st.cost_today("claude", today), 4)

    def test_no_prices_no_costs(self):
        from types import SimpleNamespace
        from unittest import mock
        local = {"x": SimpleNamespace(first=1.8e9, last=1.8e9, tokens={"qwen-local": [5, 5, 0, 0]})}
        harness = SimpleNamespace(sessions=SimpleNamespace(collect=lambda: local))
        with mock.patch.object(st.harnesses, "get", return_value=harness):
            self.assertIsNone(st.costs("hermes"))


class PerTokenAccessTest(unittest.TestCase):
    """An agent that signs in with API keys only (access.per_token) is paid per token once signed in."""

    def test_signed_in_with_api_keys_only(self):
        from unittest import mock
        import tempfile
        from types import SimpleNamespace
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        access = st.Access("API key", files=(".agent/.credentials.yaml",), env=("AGENT_KEY",), per_token=True)
        harness = SimpleNamespace(access=lambda: access)
        info = ad.Details()
        with mock.patch.object(st.harnesses, "get", return_value=harness), mock.patch.object(st, "HOME", Path(home.name)):
            self.assertFalse(st.pays_per_token("agent", info, {}))                # not signed in
            (Path(home.name) / ".agent").mkdir()
            (Path(home.name) / ".agent/.credentials.yaml").write_text("x")
            self.assertTrue(st.pays_per_token("agent", info, {}))                 # its file is there
            plain = st.Access("plan or key", files=(".agent/.credentials.yaml",))
            harness.access = lambda: plain
            self.assertFalse(st.pays_per_token("agent", info, {}))                # could be a plan
