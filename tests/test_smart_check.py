import datetime as dt
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import agent_details  # noqa: E402
import inventory  # noqa: E402
import mcp_updates  # noqa: E402
import smart_check  # noqa: E402


class SmartCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self.tmp.name) / "smart.json"

    def tearDown(self):
        self.tmp.cleanup()

    def setup_text(self):
        agent = types.SimpleNamespace(command="claude", name="Claude Code", installed=True, version="2.1.308")
        gone = types.SimpleNamespace(command="pi", name="Pi", installed=False, version="")
        info = agent_details.Details(model="opus", providers=[agent_details.Provider("Anthropic")])
        inv = inventory.Inventory(
            runtimes=[inventory.Runtime("ollama", "Ollama", installed=True, running=True, models=["a", "b"],
                                        agents=["hermes"])],
            mcp=[inventory.McpServer("photocraft", "command", "photocraft-cli", ["claude"])],
            agent_mcp={"claude": ["photocraft"]})
        report = types.SimpleNamespace(areas=[types.SimpleNamespace(findings=[
            types.SimpleNamespace(level="warn", title="OpenClaw: No model set up", text="Draw a line"),
            types.SimpleNamespace(level="ok", title="All good", text="")])])
        pc = types.SimpleNamespace(gpu="RX 7900", vendor="amd", vram=24 * 2**30, ram=64 * 2**30, cpus=32)
        library = {"photocraft": {"command": "/x/photocraft-cli", "env": {"API_KEY": "sk-secret-123"},
                                  "headers": {"Authorization": "Bearer abc"}}}
        found = [mcp_updates.Found("photocraft", "photocraft", "0.11.1", "0.12.0", "https://x.org", source="npm")]
        with mock.patch("mcp_library.load", lambda: library):
            return smart_check.snapshot([agent, gone], {"claude": info}, inv, report, pc, found)

    def test_snapshot_says_what_is_there_and_nothing_secret(self):
        text = self.setup_text()
        for part in ("RX 7900 with 24 GB", "Claude Code 2.1.308, model opus, providers: Anthropic, 1 MCP servers",
                     "Ollama: running, 2 models, used by hermes", "photocraft, 0.12.0 is out, used by 1 agents",
                     "problem: OpenClaw: No model set up (Draw a line)"):
            self.assertIn(part, text)
        for secret in ("sk-secret", "Bearer", "API_KEY", "Pi", "All good"):
            self.assertNotIn(secret, text)
        self.assertLessEqual(len(text), smart_check.MAX_SNAPSHOT)

    def test_only_answers_that_hold_up(self):
        answer = {"summary": "A strong\nsetup. " + "x" * 900, "findings": [
            {"level": "good", "title": "GPU fits", "detail": "24 GB"},
            {"level": "problem", "title": "OpenClaw has no model", "detail": "Draw a line to a local LLM."},
            {"level": "rm -rf", "title": "bad level", "detail": ""},
            {"level": "tip", "title": "openclaw HAS NO MODEL", "detail": "twice"},
            {"level": "tip", "title": "", "detail": "no title"},
            {"level": "tip", "title": "Update photocraft", "detail": "0.12.0 is out."}]}
        analysis = smart_check.validate(answer, "claude", now=dt.datetime(2026, 10, 22, 12))
        self.assertNotIn("\n", analysis.summary)
        self.assertLessEqual(len(analysis.summary), 700)
        self.assertEqual([(f.level, f.title) for f in analysis.findings],
                         [("problem", "OpenClaw has no model"), ("tip", "Update photocraft"), ("good", "GPU fits")])
        self.assertEqual(smart_check.result_text(analysis), "1 problems, 1 tips")
        with self.assertRaises(ValueError):
            smart_check.validate({"summary": " ", "findings": []}, "claude")

    def test_analyze_keeps_the_answer(self):
        def look_up(agent, task, prompt, schema, result_of=None):
            self.assertEqual(task, "setup-analysis")
            self.assertIn("PC: GPU", prompt)
            text = '```json\n{"summary": "Fine.", "findings": [{"level": "tip", "title": "T", "detail": "D"}]}\n```'
            self.assertEqual(result_of(text), "0 problems, 1 tips")
            return text
        analysis = smart_check.analyze("codex", "PC: GPU x", look_up=look_up, cache=self.cache)
        self.assertEqual(smart_check.last(self.cache), analysis)
        self.assertEqual(self.cache.stat().st_mode & 0o777, 0o600)

    def test_fix_prompt_is_ours(self):
        prompt = smart_check.fix_prompt(smart_check.Finding("problem", "OpenClaw has no model", "Draw a line."))
        self.assertIn('"OpenClaw has no model"', prompt)
        self.assertIn("Ask me before each step", prompt)

    def test_runs_grouped_by_plugin_and_task(self):
        now = dt.datetime(2026, 10, 22, 12)
        runs = [{"when": "2026-10-20T10:00:00", "plugin": "asterism", "task": "mcp-updates", "agent": "claude",
                 "ok": True, "input": 100, "output": 10},
                {"when": "2026-10-22T11:00:00", "plugin": "asterism", "task": "mcp-updates", "agent": "claude",
                 "ok": False},
                {"when": "2026-10-21T09:00:00", "plugin": "wlan-monitor", "task": "firmware", "agent": "codex",
                 "ok": True, "input": 5},
                {"when": "2026-08-01T09:00:00", "plugin": "asterism", "task": "old", "agent": "codex", "ok": True}]
        groups = smart_check.categories(runs, now=now)
        self.assertEqual([(g.plugin, g.task, g.totals.runs, g.totals.ok, g.totals.tokens) for g in groups],
                         [("asterism", "mcp-updates", 2, 1, 110), ("wlan-monitor", "firmware", 1, 1, 5)])
        self.assertEqual(groups[0].runs[0]["when"], "2026-10-22T11:00:00")     # newest first
        many = [{"when": "2026-10-22T10:00:00", "plugin": "p", "task": f"t{i}", "agent": "a", "ok": True}
                for i in range(500)]
        self.assertEqual(len(smart_check.categories(many, now=now)), smart_check.MAX_CATEGORIES)


if __name__ == "__main__":
    unittest.main()
