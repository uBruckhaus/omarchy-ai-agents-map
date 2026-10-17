import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import model_names  # noqa: E402
import models_ctl  # noqa: E402


class NamesTest(unittest.TestCase):
    def names(self, text):
        return [part for part, _t, _v in model_names.parts(text)]

    def test_parts_of_typical_names(self):
        self.assertEqual(self.names("unsloth/Qwen3.6-35B-A3B-MTP-GGUF"), ["35B-A3B", "MTP", "GGUF"])
        self.assertEqual(self.names("Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf"), ["35B-A3B", "UD", "Q4_K_XL"])
        self.assertEqual(self.names("gemma-4-12b-it-qat-Q4_0.gguf"), ["12b", "it", "qat", "Q4_0"])
        self.assertEqual(self.names("gemma-4-E2B-it-Q4_K_M"), ["E2B", "it", "Q4_K_M"])
        self.assertEqual(self.names("Qwen2.5-Coder-7B-Instruct-128K-Q8_0"), ["Coder", "7B", "Instruct", "128K", "Q8_0"])
        self.assertEqual(self.names("mmproj-BF16.gguf"), ["mmproj", "BF16"])
        moe = model_names.parts("Qwen3.6-35B-A3B")[0]
        self.assertEqual(moe[2], ("35", "3"))
        self.assertIn("{2}", moe[1])

    def test_every_kind_once_and_nothing_made_up(self):
        self.assertEqual(self.names("Model-Q4_K_M-Q4_K_M-7B-8B"), ["Q4_K_M", "7B"])
        self.assertEqual(self.names("just-a-name"), [])


class SearchTest(unittest.TestCase):
    ITEM = {"id": "unsloth/Qwen3.6-35B-A3B-MTP-GGUF", "downloads": 10, "likes": 2,
            "lastModified": "2026-10-01T00:00:00.000Z", "pipeline_tag": "image-text-to-text",
            "tags": ["gguf"], "gguf": {"total": 35_000_000_000, "chat_template": "{% if tools %}<think>"}}

    def test_rows_carry_capabilities_and_only_chat_models(self):
        image = {**self.ITEM, "id": "x/Image-Gen-GGUF", "pipeline_tag": "text-to-image"}
        bad = {**self.ITEM, "id": "../etc"}
        with mock.patch.object(models_ctl, "_json_list", return_value=[self.ITEM, image, bad, "junk"]) as fetch:
            found = models_ctl.search("")
        self.assertIn("sort=trendingScore", fetch.call_args.args[0])
        self.assertEqual([r.id for r in found], ["unsloth/Qwen3.6-35B-A3B-MTP-GGUF"])
        r = found[0]
        self.assertEqual((r.vision, r.tools, r.reasoning, r.mtp, r.params), (True, True, True, True, 35_000_000_000))
        self.assertEqual((r.author, r.name), ("unsloth", "Qwen3.6-35B-A3B-MTP-GGUF"))

    def test_details_from_card_and_gguf(self):
        data = {**self.ITEM, "cardData": {"license": "apache-2.0\x07", "base_model": ["Qwen/Qwen3.6-35B-A3B"]},
                "gguf": {**self.ITEM["gguf"], "architecture": "qwen35moe", "context_length": 262144},
                "siblings": [{"rfilename": "mmproj-F16.gguf"}], "gated": False}
        with mock.patch("netfetch.fetch_json", return_value=data):
            info = models_ctl.details("unsloth/Qwen3.6-35B-A3B-MTP-GGUF")
        self.assertEqual((info.license, info.base_model, info.architecture, info.context, info.mmproj),
                         ("apache-2.0", "Qwen/Qwen3.6-35B-A3B", "qwen35moe", 262144, True))
        with self.assertRaises(ValueError):
            models_ctl.details("not a repo/../x")

    def test_readme_is_plain_text(self):
        text = models_ctl.plain_readme("---\nlicense: x\n---\n# Title\n<img src=x onerror=alert(1)>"
                                       "![pic](http://x/y.png)\n[a link](http://evil)\n\n\n\n<b>bold</b>\x1b[31m")
        self.assertEqual(text, "# Title\n\na link\n\nbold[31m")
        self.assertNotIn("<", text)

    def test_fit(self):
        gb = 1024 ** 3
        self.assertEqual(models_ctl.fit(10 * gb, 24 * gb, 64 * gb), "fits")
        self.assertEqual(models_ctl.fit(30 * gb, 24 * gb, 64 * gb), "partly")
        self.assertEqual(models_ctl.fit(80 * gb, 24 * gb, 64 * gb), "too_large")
        self.assertEqual(models_ctl.fit(5 * gb, 0, 32 * gb), "partly")      # no graphics card: in memory


class RemovalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.models = self.root / "models"
        self.models.mkdir()

    def test_llama_file_split_parts_and_links(self):
        (self.models / "a.gguf").write_bytes(b"x" * 10)
        for i in (1, 2):
            (self.models / f"big-0000{i}-of-00002.gguf").write_bytes(b"y" * 5)
        elsewhere = self.root / "store/c.gguf"
        elsewhere.parent.mkdir()
        elsewhere.write_bytes(b"z" * 7)
        (self.models / "c.gguf").symlink_to(elsewhere)
        one = models_ctl.removal("llamacpp", self.models, "a")
        self.assertEqual((one.paths, one.size, one.links_only), ([self.models / "a.gguf"], 10, False))
        split = models_ctl.removal("llamacpp", self.models, "big")
        self.assertEqual((len(split.paths), split.size), (2, 10))
        link = models_ctl.removal("llamacpp", self.models, "c")
        self.assertEqual((link.paths, link.size, link.links_only), ([self.models / "c.gguf"], 0, True))
        with self.assertRaises(ValueError):
            models_ctl.removal("llamacpp", self.models, "missing")
        with self.assertRaises(ValueError):
            models_ctl.removal("ollama", self.models, "qwen3:8b")

    def test_shared_with_another_llm_is_named(self):
        (self.models / "a.gguf").write_bytes(b"x")
        other = self.root / "other"
        other.mkdir()
        (other / "a.gguf").symlink_to(self.models / "a.gguf")
        found = models_ctl.removal("llamacpp", self.models, "a", {"KoboldCpp": ("koboldcpp", str(other))})
        self.assertEqual(found.shared, ["KoboldCpp"])
        hard = self.root / "ollama"
        hard.mkdir()
        os.link(self.models / "a.gguf", hard / "a.gguf")          # a hard link keeps the data
        found = models_ctl.removal("llamacpp", self.models, "a", {"Ollama": ("ollama", str(hard))})
        self.assertEqual(found.shared, [])
        (self.models / "b.gguf").symlink_to(other / "a.gguf")      # removing only a link loses nobody anything
        found = models_ctl.removal("llamacpp", self.models, "b", {"KoboldCpp": ("koboldcpp", str(other))})
        self.assertEqual((found.links_only, found.shared), (True, []))

    def test_trash_goes_through_gio(self):
        (self.models / "a.gguf").write_bytes(b"x")
        calls = []
        fake = mock.Mock()
        fake.new_for_path.side_effect = lambda p: mock.Mock(trash=lambda _c: calls.append(p))
        with mock.patch("gi.repository.Gio.File", fake):
            models_ctl.trash([self.models / "a.gguf"])
        self.assertEqual(calls, [str(self.models / "a.gguf")])
        self.assertTrue((self.models / "a.gguf").exists())            # the fake did not move it

    def test_ollama_delete_checks_name_and_listener(self):
        with mock.patch.object(models_ctl, "require_own_listener", side_effect=RuntimeError("not yours")), \
                mock.patch("netfetch.fetch") as fetch, self.assertRaises(RuntimeError):
            models_ctl.ollama_delete("qwen3:8b", "127.0.0.1:11434")
        fetch.assert_not_called()
        with self.assertRaises(ValueError):
            models_ctl.ollama_delete("../../x", "127.0.0.1:11434")
        with mock.patch.object(models_ctl, "require_own_listener"), mock.patch("netfetch.fetch") as fetch:
            models_ctl.ollama_delete("qwen3:8b", "127.0.0.1:11434")
        self.assertEqual(json.loads(fetch.call_args.kwargs["data"]), {"model": "qwen3:8b"})
        self.assertEqual(fetch.call_args.kwargs["method"], "DELETE")


if __name__ == "__main__":
    unittest.main()
