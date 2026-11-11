"""Review 063 round 5: a Hermes or Oh My Pi provider whose YAML key is quoted is the same entry when it is
rewritten (a Repair of a moved local LLM), so the file never gets the key twice."""
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import adapter_data  # noqa: E402

hermes = adapter_data.hooks_module("hermes")


class QuotedProviderKeyTest(unittest.TestCase):
    def test_a_quoted_hermes_provider_is_rewritten_not_doubled(self):
        for key in ("workstation", '"workstation"', "'workstation'", "workstation "):
            with self.subTest(key=key):
                before = ("model:\n  default: qwen\n  provider: openai\nproviders:\n"
                          f"  {key}:\n    name: workstation\n    base_url: http://127.0.0.1:8080/v1\n")
                after = hermes.llm_text(before, "workstation", "workstation", "http://127.0.0.1:9090/v1",
                                        ["qwen"], {}, "qwen", False)
                providers = yaml.safe_load(after)["providers"]
                self.assertEqual(list(providers), ["workstation"])
                self.assertNotIn("127.0.0.1:8080", after)


def _with_key(key):
    return ("providers:\n"
            f"  {key}:\n    default_model: qwen\n    models:\n      qwen: {{}}\n"
            "  workstation2:\n    models: []\n")


class ProviderKeyReaderTest(unittest.TestCase):
    """Review 063 round 6: one reader for every providers key; what it cannot read safely stops the edit."""

    def test_keys_are_read_decoded(self):
        import connect
        omp = adapter_data.hooks_module("omp")
        for key in ("workstation", '"workstation"', "'workstation'", "workstation "):
            with self.subTest(key=key):
                text = _with_key(key)
                self.assertEqual(connect.provider_keys(text), ["workstation", "workstation2"])
                self.assertTrue(hermes.local_llm_connected([text], "workstation"))
                self.assertTrue(omp.local_llm_connected([text], "workstation"))
                with tempfile.TemporaryDirectory() as home:
                    (Path(home) / ".hermes").mkdir()
                    (Path(home) / ".hermes/config.yaml").write_text(text)
                    self.assertEqual(hermes.models_choices(home=Path(home)), ["workstation/qwen"])
        self.assertEqual(connect.provider_keys(_with_key("'it''s'")), ["it's", "workstation2"])

    def test_unreadable_keys_stop_the_rewrite(self):
        import connect
        for key in ('"\\u0077orkstation"', "? workstation", "{a: b}", " workstation", "&a workstation"):
            with self.subTest(key=key):
                text = _with_key(key)
                with self.assertRaises(ValueError):
                    hermes.llm_text(text, "workstation", "workstation", "http://127.0.0.1:9090/v1",
                                    ["qwen"], {}, "qwen", False)
                self.assertEqual(connect.provider_keys(text), [])

    def test_a_name_that_needs_quoting_is_not_written_bare(self):
        with self.assertRaises(ValueError):
            hermes.llm_text("", "a: b", "x", "http://h/v1", [], {}, "", False)


    def test_round7_inline_maps_and_non_string_names_are_refused(self):
        import connect
        args = ("http://127.0.0.1:9090/v1", ["qwen"], {}, "qwen", False)
        with self.assertRaises(ValueError):
            hermes.llm_text("providers: {workstation: {api: x}}\n", "workstation", "workstation", *args)
        for name in ("true", "No", "null", "1.5", "0x1f", "2001-12-14"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                hermes.llm_text(f'providers:\n  "{name}":\n    api: x\n', name, name, *args)
        after = hermes.llm_text("providers: {}  # none yet\n", "llama-local", "llama.cpp", *args)
        self.assertEqual(list(yaml.safe_load(after)["providers"]), ["llama-local"])
        self.assertEqual(connect.provider_keys(after), ["llama-local"])

if __name__ == "__main__":
    unittest.main()
