import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import isolate  # noqa: E402
import connect  # noqa: E402
import gpu_usage  # noqa: E402
import install_ctl  # noqa: E402
import inventory  # noqa: E402
import jan_ctl  # noqa: E402
import kobold_ctl  # noqa: E402
import provider_watch  # noqa: E402

PRESET = """[*]
load-on-startup = false

[gemma-4-e2b]
model = {model}
ctx-size = 8192
"""


class Home(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        isolate.desktop_in(self, self.home)
        patches = [mock.patch.object(inventory, "HOME", self.home),
                   mock.patch.object(inventory, "JAN_ROOT", self.home / ".local/share/Jan"),
                   mock.patch.object(inventory, "KOBOLD_CONFIGS", self.home / ".config/koboldcpp/models"),
                   mock.patch.object(connect, "HOME", self.home)]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def gguf(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"GGUF not really")
        return path


class JanTest(Home):
    def make_jan(self):
        data = self.home / ".local/share/Jan/data"
        model = self.gguf(self.home / "models/gemma.gguf")
        (data / "llamacpp/models/gemma-4-e2b").mkdir(parents=True)
        (data / "llamacpp/models/gemma-4-e2b/model.yml").write_text(f"model_path: {model}\nname: gemma\n")
        (data / "llamacpp/models/janhq/Jan-v3").mkdir(parents=True)
        (data / "llamacpp/models/janhq/Jan-v3/model.yml").write_text("model_path: llamacpp/models/janhq/x.gguf\n")
        (data / "llamacpp/router.preset.ini").write_text(PRESET.format(model=model))
        return data, model

    def test_inventory_and_settings(self):
        data, model = self.make_jan()
        rt = inventory.jan([])
        self.assertEqual(rt.models, ["gemma-4-e2b", "janhq/Jan-v3"])
        self.assertTrue(rt.installed)
        self.assertEqual(rt.address, "127.0.0.1:6767")
        self.assertEqual(jan_ctl.settings("gemma-4-e2b"), (8192, "f16", Path(os.path.realpath(model))))
        self.assertIsNone(jan_ctl.model_yml("../../etc"))

    def test_data_folder_from_settings(self):
        root = self.home / ".local/share/Jan"
        root.mkdir(parents=True)
        (root / "settings.json").write_text(json.dumps({"data_folder": "/srv/jan"}))
        self.assertEqual(inventory.jan_data_folder(), Path("/srv/jan"))
        (root / "settings.json").write_text(json.dumps({"data_folder": "./data"}))
        self.assertEqual(inventory.jan_data_folder(), root / "data")

    def test_context_goes_into_preset_and_model_yml(self):
        data, _model = self.make_jan()
        backup = connect.apply(connect.Plan("Jan", "jan", "", jan_ctl.context_changes("gemma-4-e2b", 32768)),
                               self.home / "backups")
        self.assertEqual(jan_ctl.settings("gemma-4-e2b")[0], 32768)
        self.assertIn("ctx_size: 32768", (data / "llamacpp/models/gemma-4-e2b/model.yml").read_text())
        connect.undo(backup)
        self.assertEqual(jan_ctl.settings("gemma-4-e2b")[0], 8192)
        self.assertNotIn("ctx_size", (data / "llamacpp/models/gemma-4-e2b/model.yml").read_text())

    def test_unit_paths_never_become_shell_code(self):
        """A folder with ; and spaces stays one argument: the worker runs, nothing else does."""
        import shlex
        import subprocess
        folder = self.home / "a b;touch PWNED;x"
        worker_dir = folder / "current" / install_ctl.Jan.worker_rel
        worker_dir.mkdir(parents=True)
        worker = worker_dir / "jan-llama-worker"
        worker.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$(dirname \"$0\")/args\"\n")
        worker.chmod(0o755)
        with mock.patch.object(install_ctl.Jan, "installed_folder", lambda self: folder):
            text = install_ctl.Jan().unit_text()
        line = next(l for l in text.splitlines() if l.startswith("ExecStart="))[len("ExecStart="):]
        argv = shlex.split(line)                 # systemd's quoting rules for these characters
        argv[2] = argv[2].replace("sleep infinity", "true")
        subprocess.run(argv, cwd=self.home, check=True, timeout=10)
        self.assertFalse((self.home / "PWNED").exists())
        self.assertFalse(any(self.home.rglob("PWNED")))
        args = (worker_dir / "args").read_text().splitlines()
        self.assertEqual(args[:2], ["--preset", str(inventory.jan_preset())])
        with self.assertRaises(ValueError):
            install_ctl.unit_path('/x/"; touch y')

    def test_engine_process_is_jan_not_llamacpp(self):
        self.assertEqual(gpu_usage.runtime_of("jan-llama-worke", ["/x/jan-llama-worker", "--port", "6767"]), "jan")
        rt = inventory.jan([(1, "jan-llama-worke", ["/x/jan-llama-worker", "--preset", "p", "--port", "6767"])])
        self.assertTrue(rt.running)
        other = inventory.jan([(1, "jan-llama-worke", ["/x/jan-llama-worker", "--port", "39271"])])
        self.assertFalse(other.running)      # the Jan app's own engine on its own port

    def test_unit_keeps_input_open(self):
        with mock.patch.object(install_ctl.Jan, "installed_folder", lambda self: Path("/opt/jan")):
            text = install_ctl.Jan().unit_text()
        self.assertIn("""sleep infinity | exec "$0" --preset "$1" """.strip(), text)
        self.assertIn('\' "/opt/jan/current/squashfs-root/usr/lib/Jan/resources/bin/jan-llama-worker" ', text)
        self.assertIn("--port 6767", text)
        self.assertIn("KillMode=control-group", text)


class KoboldTest(Home):
    def test_sync_one_config_per_model(self):
        models = self.home / "Models"
        self.gguf(models / "gemma-4-E2B-it-Q4_K_M.gguf")
        self.gguf(models / "mmproj-gemma.gguf")
        self.gguf(models / "big-00001-of-00002.gguf")
        self.gguf(models / "big-00002-of-00002.gguf")
        self.gguf(models / "repo/inner-Q4.gguf")
        configs = inventory.KOBOLD_CONFIGS
        written = kobold_ctl.sync(models, "vulkan", lambda: 24 * 1024 ** 3)
        self.assertEqual(sorted(written), ["big.kcpps", "gemma-4-E2B-it-Q4_K_M.kcpps", "inner-Q4.kcpps"])
        entry = json.loads((configs / "gemma-4-E2B-it-Q4_K_M.kcpps").read_text())
        self.assertEqual(entry["contextsize"], kobold_ctl.DEFAULT_CONTEXT)    # not a real GGUF: the default
        self.assertEqual(entry["usevulkan"], [])
        self.assertEqual(kobold_ctl.settings("gemma-4-E2B-it-Q4_K_M.kcpps")[0], 8192)
        # A model gone: our config goes, the user's own stays.
        (configs / "mine.kcpps").write_text(json.dumps({"model_param": "/nowhere.gguf"}))
        (models / "gemma-4-E2B-it-Q4_K_M.gguf").unlink()
        kobold_ctl.sync(models, "vulkan", 0)
        names = sorted(p.name for p in configs.glob("*.kcpps"))
        self.assertEqual(names, ["big.kcpps", "inner-Q4.kcpps", "mine.kcpps"])
        rt = inventory.koboldcpp([])
        self.assertEqual(rt.models, names)

    def test_context_text(self):
        old = json.dumps({"model_param": "/m.gguf", "contextsize": 8192, "usevulkan": []})
        self.assertEqual(json.loads(kobold_ctl.context_text(old, 65536))["contextsize"], 65536)
        with self.assertRaises(ValueError):
            kobold_ctl.context_text(old, 10 ** 7)

    def test_unit_is_a_router(self):
        with mock.patch.object(install_ctl.KoboldCpp, "build_of", lambda self: "vulkan"):
            text = install_ctl.KoboldCpp().unit_text()
        for flag in ("--admin", "--routermode", "--nomodel", "--skiplauncher", "--usevulkan", "--port 5001",
                     str(inventory.KOBOLD_CONFIGS)):
            self.assertIn(flag, text)

    def test_loaded_model_from_router_list(self):
        rt = inventory.Runtime("koboldcpp", "KoboldCpp", address="127.0.0.1:5001")
        reply = {"data": [{"id": "koboldcpp/gemma-4-E2B-it-Q4_K_M"}, {"id": "gemma-4-E2B-it-Q4_K_M.kcpps"}]}
        with mock.patch.object(gpu_usage, "http_json", lambda *a, **k: reply):
            self.assertEqual(gpu_usage.loaded_models(rt), {"gemma-4-e2b-it-q4_k_m.kcpps": "loaded"})
        with mock.patch.object(gpu_usage, "http_json", lambda *a, **k: {"data": [{"id": "inactive"}]}):
            self.assertEqual(gpu_usage.loaded_models(rt), {})


class WatcherTest(unittest.TestCase):
    def test_own_listener(self):
        with tempfile.TemporaryDirectory() as tmp:
            uid = os.getuid()
            header = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid\n"
            line = f"   0: 0100007F:1389 00000000:0000 0A 00000000:00000000 00:00000000 00000000 {{uid}} 0 1\n"
            Path(tmp, "tcp6").write_text(header)
            Path(tmp, "tcp").write_text(header + line.format(uid=uid))
            self.assertTrue(provider_watch.own_listener(5001, tmp))
            Path(tmp, "tcp").write_text(header + line.format(uid=uid) + line.format(uid=uid + 1))
            self.assertFalse(provider_watch.own_listener(5001, tmp))     # another account listens too
            self.assertFalse(provider_watch.own_listener(5002, tmp))

    def test_stays_up_is_unloaded_not_stopped(self):
        watcher = provider_watch.Watcher.__new__(provider_watch.Watcher)
        request = {"path": "/api/admin/reload_config", "body": {"filename": "unload_model"}}
        watcher.runtimes = {"koboldcpp": {"service": "koboldcpp", "port": 5001, "stays_up": True, "unload": request}}
        watcher.known, watcher.started, watcher.background = {}, {}, set()
        watcher.busy = lambda *a: False
        watcher.sockets = lambda *a: set()
        ran = []
        watcher.run = ran.append
        with mock.patch.object(provider_watch, "unload", lambda entry, port: (entry["unload"], port) == (request, 5001)):
            self.assertTrue(watcher.stop("koboldcpp", "test"))
        self.assertEqual(ran, [])                      # no systemctl stop


if __name__ == "__main__":
    unittest.main()
