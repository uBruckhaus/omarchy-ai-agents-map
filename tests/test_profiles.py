import datetime as dt
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import isolate  # noqa: E402
import agent_profiles  # noqa: E402
import agents  # noqa: E402
import connect  # noqa: E402
import mcp_ctl  # noqa: E402
import skills_ctl  # noqa: E402

ANSWER = {
    "skills": {"dirs": ["~/.deepseek/skills", "~/.agents/skills"], "own": "~/.deepseek/skills"},
    "mcp": {"file": "~/.deepseek/settings.json", "format": "json", "key": "tools.mcpServers",
            "type_field": "type", "stdio_type": "stdio", "http_type": "http", "url_field": "url",
            "env_field": "env", "headers_field": "headers", "command_list": False},
    "sources": ["https://example.com/docs/mcp", "javascript:alert(1)"],
    "notes": "From the docs.",
}


class ProfileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name).resolve()
        isolate.desktop_in(self, self.home)
        for module in (connect, mcp_ctl, skills_ctl, agent_profiles):
            patcher = mock.patch.object(module, "HOME", self.home)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name, value in (("PROFILE_DIR", self.home / "profiles"), ("WORK_DIR", self.home / "work")):
            patcher = mock.patch.object(agent_profiles, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        which = mock.patch("shutil.which", lambda name: f"/usr/bin/{name}")
        which.start()
        self.addCleanup(which.stop)
        self.agent = agents.Agent(command="deepseek", name="DeepSeek Harness", icon="", installed=True)

    def tearDown(self):
        self.tmp.cleanup()

    def fake_run(self, text, code=0):
        def run(argv, **_kw):
            self.argv = argv
            return (text if not code else "boom").encode(), code == 0
        return run

    def test_rules_for_web_answers(self):
        bad = dict(ANSWER, mcp=dict(ANSWER["mcp"], file="~/.ssh/config"),
                   skills={"dirs": ["/etc/skills", "~/../x", "~/.aws/skills"], "own": "~/.gnupg/s"})
        with self.assertRaises(ValueError):
            agent_profiles.validate(bad)
        odd = agent_profiles.validate(dict(ANSWER, mcp=dict(ANSWER["mcp"], type_field="rm -rf", url_field="x")))
        self.assertEqual((odd["mcp"]["type_field"], odd["mcp"]["url_field"]), ("", "url"))
        with self.assertRaises(ValueError):
            agent_profiles.validate(dict(ANSWER, skills=None, mcp=dict(ANSWER["mcp"], key="a b")))
        self.assertEqual(agent_profiles.validate(ANSWER)["sources"], ["https://example.com/docs/mcp"])

    def test_claude_only_gets_web_tools(self):
        run = self.fake_run("Here you go:\n```json\n" + json.dumps(ANSWER) + "\n```")
        result = agent_profiles.research(self.agent, {"claude", "codex"}, run=run)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(self.argv[0], "/usr/bin/claude")
        tools = self.argv[self.argv.index("--tools") + 1:self.argv.index("--allowedTools")]
        self.assertEqual(tools, ["WebSearch", "WebFetch"])
        self.assertFalse(agent_profiles.needs_research("deepseek"))

    def test_failure_is_kept_and_retried_later(self):
        result = agent_profiles.research(self.agent, {"codex"}, run=self.fake_run("", code=1))
        self.assertEqual(result["status"], "failed")
        self.assertFalse(agent_profiles.needs_research("deepseek"))
        later = dt.datetime.now() + dt.timedelta(days=8)
        self.assertTrue(agent_profiles.needs_research("deepseek", later))
        self.assertEqual(agent_profiles.research(self.agent, set())["status"], "failed")

    def test_new_agent_set_up_from_its_profile(self):
        agent_profiles.research(self.agent, {"claude"}, run=self.fake_run(json.dumps(ANSWER)))
        (self.home / ".pi/agent").mkdir(parents=True)
        (self.home / ".pi/agent/mcp.json").write_text(json.dumps(
            {"mcpServers": {"web": {"url": "https://mcp.example.com/mcp", "headers": {"X-Key": "abcd-1234"}}}}))
        result = mcp_ctl.plan("deepseek", "web", ["pi"])
        self.assertNotIn("abcd-1234", result.diff())
        connect.apply(result, self.home / "backups")
        data = json.loads((self.home / ".deepseek/settings.json").read_text())
        self.assertEqual(data["tools"]["mcpServers"]["web"],
                         {"type": "http", "url": "https://mcp.example.com/mcp", "headers": {"X-Key": "abcd-1234"}})
        self.assertIn("web", mcp_ctl.raw_entries("deepseek"))
        # Skills: it reads ~/.agents/skills by itself; another folder gets links in its own one.
        extra = self.home / "extra/one"
        extra.mkdir(parents=True)
        (extra / "SKILL.md").write_text("x")
        connect.apply(skills_ctl.plan("deepseek", str(extra.parent)), self.home / "backups")
        self.assertTrue((self.home / ".deepseek/skills/one").is_symlink())
        self.assertEqual(skills_ctl.plan("deepseek", "~/.agents/skills", remove=True).notes, ["~/.agents/skills"])


if __name__ == "__main__":
    unittest.main()
