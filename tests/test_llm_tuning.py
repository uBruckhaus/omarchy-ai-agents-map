import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import context_ctl  # noqa: E402
import llm_engine  # noqa: E402
import llm_tuning  # noqa: E402


def facts():
    engine = llm_engine.of("llamacpp")
    return llm_tuning.Facts("llama.cpp", "llamacpp", vram=24 * 1024 ** 3, build="rocm",
                            builds=["rocm", "vulkan", "cuda", "cpu"], tuning=dict(engine.tuning),
                            settings={"*": {"flash-attn": "on", "parallel": "1"}},
                            models=[llm_tuning.ModelFacts("qwen", 16 * 1024 ** 3, 98304, 94208, 262144, 75000)])


class TuningTest(unittest.TestCase):
    def test_only_what_the_app_can_stand_behind(self):
        answer = {"summary": "ok", "suggestions": [
            {"kind": "setting", "title": "Quantize the K cache", "model": "qwen", "key": "cache-type-k", "value": "q8_0"},
            {"kind": "setting", "title": "already set", "model": "*", "key": "flash-attn", "value": "on"},
            {"kind": "setting", "title": "not a key", "model": "*", "key": "rope-scale", "value": "2"},
            {"kind": "setting", "title": "shell", "model": "*", "key": "threads", "value": "8; rm -rf ~"},
            {"kind": "context", "title": "fits", "model": "qwen", "value": "90112"},
            {"kind": "context", "title": "too big", "model": "qwen", "value": "131072"},
            {"kind": "context", "title": "unknown model", "model": "other", "value": "4096"},
            {"kind": "build", "title": "Vulkan", "value": "vulkan"},
            {"kind": "build", "title": "same", "value": "rocm"},
            {"kind": "note", "title": "A note", "key": "x", "value": "y"},
            {"kind": "run", "title": "run something"}]}
        summary, found = llm_tuning.validate(answer, facts())
        self.assertEqual(summary, "ok")
        self.assertEqual([s.title for s in found], ["Quantize the K cache", "fits", "Vulkan", "A note"])
        self.assertEqual((found[1].key, found[3].key, found[3].value), ("ctx-size", "", ""))

    def test_the_prompt_holds_the_facts_and_the_rules(self):
        text = llm_tuning.prompt(facts())
        self.assertIn("qwen: 16.0 GB; now 98304; fits 94208", text)
        self.assertIn("cache-type-k: f16|q8_0|q4_0", text)

    def test_ini_setting_keeps_the_rest(self):
        old = "version = 1\n\n[*]\nflash-attn = on\n\n[qwen]\nctx-size = 8192\n"
        new = context_ctl.ini_text(old, "qwen", "cache-type-k", "q8_0")
        self.assertIn("[qwen]\ncache-type-k = q8_0\nctx-size = 8192\n", new)
        self.assertIn("[*]\nflash-attn = on\n", new)
        self.assertIn("flash-attn = off", context_ctl.ini_text(old, "*", "flash-attn", "off"))
        for bad in (("qwen", "key with space", "1"), ("qwen", "threads", "8\n[x]"), ("a]b", "threads", "8")):
            with self.assertRaises(ValueError):
                context_ctl.ini_text(old, *bad)

    def test_the_engine_refuses_other_values(self):
        engine = llm_engine.of("llamacpp")
        with self.assertRaises(ValueError):
            engine.tuning_changes(None, "*", "gpu-layers", "lots")

    def test_ollama_settings_are_for_every_model_only(self):
        from unittest import mock
        import install_ctl
        engine = llm_engine.of("ollama")
        f = llm_tuning.Facts("Ollama", "ollama", tuning=dict(engine.tuning), per_model=False,
                             models=[llm_tuning.ModelFacts("qwen")])
        _s, found = llm_tuning.validate({"suggestions": [
            {"kind": "setting", "title": "one model", "model": "qwen", "key": "OLLAMA_FLASH_ATTENTION", "value": "1"},
            {"kind": "setting", "title": "all", "model": "*", "key": "OLLAMA_FLASH_ATTENTION", "value": "1"}]}, f)
        self.assertEqual([s.title for s in found], ["all"])
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / "server.env"
            env.write_text("OLLAMA_HOST=127.0.0.1:11434\n")
            with mock.patch.object(install_ctl.Ollama, "env_file", env):
                [change] = engine.tuning_changes(None, "*", "OLLAMA_KV_CACHE_TYPE", "q8_0")
                self.assertIn("OLLAMA_KV_CACHE_TYPE=q8_0", change.new)
                with self.assertRaises(ValueError):
                    engine.tuning_changes(None, "qwen", "OLLAMA_KV_CACHE_TYPE", "q8_0")

    def test_an_older_addon_engine_gets_the_tuning_of_the_apps_own(self):
        from unittest import mock
        import providers

        class Old(llm_engine.LlamaRouter):      # a provider add-on signed before tuning existed
            pass
        old = Old()
        fake = mock.Mock(engine=old)
        with mock.patch.object(providers, "get", return_value=fake):
            engine = llm_engine.of("llamacpp")
        self.assertIs(engine, old)
        self.assertIn("flash-attn", engine.tuning)
        with self.assertRaises(ValueError):
            engine.tuning_changes(None, "*", "flash-attn", "maybe")


if __name__ == "__main__":
    unittest.main()
