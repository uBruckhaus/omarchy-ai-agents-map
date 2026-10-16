import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import models_ctl as mc  # noqa: E402

TREE = [
    {"type": "file", "path": "Model-Q4_K_M.gguf", "lfs": {"size": 5_000_000_000}},
    {"type": "file", "path": "Model-Q8_0.gguf", "lfs": {"size": 9_000_000_000}},
    {"type": "file", "path": "Model.mmproj-Q8_0.gguf", "lfs": {"size": 700_000_000}},
    {"type": "file", "path": "BF16/Model-BF16-00001-of-00002.gguf", "lfs": {"size": 9_000_000_000}},
    {"type": "file", "path": "BF16/Model-BF16-00002-of-00002.gguf", "lfs": {"size": 8_000_000_000}},
    {"type": "file", "path": "../evil.gguf", "size": 1},
    {"type": "file", "path": "README.md", "size": 10},
]


class ModelsTest(unittest.TestCase):
    def test_files_group_split_skip_mmproj_and_recommend(self):
        with mock.patch.object(mc, "_json_list", lambda url: TREE):
            entries = mc.files("someone/Model-GGUF", vram=12 * 2**30)
        names = [(e.name, e.quant, e.size) for e in entries]
        self.assertEqual(names, [("Model-Q4_K_M", "Q4_K_M", 5_000_000_000), ("Model-Q8_0", "Q8_0", 9_000_000_000),
                                 ("Model-BF16", "BF16", 17_000_000_000)])
        self.assertEqual([e.recommended for e in entries], [True, False, False])
        self.assertEqual(entries[2].paths, ["BF16/Model-BF16-00001-of-00002.gguf", "BF16/Model-BF16-00002-of-00002.gguf"])
        self.assertFalse(entries[2].fits)

    def test_bad_names_refused(self):
        with self.assertRaises(ValueError):
            mc.files("../etc", 0)
        with self.assertRaises(ValueError):
            mc.ollama_pull("bad name;rm", "127.0.0.1:11434", lambda *a: None, lambda: False)

    def test_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            originals, router = root / "Models", root / "Models/router"
            router.mkdir(parents=True)
            (originals / "a.gguf").write_text("x")
            (router / "a.gguf").symlink_to(originals / "a.gguf")
            self.assertEqual(mc.gguf_target("llamacpp", router, "u/r"), (originals, router))
            self.assertEqual(mc.gguf_target("lmstudio", originals, "u/r"), (originals / "u/r", None))
            self.assertEqual(mc.gguf_target("llamacpp", originals, "u/r"), (originals, None))

    def test_listener_owner_from_proc(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "tcp").write_text(
                "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
                "   0: 0100007F:2CAA 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 1\n")
            self.assertEqual(mc.loopback_listeners(11434, tmp), [1000])
            self.assertEqual(mc.loopback_listeners(8080, tmp), [])

    def test_ollama_name(self):
        self.assertEqual(mc.ollama_name_for("u/Model-GGUF", mc.GgufFile("Model-Q4_K_M", [], 1, "Q4_K_M")),
                         "hf.co/u/Model-GGUF:Q4_K_M")


class OllamaCreateTest(unittest.TestCase):
    def test_path_that_could_break_the_modelfile_is_refused_before_anything_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ('a"b.gguf', "a\\b.gguf", "a\nFROM x.gguf", "a\rb.gguf"):
                path = Path(tmp) / name
                path.write_bytes(b"GGUF")
                with mock.patch.object(mc, "require_own_listener") as listener, \
                        mock.patch("ai_core.read_bounded_output") as run:
                    with self.assertRaises(ValueError):
                        mc.ollama_create(path, "127.0.0.1:11434")
                listener.assert_not_called()
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class ContextTest(unittest.TestCase):
    def test_ini_text(self):
        import context_ctl as cc
        ini = "version = 1\n\n[*]\nctx-size = 8192\n\n; comment\n[A]\ncache-type-k = q4_0\nctx-size = 4096\n\n[B]\nx = 1\n"
        new = cc.llama_context_text(ini, "A", 98304)
        self.assertIn("[A]\ncache-type-k = q4_0\nctx-size = 98304\n", new)
        self.assertIn("[*]\nctx-size = 8192", new)
        self.assertIn("; comment", new)
        self.assertIn("[B]\nctx-size = 65536\nx = 1", cc.llama_context_text(ini, "B", 65536))
        self.assertTrue(cc.llama_context_text(ini, "C", 4096).endswith("[C]\nctx-size = 4096\n"))
        with self.assertRaises(ValueError):
            cc.llama_context_text(ini, "bad]name", 4096)

    def test_kv_bytes(self):
        import context_ctl as cc
        meta = {"general.architecture": "x", "x.block_count": 10, "x.attention.head_count": 8,
                "x.attention.head_count_kv": 2, "x.embedding_length": 1024}
        # 10 layers × 2 heads × (128 + 128) × 2 bytes (f16)
        self.assertEqual(cc.kv_bytes_per_token(meta, "f16"), 10 * 2 * 256 * 2)
        meta["x.attention.head_count_kv"] = [2, 0, 2, 0, 2, 0, 2, 0, 2, 0]   # hybrid: half the layers attend
        self.assertEqual(cc.kv_bytes_per_token(meta, "f16"), 5 * 2 * 256 * 2)


