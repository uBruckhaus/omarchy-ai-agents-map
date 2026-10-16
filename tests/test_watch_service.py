import ast
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import provider_watch  # noqa: E402


class WatchServiceTest(unittest.TestCase):
    """The watcher runs as `python3 -I provider_watch.py`: the plugin folder is not on
    sys.path there, so it must start with the standard library alone."""

    def test_top_level_imports_are_standard_library(self):
        tree = ast.parse((ROOT / "provider_watch.py").read_text())
        names = {a.name.split(".")[0] for n in tree.body if isinstance(n, ast.Import) for a in n.names}
        names |= {n.module.split(".")[0] for n in tree.body if isinstance(n, ast.ImportFrom) and n.module}
        self.assertTrue(names <= set(sys.stdlib_module_names), names - set(sys.stdlib_module_names))

    def test_loads_in_isolated_mode(self):
        path = ROOT / "provider_watch.py"
        code = (f"import runpy; g = runpy.run_path({str(path)!r}, run_name='probe'); "
                "print(g['config_from'](['x', '--config', '/tmp/c.json']), g['CONFIG'].name)")
        result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=30,
                                cwd="/")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(), ["/tmp/c.json", "providers-with-agents.json"])

    def test_unit_passes_the_config_file(self):
        text = provider_watch.unit_text(Path("/p/provider_watch.py"), Path("/home/u/.config/asterism/w.json"))
        self.assertIn("ExecStart=/usr/bin/python3 -I /p/provider_watch.py --config /home/u/.config/asterism/w.json\n", text)
        odd = provider_watch.unit_text(Path("/p/provider_watch.py"), Path("/home/u/my dir/w.json"))
        self.assertIn("ExecStart=/usr/bin/python3 -I /p/provider_watch.py\n", odd)
        self.assertEqual(provider_watch.config_from(["x", "--config", "relative.json"]), provider_watch.CONFIG)
        self.assertEqual(provider_watch.config_from(["x"]), provider_watch.CONFIG)


    def test_asterism_has_its_own_unit(self):
        """The plugin and a standalone Asterism on one machine never share the watcher unit."""
        from unittest import mock
        import desktop
        import desktop_generic
        import desktop_omarchy
        with mock.patch.object(desktop, "current", return_value=desktop_omarchy):
            self.assertEqual(provider_watch._unit_name(), "ai-agents-map-providers.service")
        with mock.patch.object(desktop, "current", return_value=desktop_generic):
            self.assertEqual(provider_watch._unit_name(), "asterism-providers.service")


if __name__ == "__main__":
    unittest.main()
