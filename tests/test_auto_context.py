import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import connect  # noqa: E402
import context_ctl  # noqa: E402
import inventory  # noqa: E402
import llm_engine  # noqa: E402
import provider_llamacpp  # noqa: E402
import provider_ollama  # noqa: E402

GiB = 2 ** 30


class AutoContextTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "models").mkdir()
        (self.root / "models" / "Qwen.gguf").write_bytes(b"GGUF")
        self.ini = self.root / "models.ini"
        for patcher in (mock.patch.object(context_ctl, "MODELS_INI", self.ini),
                        mock.patch.object(context_ctl, "fit", return_value=context_ctl.Fit(131072, 65536, 1, 1))):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.llama = inventory.Runtime("llamacpp", "llama.cpp", True, False, address="127.0.0.1:8080",
                                       models_dir=str(self.root / "models"), models=["Qwen"])

    def test_llama_model_without_context_gets_the_best(self):
        self.ini.write_text("version = 1\n\n[Qwen]\ncache-type-k = q4_0\n")
        engine = provider_llamacpp.PROVIDER.engine
        ctx, changes = engine.auto_context(self.llama, "Qwen", vram=24 * GiB)
        self.assertEqual(ctx, 65536)
        self.assertEqual(len(changes), 1)
        self.assertIn("ctx-size = 65536", changes[0].new)
        self.assertIn("cache-type-k = q4_0", changes[0].new)        # the rest stays
        # Connecting an agent: the change rides along in the same preview and Undo.
        context, extra, api = engine.connect_extra(self.llama, "Qwen")
        self.assertEqual((context, len(extra), api), ({"Qwen": 65536}, 1, []))

    def test_a_context_already_set_is_kept(self):
        self.ini.write_text("[Qwen]\nctx-size = 32768\n")
        engine = provider_llamacpp.PROVIDER.engine
        self.assertEqual(engine.auto_context(self.llama, "Qwen", vram=24 * GiB), (32768, []))

    def test_nothing_when_it_cannot_be_worked_out(self):
        self.ini.write_text("")
        engine = provider_llamacpp.PROVIDER.engine
        self.assertEqual(engine.auto_context(self.llama, "Missing", vram=24 * GiB), (0, []))   # no file
        self.assertEqual(engine.auto_context(self.llama, "Qwen", vram=0), (0, []))           # no card known
        no_folder = inventory.Runtime("llamacpp", "llama.cpp", True, False, models=["Qwen"])
        self.assertEqual(engine.auto_context(no_folder, "Qwen", vram=24 * GiB), (0, []))
        self.assertEqual(llm_engine.Engine().auto_context(self.llama, "Qwen", vram=24 * GiB), (0, []))

    def test_ollama_is_set_through_its_api_when_the_connection_is_written(self):
        engine = provider_ollama.PROVIDER.engine
        rt = inventory.Runtime("ollama", "Ollama", True, False, address="127.0.0.1:11434",
                               models_dir=str(self.root / "models"), models=["qwen3:8b"])
        with mock.patch.object(type(engine), "context_now", return_value=(0, "f16", self.root / "models/Qwen.gguf")), \
                mock.patch.object(type(engine), "set_context") as set_context, \
                mock.patch.object(connect, "HOME", self.root):
            plan = connect.plan("opencode", rt, "qwen3:8b", use_now=True)
            self.assertEqual(plan.contexts, [(rt, "qwen3:8b", 65536)])
            self.assertTrue(any("65536" in note for note in plan.notes))
            set_context.assert_not_called()                          # not while only previewing
            connect.apply(plan, backup_root=self.root / "backups")
            set_context.assert_called_once_with(rt, "qwen3:8b", 65536)

    def test_new_models_after_a_download(self):
        self.ini.write_text("[Qwen]\n")
        engine = provider_llamacpp.PROVIDER.engine
        with mock.patch.object(connect, "apply") as apply, \
                mock.patch("gpu_usage.gpu_totals", return_value=(0, 24 * GiB, None)):
            self.assertEqual(engine.set_new_contexts(self.llama, ["Qwen", "Missing"]), [("Qwen", 65536)])
        written = apply.call_args[0][0]
        self.assertIn("ctx-size = 65536", written.changes[0].new)


if __name__ == "__main__":
    unittest.main()
