"""Measure and tune (llama_tune, Maprisma task 029): the fastest batch wins, the context is what the card
leaves, models.ini gets ctx-size and the batch sizes."""

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import llama_tune as t  # noqa: E402

GIB = 1024 ** 3


def bench_rows(rates):
    rows = []
    for ub, (pp, tg) in rates.items():
        rows.append({"n_ubatch": ub, "n_prompt": 2048, "n_gen": 0, "avg_ts": pp})
        rows.append({"n_ubatch": ub, "n_prompt": 0, "n_gen": 64, "avg_ts": tg})
    return "llama-bench says\n" + json.dumps(rows)


class MeasureTest(unittest.TestCase):
    def measure(self, rates, used_after, trained=131072):
        meta = {"general.architecture": "qwen3", "qwen3.context_length": trained}
        used = [4 * GIB]

        def run(argv, timeout, cancel):
            if "-ub" in argv and "," not in argv[argv.index("-ub") + 1]:
                used[0] = used_after                  # the memory run: the card fills up
            return bench_rows(rates), True
        with mock.patch.object(t, "bench_binary", return_value="/x/llama-bench"), \
                mock.patch.object(t.context_ctl, "read_gguf", return_value=meta), \
                mock.patch.object(t.context_ctl, "kv_layout", return_value=(64 * 1024, 40)):
            return t.measure("Qwen", "/m/q.gguf", "", run=run, totals=lambda: (used[0], 24 * GIB, 0.0))

    def test_the_fastest_batch_wins_within_three_percent_the_smaller(self):
        result = self.measure({512: (1000, 50), 1024: (1020, 50), 2048: (1500, 49)}, 10 * GIB)
        self.assertEqual(result["ubatch"], 2048)
        result = self.measure({512: (1000, 50), 1024: (1020, 50), 2048: (1025, 49)}, 10 * GIB)
        self.assertEqual(result["ubatch"], 512)
        self.assertEqual((result["pp"], result["tg"]), (1000, 50))

    def test_the_context_is_what_the_card_leaves(self):
        result = self.measure({512: (1000, 50), 1024: (900, 50), 2048: (800, 50)}, 10 * GIB)
        room = 24 * GIB - 4 * GIB - t.MARGIN - 6 * GIB
        self.assertEqual(result["ctx"], min(131072, (room // (64 * 1024) + t.PROMPT + t.MEM_GEN) // t.STEP * t.STEP))
        self.assertEqual(self.measure({512: (1, 1)}, 10 * GIB, trained=8192)["ctx"], 8192)   # trained limit
        self.assertEqual(self.measure({512: (1, 1)}, 24 * GIB)["reason"], "it does not fit on the card")

    def test_no_bench_no_measurement(self):
        with mock.patch.object(t, "bench_binary", return_value=None):
            self.assertIn("reason", t.measure("x", "/m/x.gguf"))

    def test_a_wrapper_script_names_its_llama_server(self):
        import tempfile, os
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "build" / "llama-server"
            real.parent.mkdir()
            real.write_bytes(b"\x7fELF")
            real.chmod(0o755)
            script = Path(tmp) / "llama-server"
            script.write_text(f"#!/bin/bash\nexec {real} --models-dir /m \"$@\"\n")
            self.assertEqual(t.real_server(str(script)), os.path.realpath(real))

    def test_only_programs_nobody_else_can_change(self):
        import os, stat, tempfile
        with tempfile.TemporaryDirectory() as folder:
            os.chmod(folder, 0o700)
            bench = os.path.join(folder, "llama-bench")
            Path(bench).write_text("#!/bin/sh\n")
            os.chmod(bench, 0o755)
            self.assertTrue(t.trusted(bench))
            os.chmod(bench, 0o775)                      # group-writable: another user could swap it
            self.assertFalse(t.trusted(bench))
            os.chmod(bench, 0o755)
            os.chmod(folder, 0o777)                     # its folder open to others
            self.assertFalse(t.trusted(bench))
            os.chmod(folder, 0o700)
        self.assertFalse(t.trusted(None))

    def test_another_users_llama_server_is_ignored(self):
        class Entry:
            name = "4242"
        real_stat = os.stat if (os := __import__("os")) else None

        def fake_stat(path, *a, **k):
            if path == "/proc/4242":
                return type("S", (), {"st_uid": os.getuid() + 1})()
            return real_stat(path, *a, **k)
        with mock.patch("os.scandir", return_value=[Entry()]), mock.patch("os.stat", fake_stat), \
                mock.patch("os.readlink", side_effect=AssertionError("another user's exe was read")), \
                mock.patch("shutil.which", return_value=None):
            self.assertIsNone(t.server_binary())

    def test_endless_output_is_cut(self):
        out, ok = t._run(["sh", "-c", "yes x"], 20)
        self.assertFalse(ok)
        self.assertLessEqual(len(out), t.MAX_OUTPUT)

    def test_the_ini_gets_context_and_batches(self):
        text = t.tuned_ini("", {"name": "Qwen", "ctx": 32768, "ubatch": 1024, "batch": 2048})
        self.assertIn("[Qwen]", text)
        for line in ("ctx-size = 32768", "ubatch-size = 1024", "batch-size = 2048"):
            self.assertIn(line, text)


if __name__ == "__main__":
    unittest.main()
