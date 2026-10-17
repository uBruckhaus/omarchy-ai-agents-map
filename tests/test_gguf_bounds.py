import struct
import sys
import tempfile
import tracemalloc
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import context_ctl  # noqa: E402


def _str(text: str) -> bytes:
    data = text.encode()
    return struct.pack("<Q", len(data)) + data


def _kv(key: str, kind: int, payload: bytes) -> bytes:
    return _str(key) + struct.pack("<I", kind) + payload


def gguf(*pairs: bytes) -> bytes:
    return b"GGUF" + struct.pack("<IQQ", 3, 0, len(pairs)) + b"".join(pairs)


class GgufBoundsTest(unittest.TestCase):
    """A downloaded model header is untrusted: tiny files must not make the window allocate gigabytes."""

    def _write(self, data: bytes) -> Path:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "tiny.gguf"
        path.write_bytes(data)
        return path

    def _llama(self, *extra: bytes) -> Path:
        return self._write(gguf(_kv("general.architecture", 8, _str("llama")), *extra))

    def test_billion_layers_refused_without_allocating(self):
        path = self._llama(_kv("llama.block_count", 4, struct.pack("<I", 1_000_000_000)),
                           _kv("llama.attention.head_count_kv", 4, struct.pack("<I", 8)))
        self.assertLess(path.stat().st_size, 200)
        tracemalloc.start()
        try:
            with self.assertRaises(ValueError):
                context_ctl.fit(path, 24 * context_ctl.GiB)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 10 * 2 ** 20)

    def test_negative_or_odd_block_count_refused(self):
        for meta in ({"general.architecture": "x", "x.block_count": -1},
                     {"general.architecture": "x", "x.block_count": [1, 2]},
                     {"general.architecture": "x", "x.block_count": 1e12}):
            with self.assertRaises(ValueError):
                context_ctl.kv_layout(meta)

    def test_kept_array_too_long_refused(self):
        too_long = context_ctl.MAX_ARRAY + 1
        path = self._llama(_kv("llama.attention.head_count_kv", 9,
                               struct.pack("<IQ", 4, too_long) + b"\0" * 4 * too_long))
        with self.assertRaises(ValueError):
            context_ctl.read_gguf(path)

    def test_kept_array_of_strings_refused(self):
        path = self._llama(_kv("llama.attention.head_count_kv", 9, struct.pack("<IQ", 8, 2) + _str("a") + _str("b")))
        with self.assertRaises(ValueError):
            context_ctl.read_gguf(path)

    def test_deeply_nested_skipped_array_refused(self):
        nested = struct.pack("<IQ", 9, 1) * 50 + struct.pack("<IQ", 0, 0)
        path = self._llama(_kv("tokenizer.ggml.whatever", 9, nested))
        with self.assertRaises(ValueError):
            context_ctl.read_gguf(path)

    def test_many_matching_string_values_not_kept(self):
        big = "x" * (1 << 20)
        path = self._llama(*(_kv(f"k{i}.block_count", 8, _str(big)) for i in range(64)))
        tracemalloc.start()
        try:
            meta = context_ctl.read_gguf(path)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertEqual(meta, {"general.architecture": "llama"})
        self.assertLess(peak, 4 * 2 ** 20)

    def test_too_many_matching_values_refused(self):
        path = self._llama(*(_kv(f"k{i}.block_count", 4, struct.pack("<I", 32))
                             for i in range(context_ctl.MAX_KEPT + 1)))
        with self.assertRaises(ValueError):
            context_ctl.read_gguf(path)

    def test_long_architecture_refused(self):
        path = self._write(gguf(_kv("general.architecture", 8, _str("x" * (context_ctl.MAX_ARCH + 1)))))
        with self.assertRaises(ValueError):
            context_ctl.read_gguf(path)

    def test_plausible_header_still_fits(self):
        layers = 32
        path = self._llama(_kv("llama.block_count", 4, struct.pack("<I", layers)),
                           _kv("llama.context_length", 4, struct.pack("<I", 131072)),
                           _kv("llama.embedding_length", 4, struct.pack("<I", 4096)),
                           _kv("llama.attention.head_count", 4, struct.pack("<I", 32)),
                           _kv("llama.attention.head_count_kv", 9,
                               struct.pack("<IQ", 4, layers) + struct.pack("<I", 8) * layers),
                           _kv("tokenizer.ggml.tokens", 9, struct.pack("<IQ", 8, 3) + _str("a") + _str("b") + _str("c")))
        meta = context_ctl.read_gguf(path)
        self.assertEqual(meta["llama.block_count"], layers)
        # 32 layers × 8 KV heads × (128 + 128) × 2 bytes
        self.assertEqual(context_ctl.kv_layout(meta), (32 * 8 * 256 * 2, 0))


if __name__ == "__main__":
    unittest.main()
