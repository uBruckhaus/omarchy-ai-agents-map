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

    def test_usage_record_name_checked(self):
        self.assertEqual(ad.usage_record("../etc/passwd", self.home), {})

    def test_last_used(self):
        f = self.write(".pi/agent/sessions/a/b.jsonl", "x")
        os.utime(f, (1_800_000_000, 1_800_000_000))
        self.assertEqual(ad.newest_mtime(["~/.pi/agent/sessions"]).timestamp(), 1_800_000_000)
        self.assertIsNone(ad.newest_mtime(["~/missing"]))

    def test_oversized_file_ignored(self):
        self.write(".copilot/config.json", '{"model": "x"}' + " " * (ad.MAX_FILE_BYTES + 1))
        self.assertEqual(ad.details_for("copilot", usage_dir=self.home).model, "")


if __name__ == "__main__":
    unittest.main()
