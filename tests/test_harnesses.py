import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import claude_sessions  # noqa: E402
import edition  # noqa: E402
import adapter_data  # noqa: E402

# Their session readers are hooks now (adapters/<command>.py).
harness_hermes = adapter_data.hooks_module("hermes")
harness_opencode = adapter_data.hooks_module("opencode")
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
        sessions = harness_opencode.sessions(self.db)
        self.assertEqual(set(sessions), {"a", "c"})          # the child run is not a session of its own
        a = sessions["a"]
        self.assertEqual(a.cwd, "/home/u/proj")
        self.assertEqual(a.tokens, {"qwen3": [100, 25, 300, 40]})   # reasoning counts as output
        self.assertEqual(a.active, 120)                      # the gap of an hour is not working time
        self.assertEqual((a.lines_added, a.lines_removed, a.cost), (12, 3, 0.5))
        self.assertEqual(a.subagents, {"explore": [1, 13, 30.0]})
        self.assertEqual(sessions["c"].cwd, "")              # not an absolute folder: unknown

    def test_projects_and_busiest_work_on_it(self):
        rows = claude_sessions.projects(list(harness_opencode.sessions(self.db).values()))
        self.assertEqual(rows[0].cwd, "/home/u/proj")
        self.assertEqual(rows[0].subagent_runs, 1)
        self.assertIsNotNone(claude_sessions.busiest(list(harness_opencode.sessions(self.db).values())))

    def test_missing_or_foreign_db(self):
        self.assertEqual(harness_opencode.sessions(self.db.with_name("none.db")), {})
        bad = self.db.with_name("bad.db")
        bad.write_bytes(b"not sqlite")
        self.assertEqual(harness_opencode.sessions(bad), {})

    def test_never_writes(self):
        before = self.db.read_bytes()
        harness_opencode.sessions(self.db)
        self.assertEqual(self.db.read_bytes(), before)
        self.assertFalse(self.db.with_name("opencode.db-journal").exists())


class HermesTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = Path(tmp.name) / "state.db"
        hermes_db(self.db)

    def test_numbers_per_session(self):
        sessions = harness_hermes.sessions(self.db)
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
        h1 = harness_hermes.sessions(self.db)["h1"]
        self.assertEqual((h1.first, h1.last), (T0, T0 + 200))


class RegistryTest(unittest.TestCase):
    def test_fixed_list(self):
        commands = [h.command for h in harnesses.all_harnesses()]
        self.assertEqual(len(commands), len(set(commands)))
        self.assertEqual(harnesses.get("opencode").name, "OpenCode")
        self.assertIsNone(harnesses.get("nope"))
        self.assertEqual({h.command for h in harnesses.with_sessions()}, {"claude", "hermes", "opencode"})
        self.assertEqual(len(commands), 16)

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



class AgentTablesTest(unittest.TestCase):
    """agents.py builds its tables from the adapters; they equal what it had written out."""

    def test_same_as_before(self):
        import agents
        self.assertEqual([c for c, *_r in agents.FALLBACK],
                         ["claude", "codex", "copilot", "crush", "cursor-agent", "gemini", "grok", "hermes", "muse",
                          "omp", "openclaw", "opencode", "pi"])
        self.assertEqual([c for c, *_r in agents.EXTRA], ["dsh", "bionic", "agy"])
        self.assertEqual(agents.NAMES, {"claude": "Claude Code", "copilot": "GitHub Copilot", "gemini": "Gemini CLI",
                                        "omp": "Oh My Pi", "pi": "Pi"})
        self.assertEqual(agents.SPECS, {
            "omp": "github:can1357/oh-my-pi", "grok": "npm:@xai-official/grok",
            "muse": "http:muse[url=https://api.meta.ai/muse-launcher.sh,bin=muse]",
            "hermes": "pipx:hermes-agent[extras=all]"})
        self.assertEqual(agents.FALLBACK[0], ("claude", "Claude", "\U000f06c4", ""))
        self.assertEqual(agents.FALLBACK[1], ("codex", "Codex", "\ue905", "omarchy"))

    def test_every_agent_complete(self):
        import install_ctl
        for h in harnesses.all_harnesses():
            self.assertTrue(h.label and h.icon, h.command)
            if h.installer is not None:
                self.assertIs(type(install_ctl.AGENT_INSTALLERS[h.command]), type(h.installer()))
        self.assertEqual(set(install_ctl.AGENT_INSTALLERS), {"dsh", "bionic", "agy"})



