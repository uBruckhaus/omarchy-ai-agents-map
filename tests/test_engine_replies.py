"""Review 064 #4: a local LLM's JSON reply of an unexpected shape (a list, "data" an object or a string,
rows that are not objects, a status that is text) never breaks loaded_models or unload_all while the map
is built: no models, nothing unloaded, no exception. Every built-in engine, Strata's (the base) too."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import llm_engine  # noqa: E402
import providers  # noqa: E402

# Shapes that hold no model at all.
ODD = [[], [1, 2], "text", 5, {"data": {}}, {"data": "x"}, {"data": 5}, {"models": {}}, {"models": "x"},
       {"data": [1, "x", None, []]}, {"models": [1, "x", None]}]
# Rows of mixed shapes, a status that is not an object: no exception (what counts is the engine's rule).
MIXED = [{"data": [1, "x", None, [], {"status": "loaded", "id": "a"}]}, {"data": [{"status": ["loaded"], "id": "b"}]},
         {"models": [{"name": 5}, "y", {"name": "c"}]}]


class OddRepliesTest(unittest.TestCase):
    def test_every_engine_survives_odd_replies(self):
        rt = NS(key="", name="", address="127.0.0.1:8080", installed=True, service="", running=True)
        for provider in providers.built_in_local():
            engine = llm_engine.of(provider.key)
            for reply in ODD:
                with self.subTest(provider=provider.key, reply=reply), \
                        mock.patch.object(llm_engine, "_http", return_value=reply), \
                        mock.patch("gpu_usage._own_port", return_value=True), \
                        mock.patch.object(engine, "stop", return_value=(True, "")), \
                        mock.patch.object(llm_engine, "_run", return_value=(True, "")):
                    rt.key = provider.key
                    self.assertEqual(engine.loaded_models(rt), {})
                    engine.unload_all(rt)
            for reply in MIXED:
                with self.subTest(provider=provider.key, reply=reply), \
                        mock.patch.object(llm_engine, "_http", return_value=reply), \
                        mock.patch("gpu_usage._own_port", return_value=True), \
                        mock.patch.object(engine, "stop", return_value=(True, "")), \
                        mock.patch.object(llm_engine, "_run", return_value=(True, "")):
                    rt.key = provider.key
                    self.assertIsInstance(engine.loaded_models(rt), dict)
                    engine.unload_all(rt)

    def test_rows(self):
        self.assertEqual(llm_engine.rows({"data": [1, 2, 3]}, "data", 2), [1, 2])
        for reply in ([], "x", 5, None, {"data": {}}, {"data": "x"}, {"other": []}):
            self.assertEqual(llm_engine.rows(reply, "data", 10), [], reply)

if __name__ == "__main__":
    unittest.main()