class SlidingWindowTest(unittest.TestCase):
    def test_gemma_style(self):
        import context_ctl as cc
        meta = {"general.architecture": "g", "g.block_count": 6, "g.attention.head_count": 16,
                "g.attention.head_count_kv": [8, 8, 8, 8, 8, 1], "g.attention.key_length": 512,
                "g.attention.value_length": 512, "g.attention.key_length_swa": 256, "g.attention.value_length_swa": 256,
                "g.attention.sliding_window": 1024, "g.attention.sliding_window_pattern": [True] * 5 + [False]}
        per_token, fixed = cc.kv_layout(meta, "f16")
        self.assertEqual(per_token, 1 * 1024 * 2)                      # only the global layer grows
        self.assertEqual(fixed, 5 * 8 * 512 * 2 * (1024 + cc.UBATCH))   # the window layers are fixed

    def test_unknown_model(self):
        import context_ctl as cc
        self.assertEqual(cc.kv_layout({"general.architecture": "tts"}), (0, 0))


class LmStudioContextTest(unittest.TestCase):
    def test_defaults_file_and_text(self):
        import json
        import context_ctl as c
        from pathlib import Path
        base = Path("/d")
        ok = {"indexedModelIdentifier": "pub/repo/m-Q4_0.gguf"}
        self.assertEqual(c.lmstudio_defaults_file(ok, base), Path("/d/pub/repo/m-Q4_0.gguf.json"))
        for bad in ("../x/m.gguf", "/etc/passwd", "pub/../../x", "a/b c", "a\nb", "", None, 5):
            self.assertIsNone(c.lmstudio_defaults_file({"indexedModelIdentifier": bad}, base), bad)
        text = c.lmstudio_context_text(json.dumps({"preset": "p", "load": {"fields": [
            {"key": "llm.load.contextLength", "value": 4096}, {"key": "llm.load.gpuOffload", "value": 1}]}}), 65536)
        data = json.loads(text)
        self.assertEqual(data["preset"], "p")
        self.assertEqual(data["load"]["fields"], [{"key": "llm.load.gpuOffload", "value": 1},
                                                  {"key": "llm.load.contextLength", "value": 65536}])
        with self.assertRaises(ValueError):
            c.lmstudio_context_text("{not json", 4096)

    def test_settings_and_model_file(self):
        import json
        import tempfile
        import context_ctl as c
        from pathlib import Path
        with tempfile.TemporaryDirectory() as root:
            entry = {"indexedModelIdentifier": "pub/repo/m.gguf",
                     "entryPoint": {"absPath": f"{root}/models/pub/repo/m.gguf", "rootAbsolutePath": f"{root}/models"}}
            defaults = Path(root, "defaults")
            self.assertEqual(c.lmstudio_settings(entry, defaults), (0, "f16"))
            file = defaults / "pub/repo/m.gguf.json"
            file.parent.mkdir(parents=True)
            file.write_text(c.lmstudio_context_text("", 32768))
            self.assertEqual(c.lmstudio_settings(entry, defaults), (32768, "f16"))
            self.assertIsNone(c.lmstudio_model_file(entry))                 # no such file
            Path(root, "models/pub/repo").mkdir(parents=True)
            Path(root, "models/pub/repo/m.gguf").write_bytes(b"GGUF")
            self.assertEqual(c.lmstudio_model_file(entry), Path(root, "models/pub/repo/m.gguf").resolve())
            outside = dict(entry, entryPoint={"absPath": "/etc/hostname.gguf", "rootAbsolutePath": f"{root}/models"})
            self.assertIsNone(c.lmstudio_model_file(outside))


class LmStudioAutoContextTest(unittest.TestCase):
    def setUp(self):
        import json
        import tempfile
        from pathlib import Path
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        (self.root / ".internal").mkdir()
        (self.root / "models/pub/repo").mkdir(parents=True)
        self.model = self.root / "models/pub/repo/m.gguf"
        self.model.write_bytes(b"GGUF")
        (self.root / ".internal/model-index-cache.json").write_text(json.dumps({"models": [{
            "defaultIdentifier": "m", "domain": "llm", "indexedModelIdentifier": "pub/repo/m.gguf",
            "entryPoint": {"absPath": str(self.model), "rootAbsolutePath": str(self.root / "models")}}]}))
        self.defaults = self.root / "defaults"

    def tearDown(self):
        self.dir.cleanup()

    def test_sets_best_when_nothing_set(self):
        import context_ctl as c
        old_fit = c.fit
        c.fit = lambda *_a, **_k: c.Fit(native=131072, best=65536, per_token=1, model_size=1)
        try:
            ctx, path, old, new = c.lmstudio_auto_context("m", 24 * 2**30, self.root, self.defaults)
        finally:
            c.fit = old_fit
        self.assertEqual(ctx, 65536)
        self.assertEqual(path, self.defaults / "pub/repo/m.gguf.json")
        self.assertEqual(old, "")
        self.assertIn('"value": 65536', new)

    def test_keeps_users_context(self):
        import context_ctl as c
        path = self.defaults / "pub/repo/m.gguf.json"
        path.parent.mkdir(parents=True)
        path.write_text(c.lmstudio_context_text("", 16384))
        ctx, got, old, new = c.lmstudio_auto_context("m", 24 * 2**30, self.root, self.defaults)
        self.assertEqual((ctx, got), (16384, path))
        self.assertEqual(old, new)                       # nothing to change

    def test_unknown_model_or_no_gpu(self):
        import context_ctl as c
        self.assertEqual(c.lmstudio_auto_context("other", 24 * 2**30, self.root, self.defaults), (0, None, "", ""))
        self.assertEqual(c.lmstudio_auto_context("m", 0, self.root, self.defaults), (0, None, "", ""))