class CapabilityTablesTest(unittest.TestCase):
    """The feature modules build their per-agent tables from the adapters; same values as before."""

    def test_security_status_connect(self):
        import agent_security
        import agent_status
        import connect
        self.assertEqual(set(agent_security.READERS),
                         {"claude", "codex", "copilot", "cursor-agent", "hermes", "opencode", "pi"})
        self.assertEqual(set(agent_status.READERS), {"hermes", "opencode", "pi"})
        self.assertEqual(agent_status.STATUS_COMMANDS,
                         {"cursor-agent": (("cursor-agent", "status"), "not logged in", "logged in")})
        self.assertEqual(set(agent_status.access_table()), {
            "claude", "codex", "gemini", "copilot", "cursor-agent", "opencode", "hermes", "pi", "dsh", "grok",
            "crush", "bionic"})
        self.assertEqual(set(connect.SUPPORTED), {"hermes", "pi", "opencode", "dsh"})
        self.assertEqual(connect.FIXED, {"bionic": "lmstudio"})

    def test_skills(self):
        import skills_ctl
        self.assertEqual(skills_ctl.OWN["codex"], "~/.codex/skills")
        self.assertEqual(skills_ctl.NATIVE["codex"], ["~/.agents/skills", "~/.codex/skills"])
        self.assertEqual(skills_ctl.NATIVE["bionic"], ["~/.lmstudio/skills"])
        self.assertEqual(set(skills_ctl.SETTINGS_LIST), {"pi", "hermes"})
        self.assertEqual(len(skills_ctl.NATIVE), 15)

    def test_mcp(self):
        import mcp_ctl
        self.assertEqual(mcp_ctl.SUPPORTED, ("claude", "codex", "copilot", "crush", "cursor-agent", "gemini", "grok",
                                             "hermes", "muse", "omp", "openclaw", "opencode", "pi", "dsh"))
        self.assertEqual(mcp_ctl.IN_APP, {"bionic": "Settings › Connected apps"})
        self.assertEqual(mcp_ctl.paths_for("claude"), [])
        self.assertEqual(mcp_ctl.paths_for("codex"), [Path.home() / ".codex/config.toml"])
        self.assertEqual(mcp_ctl.paths_for("lmstudio"), [Path.home() / ".lmstudio/mcp.json"])
        shapes = {h.mcp.shape for h in harnesses.all_harnesses() if h.mcp and not h.mcp.in_app}
        self.assertEqual(shapes, {"plain", "claude", "codex", "copilot", "crush", "gemini", "muse", "omp",
                                  "openclaw", "opencode"})
        own = {h.command for h in harnesses.all_harnesses() if h.mcp and (h.mcp.read or h.mcp.write or h.mcp.native)}
        self.assertEqual(own, {"dsh", "muse", "openclaw"})

    def test_short_model_flag(self):
        import provider_watch
        self.assertEqual({h.command for h in harnesses.all_harnesses() if h.short_model_flag},
                         {"codex", "opencode", "hermes", "crush"})
        self.assertEqual(provider_watch.instance_model("codex", ["-m", "gpt-5"]), "gpt-5")
        self.assertEqual(provider_watch.instance_model("claude", ["-m", "x"]), "")

    def test_update_whitelist_unchanged(self):
        import agent_updates
        self.assertEqual(agent_updates.self_updates(), (["claude", "update"],))
        self.assertTrue(agent_updates._allowed(["claude", "update"]))
        self.assertTrue(agent_updates._allowed(["mise", "upgrade", "x"]))
        self.assertFalse(agent_updates._allowed(["claude", "mcp", "add"]))
        self.assertFalse(agent_updates._allowed(["rm", "-rf"]))


class EditionTest(unittest.TestCase):
    def test_without_add_ons_numbers_only(self):
        with mock.patch.dict(sys.modules, {"addon_api": None}):          # the plugin: no add-on loader
            self.assertEqual(edition.maximum(), edition.NUMBERS)
            self.assertFalse(edition.allows(edition.TITLES))

    def test_follows_loaded_add_ons(self):
        try:
            import addon_api
        except ImportError:
            self.skipTest("the AI Agents Map plugin has no add-ons")
        with mock.patch.object(addon_api, "loaded_privacy", return_value=edition.SUMMARY):
            self.assertTrue(edition.allows(edition.SUMMARY))
        with mock.patch.object(addon_api, "loaded_privacy", return_value=0):
            self.assertEqual(edition.maximum(), edition.NUMBERS)


class ExtendTest(unittest.TestCase):
    """harnesses.extend: facts an extension add-on adds to an agent."""

    def tearDown(self):
        harnesses.remove_added()

    def test_empty_fields_only_first_wins(self):
        reader = lambda: ("Pro", [])          # noqa: E731
        other = lambda: ("Max", [])           # noqa: E731
        self.assertEqual(harnesses.extend("opencode", {"limits": reader}, "ext-a"), [])
        harnesses.extend("opencode", {"limits": other}, "ext-b")
        self.assertIs(harnesses.get("opencode").limits, reader)
        self.assertEqual(harnesses.conflicts(), {"ext-b": ["opencode.limits"]})

    def test_a_set_fact_is_never_replaced(self):
        mine = harnesses.get("opencode").usage
        harnesses.extend("opencode", {"usage": lambda: None}, "ext-a")
        self.assertIs(harnesses.get("opencode").usage, mine)
        self.assertEqual(harnesses.conflicts(), {"ext-a": ["opencode.usage"]})

    def test_refused_fields(self):
        self.assertEqual(sorted(harnesses.extend("opencode", {"installer": 1, "command": "x"}, "ext-a")),
                         ["command", "installer"])
        self.assertEqual(harnesses.get("opencode").command, "opencode")

    def test_removing_the_addon_removes_its_facts(self):
        harnesses.extend("opencode", {"limits": lambda: ("x", [])}, "ext-a")
        harnesses.remove_added("ext-a")
        self.assertIsNone(harnesses.get("opencode").limits)


if __name__ == "__main__":
    unittest.main()
