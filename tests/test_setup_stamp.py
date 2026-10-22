import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import agent_details  # noqa: E402
import harnesses  # noqa: E402


class SetupStampTest(unittest.TestCase):
    """An agent's model or providers changed in its own settings (outside the map) change the stamp
    the map's poll compares, so the map shows the new model while it is open."""

    def stamp(self, model, providers=(), fail=False):
        def details(d):
            if fail:
                raise RuntimeError("unknown format")
            d.model = model
            d.providers = [agent_details.Provider(name=p) for p in providers]
        fake = {"a": types.SimpleNamespace(details=details), "b": types.SimpleNamespace(details=None)}
        with mock.patch.object(harnesses, "get", side_effect=fake.get):
            return agent_details.setup_stamp(["a", "b", "missing"])

    def test_same_settings_same_stamp(self):
        self.assertEqual(self.stamp("opus", ["ollama"]), self.stamp("opus", ["ollama"]))

    def test_changed_model_or_provider_changes_it(self):
        self.assertNotEqual(self.stamp("opus"), self.stamp("sonnet"))
        self.assertNotEqual(self.stamp("opus", ["ollama"]), self.stamp("opus", ["llama-local"]))

    def test_a_reader_that_fails_is_left_out(self):
        self.assertEqual(self.stamp("opus", fail=True), ())


if __name__ == "__main__":
    unittest.main()
