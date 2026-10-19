import json
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import graph  # noqa: E402
import gpu_usage  # noqa: E402
import runtime_ctl  # noqa: E402


def runtime(**kw):
    base = dict(key="lmstudio", name="LM Studio", installed=True, running=False, server_off=False,
                address="127.0.0.1:1234")
    return types.SimpleNamespace(**{**base, **kw})


class ServerOff(unittest.TestCase):
    # LM Studio open without `lms server start`: agents cannot reach it, so the card says so.
    def test_status(self):
        self.assertEqual(graph.runtime_status(runtime(running=True)), "running")
        self.assertEqual(graph.runtime_status(runtime(running=True, server_off=True)), "noserver")
        self.assertEqual(graph.runtime_status(runtime()), "stopped")
        self.assertEqual(graph.runtime_status(runtime(installed=False)), "missing")

    def test_card_detail(self):
        labels = {"server_off": "server off"}
        self.assertEqual(graph.runtime_detail(runtime(running=True, server_off=True), labels),
                         "127.0.0.1:1234 · server off")
        self.assertEqual(graph.runtime_detail(runtime(running=True), labels), "127.0.0.1:1234")


class LoadedWithoutServer(unittest.TestCase):
    def setUp(self):
        self.saved = runtime_ctl.lms_binary
        runtime_ctl.lms_binary = lambda: "/usr/bin/lms"

    def tearDown(self):
        runtime_ctl.lms_binary = self.saved

    def test_lms_ps(self):
        rows = [{"type": "llm", "identifier": "Qwen3.8-27b"}, {"type": "embedding", "identifier": "nomic"}]
        found = gpu_usage.lms_loaded(lambda argv, **kw: (json.dumps(rows).encode(), True))
        self.assertEqual(found, {"qwen3.8-27b": "loaded"})

    def test_bad_output(self):
        for out in (b"", b"not json", b'{"a": 1}', b"[1, 2]"):
            self.assertEqual(gpu_usage.lms_loaded(lambda argv, **kw: (out, True)), {}, out)
        self.assertEqual(gpu_usage.lms_loaded(lambda argv, **kw: (b"[]", False)), {})


if __name__ == "__main__":
    unittest.main()
