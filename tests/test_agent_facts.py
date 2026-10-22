"""Agents come from their adapters only: the core asks an agent's facts (home, activity, details,
security settings, research, rewrite, …) and never names one. A new agent is an adapter (or an
add-on), nothing in the core changes."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
import harnesses  # noqa: E402
from test_llm_engine import string_constants  # noqa: E402

# The libraries adapters pick from by name (readers, installers, MCP entry shapes, usage readers,
# host names), and the registry itself: these may name agents.
LIBRARIES = {"adapter_data.py", "agent_status.py", "install_ctl.py", "mcp_ctl.py", "traffic.py", "plugin_costs.py",
             "harnesses.py", "i18n.py", "edition.py",
             "plugin_ai.py"}     # vendored for every plugin: its defaults; the app passes its adapters' facts


class NoAgentNamesTest(unittest.TestCase):
    def test_the_core_names_no_agent(self):
        agents = harnesses.built_in_adapters()
        names = {h.command for h in agents} | {h.name for h in agents}
        for path in sorted(ROOT.glob("*.py")):
            if path.name in LIBRARIES or path.name.startswith("provider_"):
                continue
            with self.subTest(module=path.name):
                self.assertEqual(sorted(string_constants(path) & names), [])


class FactsTest(unittest.TestCase):
    """The adapters' facts say what the core's name tables said before."""

    def facts(self, name) -> dict:
        return {h.command: getattr(h, name) for h in harnesses.built_in_adapters() if getattr(h, name)}

    def test_same_as_the_old_tables(self):
        self.assertEqual(set(self.facts("web_search")), {"claude", "codex", "gemini", "grok", "copilot",
                                                         "cursor-agent"})
        self.assertEqual(set(self.facts("chat_app")), {"bionic"})
        self.assertEqual(self.facts("herdr"), {"cursor-agent": "cursor"})
        self.assertEqual(self.facts("program_dir"), {"hermes": "~/.hermes/hermes-agent"})
        self.assertEqual(self.facts("logout"), {"claude": ("claude", "auth", "logout"), "codex": ("codex", "logout")})
        self.assertEqual(set(self.facts("research")), {"claude", "codex"})
        self.assertEqual(set(self.facts("rewrite")), {"claude", "codex"})
        self.assertEqual(set(self.facts("models")), {"claude", "codex"})
        self.assertEqual(self.facts("sandbox_setting"), {"claude": "sandbox.enabled", "codex": "sandbox_mode",
                                                         "cursor-agent": "sandbox.mode"})
        self.assertEqual(self.facts("asks_setting"), {
            "claude": "permissions.defaultMode", "codex": "approval_policy", "copilot": "defaultPermissionMode",
            "cursor-agent": "approvalMode", "hermes": "approvals.mode", "opencode": "permission.bash",
            "pi": "defaultTools"})
        self.assertEqual(set(self.facts("details")), {"claude", "codex", "copilot", "cursor-agent", "opencode",
                                                      "pi", "omp", "hermes", "dsh", "bionic", "openclaw", "goose"})
        self.assertEqual(self.facts("home")["pi"], "~/.pi/agent")
        self.assertEqual(self.facts("tools_setting"), {"pi": ("~/.pi/agent/settings.json", "defaultTools"),
                                                       "omp": ("~/.omp/agent/settings.json", "defaultTools")})

    def test_commands_hold_the_placeholders(self):
        import agent_profiles
        import skill_update
        argv = agent_profiles.research_command("codex", "PROMPT", Path("/s.json"))
        self.assertEqual(argv[1:], ["--search", "exec", "--sandbox", "read-only", "--skip-git-repo-check",
                                    "--json", "--output-schema", "/s.json", "PROMPT"])
        self.assertEqual(agent_profiles.research_command("claude", "PROMPT", Path("/s.json"))[1:3], ["-p", "PROMPT"])
        self.assertEqual(skill_update.writer_command("codex", Path("/a.md"))[-2:], ["/a.md", "-"])
        self.assertTrue(skill_update.answers_in_file("codex"))
        self.assertFalse(skill_update.answers_in_file("claude"))

    def test_an_add_on_signed_before_gets_the_newer_facts(self):
        import dataclasses
        claude = next(h for h in harnesses.built_in_adapters() if h.command == "claude")
        defaults = {f.name: f.default for f in dataclasses.fields(harnesses.Harness)}
        old = dataclasses.replace(claude, **{n: defaults[n] for n in harnesses.NEWER_FACTS})   # as signed before
        self.assertEqual(old.home, "")
        filled = harnesses._with_newer_facts(old)
        self.assertEqual((filled.home, filled.logout, filled.sandbox_setting),
                         ("~/.claude", ("claude", "auth", "logout"), "sandbox.enabled"))
        self.assertIs(harnesses._with_newer_facts(claude), claude)

    def test_an_older_text_look_up_gives_way_to_one_that_says_its_tokens(self):
        import dataclasses
        claude = next(h for h in harnesses.built_in_adapters() if h.command == "claude")
        text_only = tuple(a if a != "json" else "text" for a in claude.research)
        old = dataclasses.replace(claude, research=text_only, research_output="text", task=())
        filled = harnesses._with_newer_facts(old)
        self.assertEqual((filled.research, filled.research_output), (claude.research, "claude-json"))
        self.assertEqual(filled.task, ("claude", "{prompt}"))


if __name__ == "__main__":
    unittest.main()
