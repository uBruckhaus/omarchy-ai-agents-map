import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import agent_details  # noqa: E402
import agent_readers  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "adapters"))
import omp  # noqa: E402


class OmpReaderTest(unittest.TestCase):
    def test_reads_what_connecting_writes(self):
        # What omp.py writes when Oh My Pi is connected to llama.cpp, read back for the map.
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "models.yml").write_text(omp.models_text(
                "", "llama-local", "http://127.0.0.1:8080/v1", ["Qwen3-8B", "gemma-4"], {"Qwen3-8B": 32768}))
            (root / "config.yml").write_text(omp.config_text("", "llama-local", "Qwen3-8B", True))
            details = agent_details.Details()
            agent_readers.pi_like(root, details)
        self.assertEqual(details.model, "Qwen3-8B (llama-local)")
        self.assertEqual([(p.name, p.active) for p in details.providers], [("llama-local", True)])

    def test_no_yaml_no_providers(self):
        with tempfile.TemporaryDirectory() as folder:
            details = agent_details.Details()
            agent_readers.pi_like(Path(folder), details)
        self.assertEqual((details.model, details.providers), ("", []))


if __name__ == "__main__":
    unittest.main()
