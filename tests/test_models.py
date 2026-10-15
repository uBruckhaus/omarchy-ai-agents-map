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


if __name__ == "__main__":
    unittest.main()
