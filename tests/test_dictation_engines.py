"""Dictation in Ask AI (dictation.py): Omarchy's dictation types itself, Superwhisper's text comes from its
history, only a dictation made since the start counts."""

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import dictation  # noqa: E402


class EnginesTest(unittest.TestCase):
    def test_the_engine_set_or_the_first_one_here(self):
        with mock.patch.object(dictation, "available", return_value=["omarchy", "superwhisper"]):
            self.assertEqual(dictation.chosen("superwhisper"), "superwhisper")
            self.assertEqual(dictation.chosen("auto"), "omarchy")
        with mock.patch.object(dictation, "available", return_value=[]):
            self.assertIsNone(dictation.chosen("auto"))

    def test_superwhisper_text_is_the_newest_since_the_start(self):
        calls = []
        history = {"items": [{"timestamp": "2000", "metadata": {"result": "  hello there "}},
                             {"timestamp": "900", "metadata": {"result": "old"}}]}

        def run(argv):
            calls.append(argv[1])
            if argv[1] == "status":
                return True, json.dumps({"phase": "idle"})
            if argv[1] == "history":
                return True, json.dumps(history)
            return True, "{}"
        with mock.patch.object(dictation, "_run", run):
            d = dictation.Dictation("superwhisper")
            d.start()
            d.started = 1999
            self.assertEqual(d.stop(sleep=lambda _s: None), "hello there")
            d.started = 5000                          # nothing made since: no old text
            self.assertEqual(d.stop(sleep=lambda _s: None), "")
        self.assertEqual(calls[:3], ["start", "stop", "status"])

    def test_endless_engine_output_is_cut(self):
        ok, out = dictation._run(["sh", "-c", "yes x"])
        self.assertFalse(ok)
        self.assertLessEqual(len(out), dictation.MAX_OUTPUT)

    def test_omarchy_types_it_itself(self):
        calls = []
        with mock.patch.object(dictation, "_run", lambda argv: calls.append(argv) or (True, "")):
            d = dictation.Dictation("omarchy")
            self.assertIsNone(d.start())
            self.assertEqual(d.stop(), "")
        self.assertEqual(calls, [["omarchy-dictation", "start"], ["omarchy-dictation", "stop"]])


if __name__ == "__main__":
    unittest.main()
