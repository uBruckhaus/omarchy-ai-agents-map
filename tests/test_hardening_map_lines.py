"""0.17.0 review hardening: model names to lms, links and models.ini in shared model folders,
where a download may go."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import model_folders  # noqa: E402
import models_ctl  # noqa: E402
import runtime_ctl  # noqa: E402


class ModelNameTest(unittest.TestCase):
    def test_a_name_that_looks_like_an_option_never_reaches_lms(self):
        rt = SimpleNamespace(key="lmstudio", address="127.0.0.1:1234", service="")
        with mock.patch.object(runtime_ctl, "_run", side_effect=AssertionError("run")), \
                mock.patch.object(runtime_ctl, "lms_binary", return_value="/usr/bin/lms"):
            for bad in ("--yes", "-x", "", "a b", "a\nb"):
                self.assertEqual(runtime_ctl.unload_model(rt, bad), (False, "not a model name"))
                self.assertEqual(runtime_ctl.load(rt, bad), (False, "not a model name"))

    def test_a_plain_name_goes_after_double_dash(self):
        rt = SimpleNamespace(key="lmstudio", address="127.0.0.1:1234", service="")
        with mock.patch.object(runtime_ctl, "_run", return_value=(True, "")) as run, \
                mock.patch.object(runtime_ctl, "lms_binary", return_value="/usr/bin/lms"):
            runtime_ctl.unload_model(rt, "qwen/qwen3-8b")
        run.assert_called_once_with(["/usr/bin/lms", "unload", "--", "qwen/qwen3-8b"])


class SharedFolderTest(unittest.TestCase):
    def test_relink_takes_only_own_links_and_never_a_planted_temp_name(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            (base / "a.gguf").write_bytes(b"a")
            (base / "b.gguf").write_bytes(b"b")
            link = base / "m.gguf"
            link.symlink_to(base / "a.gguf")
            planted = base / ".m.gguf.relink"
            planted.write_text("someone else's")
            model_folders._relink(link, base / "b.gguf")
            self.assertEqual(os.readlink(link), str(base / "b.gguf"))
            self.assertEqual(planted.read_text(), "someone else's")        # left alone
            with self.assertRaises(OSError):
                model_folders._relink(base / "a.gguf", base / "b.gguf")    # a real file, not a link

    def test_models_ini_is_not_written_through_a_planted_link(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            ini = base / "models.ini"
            ini.write_text("[m]\nmodel = /old/m.gguf\n")
            os.chmod(ini, 0o600)
            victim = base / "victim"
            victim.write_text("keep")
            (base / ".models.ini.new").symlink_to(victim)
            move = SimpleNamespace(steps=[(Path("/old/m.gguf"), Path("/new/m.gguf"))])
            change = model_folders.Change(move=move, ini=True)
            with mock.patch.object(model_folders, "BACKUPS", base / "backups"):
                self.assertEqual(model_folders.fix(change, ini), [])
            self.assertIn("/new/m.gguf", ini.read_text())
            self.assertEqual(victim.read_text(), "keep")
            self.assertEqual(ini.stat().st_mode & 0o777, 0o600)

    def test_a_download_never_goes_into_a_folder_others_can_write(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            shared_a, shared_b = base / "shared" / "x", base / "shared" / "y"
            shared_a.mkdir(parents=True)
            shared_b.mkdir(parents=True)
            (shared_a / "one.gguf").write_bytes(b"1")
            (shared_b / "two.gguf").write_bytes(b"2")
            router = base / "router"
            router.mkdir()
            (router / "one.gguf").symlink_to(shared_a / "one.gguf")
            (router / "two.gguf").symlink_to(shared_b / "two.gguf")
            self.assertEqual(models_ctl.gguf_target("llamacpp", router, "pub/repo"),
                             (base / "shared" / "pub" / "repo", router))
            os.chmod(base / "shared", 0o777)                             # now another account may write there
            try:
                self.assertEqual(models_ctl.gguf_target("llamacpp", router, "pub/repo"), (router, None))
            finally:
                os.chmod(base / "shared", 0o755)


if __name__ == "__main__":
    unittest.main()
