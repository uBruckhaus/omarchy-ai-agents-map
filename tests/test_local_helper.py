import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import local_helper  # noqa: E402

GB = 1024 ** 3


def runtime(models):
    return types.SimpleNamespace(key="llamacpp", name="llama.cpp", installed=True, models=models)


class LocalHelperTest(unittest.TestCase):
    def found(self, sizes):
        details = types.SimpleNamespace(providers=[types.SimpleNamespace(name="llama-local", local=True,
                                                                         runtime="llama.cpp")])
        with mock.patch.object(local_helper, "advisers", return_value=["pi"]):
            return local_helper.candidates({"pi"}, [runtime(list(sizes))], details_for=lambda _a: details,
                                           size_of=lambda _rt, name: sizes[name])

    def test_speech_and_embedding_models_are_left_out(self):
        found = self.found({"qwen3-tts-0.6b": 1 * GB, "nomic-embed-text": 1 * GB, "gemma-4-12B": 7 * GB})
        self.assertEqual([c.model for c in found], ["gemma-4-12B"])

    def test_the_largest_model_that_fits_is_suggested(self):
        found = self.found({"small": 3 * GB, "big": 17 * GB, "huge": 40 * GB, "tiny": 1 * GB})
        self.assertEqual(local_helper.suggest(found, 24 * GB).model, "big")
        self.assertIsNone(local_helper.suggest(self.found({"huge": 40 * GB}), 24 * GB))

    def test_only_a_confirmed_helper_is_used(self):
        found = self.found({"big": 17 * GB})
        self.assertIsNone(local_helper.chosen(found, {}))
        self.assertEqual(local_helper.chosen(found, {"agent": "pi", "provider": "llama-local", "model": "big"}),
                         found[0])
        self.assertIsNone(local_helper.chosen(found, {"agent": "pi", "provider": "llama-local", "model": "gone"}))

    def test_pi_answers_offline_with_the_model_given(self):
        candidate = local_helper.Candidate("pi", "llama-local", "llamacpp", "gemma-4-26B")
        argv, output = local_helper.advise_argv(candidate)
        self.assertEqual(argv[-4:], ["--provider", "llama-local", "--model", "gemma-4-26B"])
        for flag in ("--no-tools", "--no-mcp", "--no-extensions", "--no-context-files", "--offline", "-p"):
            self.assertIn(flag, argv)
        self.assertEqual(output, "text")


if __name__ == "__main__":
    unittest.main()
