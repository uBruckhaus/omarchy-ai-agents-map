import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import agent_details as ad  # noqa: E402


class DetailsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        patcher = mock.patch.object(ad, "_expand", lambda p: Path(p.replace("~", str(self.home), 1)))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def write(self, rel, text):
        path = self.home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def test_local_provider_by_port(self):
        p = ad.provider_for_url("llama-local", "http://127.0.0.1:8080/v1", 3)
        self.assertTrue(p.local)
        self.assertEqual(p.runtime, "llama.cpp")
        self.assertEqual(p.models, 3)
        self.assertFalse(ad.provider_for_url("cloud", "https://api.example.com/v1").local)
        self.assertFalse(ad.provider_for_url("bad", "http://[::1").local)

    def test_pi_providers_and_model(self):
        self.write(".pi/agent/settings.json", json.dumps({"defaultProvider": "ollama-local", "defaultModel": "qwen"}))
        self.write(".pi/agent/models.json", json.dumps({"providers": {
            "ollama-local": {"baseUrl": "http://localhost:11434/v1", "apiKey": "secret", "models": [{"id": "a"}, {"id": "b"}]}}}))
        d = ad.details_for("pi", usage_dir=self.home / "none")
        self.assertEqual(d.model, "qwen (ollama-local)")
        self.assertEqual([(p.name, p.runtime, p.models) for p in d.providers], [("ollama-local", "Ollama", 2)])
        self.assertNotIn("secret", repr(d))

    def test_openclaw_model_and_providers(self):
        d = ad.details_for("openclaw", usage_dir=self.home / "none")
        self.assertEqual((d.model, d.providers), ("", []))              # not set up: nothing made up
        self.write(".openclaw/openclaw.json", json.dumps({
            "agents": {"defaults": {"model": {"primary": "llama-local/gemma", "fallbacks": ["anthropic/x"]}}},
            "models": {"providers": {"llama-local": {"baseUrl": "http://127.0.0.1:8080/v1", "apiKey": "secret",
                                                     "models": [{"id": "gemma"}]}}}}))
        d = ad.details_for("openclaw", usage_dir=self.home / "none")
        self.assertEqual(d.model, "gemma (llama-local)")
        self.assertEqual([(p.name, p.runtime, p.active) for p in d.providers], [("llama-local", "llama.cpp", True)])
        self.assertNotIn("secret", repr(d))
        # Its own local providers need no entry (their usual address).
        self.write(".openclaw/openclaw.json", json.dumps({"agents": {"defaults": {"model": "ollama/qwen3"}}}))
        d = ad.details_for("openclaw", usage_dir=self.home / "none")
        self.assertEqual((d.model, [p.runtime for p in d.providers]), ("qwen3 (ollama)", ["Ollama"]))

    def test_opencode_jsonc(self):
        self.write(".config/opencode/opencode.json", '{\n // comment\n "model": "anthropic/claude", /* x */\n'
                   ' "provider": {"lm": {"options": {"baseURL": "http://127.0.0.1:1234/v1"}, "models": {"m": {}},},},\n}')
        d = ad.details_for("opencode", usage_dir=self.home / "none")
        self.assertEqual(d.model, "anthropic/claude")
        self.assertEqual(d.providers[0].runtime, "LM Studio")

    def test_hermes_yaml_block(self):
        self.write(".hermes/config.yaml", "model:\n  default: some/model\n  provider: nous\n  base_url: ''\n"
                   "other:\n  provider: x\n")
        d = ad.details_for("hermes", usage_dir=self.home / "none")
        self.assertEqual(d.model, "some/model (nous)")
        self.assertEqual(d.providers, [])

    def test_usage_record(self):
        usage = self.home / "usage"
        usage.mkdir()
        (usage / "claude.json").write_text(json.dumps({
            "ready": True, "tierLabel": "Pro", "todayPrompts": 5, "todayTotalTokens": 1200, "todaySessions": 2,
            "limits": [{"label": "Session", "percent": 1.7, "resetsAt": "2026-10-13T12:30:00+00:00"},
                       {"label": "Bad", "percent": "x"}],
            "updatedAt": "2026-10-13T10:00:00"}))
        d = ad.details_for("claude", usage_dir=usage)
        self.assertEqual(d.plan, "Pro")
        self.assertEqual((d.today_prompts, d.today_tokens), (5, 1200))
        self.assertEqual(len(d.limits), 1)
        self.assertEqual(d.limits[0].percent, 1.0)
        self.assertIsNotNone(d.usage_updated.tzinfo)

    def test_usage_stamps_follow_the_records(self):
        """The map's poll reads a record again when its time changes; other files are left out."""
        folder = self.home / "usage"
        folder.mkdir()
        (folder / "codex.json").write_text("{}")
        (folder / "notes.txt").write_text("x")
        (folder / "Bad Name.json").write_text("{}")
        first = ad.usage_stamps(folder)
        self.assertEqual(list(first), ["codex"])
        os.utime(folder / "codex.json", ns=(1, first["codex"] + 10 ** 9))
        self.assertNotEqual(ad.usage_stamps(folder)["codex"], first["codex"])
        self.assertEqual(ad.usage_stamps(self.home / "missing"), {})

    def test_usage_record_name_checked(self):
        self.assertEqual(ad.usage_record("../etc/passwd", self.home), {})

    def test_last_used(self):
        f = self.write(".pi/agent/sessions/a/b.jsonl", "x")
        os.utime(f, (1_800_000_000, 1_800_000_000))
        self.assertEqual(ad.newest_mtime(["~/.pi/agent/sessions"]).timestamp(), 1_800_000_000)
        self.assertIsNone(ad.newest_mtime(["~/missing"]))

    def test_last_used_date_tree(self):
        """Codex: sessions/YYYY/MM/DD/rollout-….jsonl, four levels down: today's file is seen."""
        old = self.write(".codex/sessions/2026/09/30/rollout-a.jsonl", "x")
        new = self.write(".codex/sessions/2026/10/23/rollout-b.jsonl", "x")
        os.utime(old, (1_700_000_000, 1_700_000_000))
        os.utime(new, (1_800_000_000, 1_800_000_000))
        for folder in (".codex/sessions/2026/10/23", ".codex/sessions/2026/10", ".codex/sessions/2026/09/30",
                       ".codex/sessions/2026/09", ".codex/sessions/2026", ".codex/sessions"):
            os.utime(self.home / folder, (1_600_000_000, 1_600_000_000))
        self.assertEqual(ad.newest_mtime(["~/.codex/sessions"]).timestamp(), 1_800_000_000)

    def test_codex_model_from_its_last_session(self):
        """No model in config.toml: the one its newest session ran, Codex's own default."""
        import agent_readers
        self.write(".codex/config.toml", "[tui]\n")
        old = self.write(".codex/sessions/2026/10/22/rollout-a.jsonl",
                         '{"type":"turn_context","payload":{"cwd":"/x","model":"gpt-5"}}\n')
        os.utime(old, (1_700_000_000, 1_700_000_000))
        self.write(".codex/sessions/2026/10/23/rollout-b.jsonl",
                   '{"type":"session_meta","payload":{"model":"no"}}\n'
                   '{"type":"turn_context","payload":{"cwd":"/x","model":"gpt-6"}}\n'
                   '{"type":"turn_context","payload":{"cwd":"/x","model":"gpt-6.1-sol"}}\n')
        self.assertEqual(agent_readers.codex_session_model(), "gpt-6.1-sol")
        self.assertEqual(ad.details_for("codex", usage_dir=self.home).model, "gpt-6.1-sol")
        self.write(".codex/config.toml", 'model = "o9"\n')
        self.assertEqual(ad.details_for("codex", usage_dir=self.home).model, "o9")      # set: as it is

    def test_claude_model_from_its_last_session(self):
        """No model in settings.json: the one its newest session's last reply ran, as the map's alias."""
        import agent_readers
        self.write(".claude/settings.json", '{"theme": "dark"}')
        self.assertEqual(agent_readers.claude_session_model(), "")         # no sessions yet
        old = self.write(".claude/projects/-a/old.jsonl", '{"message":{"model":"claude-sonnet-5-5"}}\n')
        os.utime(old, (1_700_000_000, 1_700_000_000))
        self.write(".claude/projects/-b/new.jsonl",
                   '{"type":"assistant","message":{"model":"claude-sonnet-5-5"}}\n'
                   '{"type":"assistant","message":{"model":"<synthetic>"}}\n'
                   '{"type":"assistant","message":{"model":"claude-opus-5-5"}}\n')
        self.assertEqual(agent_readers.claude_session_model(), "opus")
        self.assertEqual(ad.details_for("claude", usage_dir=self.home).model, "opus")
        self.write(".claude/projects/-b/new.jsonl", '{"message":{"model":"claude-haiku-5-5"}}\n')
        self.assertEqual(agent_readers.claude_session_model(), "claude-haiku-5-5")   # no alias: its own name
        self.write(".claude/settings.json", '{"model": "sonnet"}')
        self.assertEqual(ad.details_for("claude", usage_dir=self.home).model, "sonnet")   # set: as it is

    def test_claude_session_model_by_file_time_and_cached(self):
        """A resumed old project counts by its file's time; within CLAUDE_RESCAN the folders are not
        looked through again, and an unchanged file is not read again."""
        import agent_readers
        self.write(".claude/settings.json", "{}")
        old = self.write(".claude/projects/-old/s.jsonl", '{"message":{"model":"claude-sonnet-5-5"}}\n')
        for n in range(20):                                   # more projects than any shortlist
            self.write(f".claude/projects/-p{n}/s.jsonl", '{"message":{"model":"claude-haiku-5-5"}}\n')
            os.utime(self.home / f".claude/projects/-p{n}/s.jsonl", (1_700_000_000 + n,) * 2)
        os.utime(self.home / ".claude/projects/-old", (1_600_000_000,) * 2)   # its folder looks oldest
        os.utime(old, (1_800_000_000,) * 2)                    # but its session was written last
        agent_readers._claude_seen["folder"] = None
        self.assertEqual(agent_readers.claude_session_model(), "sonnet")
        with mock.patch.object(agent_readers.os, "scandir", side_effect=AssertionError("rescanned")):
            self.assertEqual(agent_readers.claude_session_model(), "sonnet")
        (self.home / ".claude/projects/-p3").chmod(0)          # unreadable project: skipped, not fatal
        self.addCleanup((self.home / ".claude/projects/-p3").chmod, 0o755)
        agent_readers._claude_seen["folder"] = None
        self.assertEqual(agent_readers.claude_session_model(), "sonnet")

    def test_oversized_file_ignored(self):
        self.write(".copilot/config.json", '{"model": "x"}' + " " * (ad.MAX_FILE_BYTES + 1))
        self.assertEqual(ad.details_for("copilot", usage_dir=self.home).model, "")


if __name__ == "__main__":
    unittest.main()
