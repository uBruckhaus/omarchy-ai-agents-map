import os
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import install_ctl as ic  # noqa: E402


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        for name, value in (("HOME", self.home), ("BIN_DIR", self.home / ".local/bin"),
                            ("UNIT_DIR", self.home / ".config/systemd/user")):
            patcher = mock.patch.object(ic, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.calls = []
        patcher = mock.patch.object(ic, "_systemctl", lambda *a: (self.calls.append(a), (True, ""))[1])
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ollama = ic.Ollama()
        self.ollama.env_file = self.home / ".config/ollama/server.env"

    def fake_release(self):
        """A local tar.zst... as .tgz with bin/ollama, served by a fake download."""
        src = self.home / "src"
        (src / "bin").mkdir(parents=True)
        (src / "bin/ollama").write_text("#!/bin/sh\n")
        archive = self.home / "ollama-linux-amd64.tgz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(src / "bin", arcname="bin")
        return ic.Release("v9.9.9", [("ollama-linux-amd64.tgz", "https://example.invalid/x", archive.stat().st_size)]), archive

    def install(self, folder, models):
        release, archive = self.fake_release()

        def fake_download(url, target, size, progress, cancelled, done_before=0, total=0):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read_bytes())

        with mock.patch.object(ic, "download", fake_download):
            self.ollama.install(folder, models, True, False, ic.Gpu("amd", index=0, count=2), release,
                                lambda *a: None, lambda: False)

    def test_install_layout_env_and_service(self):
        folder, models = self.home / "ai/ollama", self.home / "ai/Models"
        self.install(folder, models)
        self.assertEqual(os.readlink(folder / "current"), "v9.9.9")
        self.assertEqual(Path(os.readlink(self.home / ".local/bin/ollama")), folder / "current/bin/ollama")
        self.assertEqual(self.ollama.installed_folder(), folder)
        env = ic.read_env(self.ollama.env_file)
        self.assertEqual((env["OLLAMA_MODELS"], env["HIP_VISIBLE_DEVICES"]), (str(models), "0"))
        unit = (self.home / ".config/systemd/user/ollama.service").read_text()
        self.assertIn(f"ExecStart={self.home}/.local/bin/ollama serve", unit)
        self.assertIn(("enable", "ollama.service"), self.calls)
        self.assertFalse((folder / "downloads").exists())

    def test_change_models_folder_keeps_other_lines(self):
        self.install(self.home / "o", self.home / "m1")
        self.ollama.set_models_folder(self.home / "m2")
        env = ic.read_env(self.ollama.env_file)
        self.assertEqual(env["OLLAMA_MODELS"], str(self.home / "m2"))
        self.assertEqual(env["OLLAMA_HOST"], "127.0.0.1:11434")

    def test_uninstall_keeps_models_even_inside(self):
        folder = self.home / "o"
        self.install(folder, folder / "models")
        (folder / "models/blob").write_text("x")
        self.ollama.uninstall()
        self.assertTrue((folder / "models/blob").exists())
        self.assertFalse((folder / "v9.9.9").exists())
        self.assertFalse((self.home / ".local/bin/ollama").exists())
        self.assertFalse(self.ollama.env_file.exists())

    def test_hand_install_is_not_touched(self):
        (self.home / ".local/bin").mkdir(parents=True)
        (self.home / ".local/bin/ollama").symlink_to("/opt/elsewhere/bin/ollama")
        self.assertIsNone(self.ollama.installed_folder())
        with self.assertRaises(RuntimeError):
            self.ollama.uninstall()

    def test_check_folder(self):
        for bad in ("", "relative/path", "/usr/local/x", "/etc/x", "/", "a\nb"):
            with self.assertRaises(ValueError):
                ic.check_folder(bad)
        self.assertEqual(ic.check_folder(str(self.home / "x")), self.home / "x")

    def test_models_root(self):
        self.assertEqual(ic._models_root(Path("/mnt/ai/Models/llama.cpp-router")), Path("/mnt/ai/Models"))


if __name__ == "__main__":
    unittest.main()
