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
        return ic.Release("v9.9.9", [("ollama-linux-amd64.tgz", "https://example.invalid/x", archive.stat().st_size)],
                          {"ollama-linux-amd64.tgz": ic.sha256_of(archive)}), archive

    def install(self, folder, models):
        release, archive = self.fake_release()

        def fake_download(url, target, size, progress, cancelled, done_before=0, total=0, sha256=""):
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
        self.assertIn(f'ExecStart="{self.home}/.local/bin/ollama" serve', unit)
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


class LlamaInstallTest(InstallTest):
    def test_llamacpp_flatten_env_unit_and_preset(self):
        inst = ic.LlamaCpp()
        inst.env_file = self.home / ".config/llama.cpp/server.env"
        src = self.home / "src2/llama-b1"
        src.mkdir(parents=True)
        (src / "llama-server").write_text("#!/bin/sh\n")
        archive = self.home / "llama.tgz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(src, arcname="llama-b1")
        release = ic.Release("b1-vulkan", [("llama.tgz", "https://example.invalid/l", archive.stat().st_size)],
                             {"llama.tgz": ic.sha256_of(archive)})

        def fake_download(url, target, size, progress, cancelled, done_before=0, total=0, sha256=""):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read_bytes())

        with mock.patch.object(ic, "download", fake_download), \
                mock.patch.object(ic, "HOME", self.home):
            inst.install(self.home / "llama", self.home / "Models", False, False, ic.Gpu("amd", index=0, count=2),
                         release, lambda *a: None, lambda: False)
        self.assertTrue((self.home / "llama/b1-vulkan/llama-server").exists())
        self.assertEqual(inst.installed_folder(), self.home / "llama")
        self.assertEqual(ic.read_env(inst.env_file)["LLAMA_ARG_MODELS_DIR"], str(self.home / "Models"))
        unit = (self.home / ".config/systemd/user/llama-server.service").read_text()
        self.assertIn("--models-preset", unit)

    def test_default_builds(self):
        llama = ic.LlamaCpp()
        self.assertEqual(llama.default_build(ic.Gpu("nvidia")), "cuda")
        self.assertEqual(llama.default_build(ic.Gpu("cpu")), "cpu")
        self.assertIn(llama.default_build(ic.Gpu("amd")), ("vulkan", "rocm"))

    def test_vllm_refuses_amd(self):
        with self.assertRaises(RuntimeError):
            ic.Vllm().latest(ic.Gpu("amd"))


class FakeCurl:
    """Stands in for curl: writes the given bytes to the -o file."""
    def __init__(self, body: bytes):
        self.body = body

    def __call__(self, argv, **_kw):
        Path(argv[argv.index("-o") + 1]).write_bytes(self.body)
        proc = mock.Mock(returncode=0)
        proc.poll.return_value = 0
        return proc


class IntegrityTest(unittest.TestCase):
    """Every downloaded program is bound to the SHA-256 its release lists before
    anything unpacks or runs it; without a checksum nothing is downloaded."""
    GOOD = b"genuine release bytes"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        import hashlib
        self.digest = hashlib.sha256(self.GOOD).hexdigest()

    def fetch(self, body: bytes) -> Path:
        target = self.home / "downloads/app.AppImage"
        with mock.patch.object(ic.subprocess, "Popen", FakeCurl(body)):
            ic.download("https://example.invalid/app", target, len(self.GOOD), lambda *a: None, lambda: False,
                        sha256=self.digest)
        return target

    def test_matching_bytes_are_kept(self):
        self.assertEqual(self.fetch(self.GOOD).read_bytes(), self.GOOD)

    def test_substituted_same_size_download_is_refused(self):
        evil = b"x" * len(self.GOOD)
        with self.assertRaises(RuntimeError):
            self.fetch(evil)
        self.assertEqual(list((self.home / "downloads").iterdir()), [])   # neither the file nor the .part

    def test_github_asset_needs_a_sha256(self):
        asset = {"name": "Jan_1.0.0_amd64.AppImage", "browser_download_url": "https://github.com/x", "size": 5}
        for digest in (None, "", "md5:abc", "sha256:" + "0" * 63, "sha256:" + "G" * 64):
            with self.assertRaises(RuntimeError):
                ic.github_asset({**asset, "digest": digest})
        self.assertEqual(ic.github_asset({**asset, "digest": "sha256:" + self.digest})[3], self.digest)

    def test_jan_and_kobold_releases_carry_digests(self):
        jan = {"tag_name": "v1.0.0", "assets": [{"name": "Jan_1.0.0_amd64.AppImage", "size": 5,
                                                 "browser_download_url": "https://github.com/j",
                                                 "digest": "sha256:" + self.digest}]}
        with mock.patch.object(ic, "_curl_json", return_value=jan):
            release = ic.Jan().latest(ic.Gpu())
        self.assertEqual(release.digests, {"Jan_1.0.0_amd64.AppImage": self.digest})
        del jan["assets"][0]["digest"]
        with mock.patch.object(ic, "_curl_json", return_value=jan), self.assertRaises(RuntimeError):
            ic.Jan().latest(ic.Gpu())

    def test_install_without_digest_downloads_and_runs_nothing(self):
        release = ic.Release("v1.0.0", [("Jan_1.0.0_amd64.AppImage", "https://github.com/j", 5)])
        with mock.patch.object(ic, "download") as download, mock.patch.object(ic.ai_core, "read_bounded_output") as run, \
                self.assertRaises(RuntimeError):
            ic.Jan().install(self.home / "jan", self.home / "data", False, False, ic.Gpu(), release,
                             lambda *a: None, lambda: False)
        download.assert_not_called()
        run.assert_not_called()

    def test_lm_studio_and_bionic_are_not_installed_from_here(self):
        for inst in (ic.LmStudio(), ic.Bionic()):
            self.assertFalse(inst.can_install)
            with self.assertRaises(NotImplementedError):
                inst.latest(ic.Gpu())
            release = ic.Release("v1", [("a.AppImage", "https://installers.lmstudio.ai/a", 5)], {"a.AppImage": self.digest})
            with mock.patch.object(ic, "download") as download, self.assertRaises(RuntimeError):
                inst.install(self.home / inst.key, self.home / "m", False, False, ic.Gpu(), release,
                             lambda *a: None, lambda: False)
            download.assert_not_called()
