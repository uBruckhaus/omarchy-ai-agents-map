"""HANCORE review of 3452749: a program folder others can change is refused; the archive that
is unpacked is the one whose checksum was checked; downloads have curl deadlines."""

import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import install_ctl as ic  # noqa: E402


class TrustTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_private_folder_and_missing_one_are_fine(self):
        ic.check_trusted(self.root)
        ic.check_trusted(self.root / "new" / "deeper")

    def test_group_or_world_writable_program_folder_refused(self):
        shared = self.root / "shared"
        shared.mkdir()
        for mode in (0o775, 0o777, 0o1777):         # sticky does not help the program folder itself
            shared.chmod(mode)
            with self.assertRaises(RuntimeError):
                ic.check_trusted(shared)

    def test_writable_ancestor_needs_the_sticky_bit(self):
        parent = self.root / "parent"
        (parent / "prog").mkdir(parents=True)
        parent.chmod(0o777)
        with self.assertRaises(RuntimeError):
            ic.check_trusted(parent / "prog")
        with self.assertRaises(RuntimeError):     # not created yet: refused before creating, too
            ic.check_trusted(parent / "other")
        parent.chmod(0o1777)
        ic.check_trusted(parent / "prog")
        parent.chmod(0o755)

    def test_other_owner_refused(self):
        real_lstat = os.lstat

        def lstat(path, *a, **k):
            st = real_lstat(path, *a, **k)
            if str(path) == str(self.root):
                values = list(st)
                values[4] = os.getuid() + 12345      # st_uid
                return os.stat_result(values)
            return st
        with mock.patch.object(ic.os, "lstat", lstat):
            with self.assertRaises(RuntimeError):
                ic.check_trusted(self.root / "prog")

    def test_claim_refuses_shared_folder(self):
        shared = self.root / "shared"
        shared.mkdir()
        shared.chmod(0o777)
        with self.assertRaises(RuntimeError):
            ic.Ollama().claim(shared)
        self.assertEqual(list(shared.iterdir()), [])     # no marker written
        shared.chmod(0o755)


class VerifiedTest(unittest.TestCase):
    def test_replaced_after_the_check_the_checked_bytes_are_read(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        good = Path(tmp.name) / "release.tar.zst"
        good.write_bytes(b"verified bytes")
        digest = hashlib.sha256(b"verified bytes").hexdigest()
        verified = ic.open_verified(good, digest)
        try:
            evil = Path(tmp.name) / "evil"
            evil.write_bytes(b"other bytes")
            os.replace(evil, good)                     # swapped right before unpacking
            self.assertEqual(verified.name, "release.tar.zst")
            self.assertEqual(Path(os.fspath(verified)).read_bytes(), b"verified bytes")
            self.assertEqual(verified.read_bytes(), b"verified bytes")
        finally:
            os.close(verified.fd)

    def test_wrong_bytes_or_link_refused(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        file = Path(tmp.name) / "a"
        file.write_bytes(b"x")
        with self.assertRaises(RuntimeError):
            ic.open_verified(file, hashlib.sha256(b"y").hexdigest())
        link = Path(tmp.name) / "b"
        link.symlink_to(file)
        with self.assertRaises(RuntimeError):
            ic.open_verified(link, hashlib.sha256(b"x").hexdigest())


class CurlDeadlineTest(unittest.TestCase):
    def test_download_argv_has_connect_stall_and_total_deadlines(self):
        seen = {}

        class Proc:
            returncode = 1

            def __init__(self, argv, **kwargs):
                seen["argv"] = argv
                self.stderr = open(os.devnull, "rb")

            def poll(self):
                return 1

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with mock.patch.object(ic.subprocess, "Popen", Proc):
            with self.assertRaises(ic.DownloadError):
                ic.download("https://example.invalid/x", Path(tmp.name) / "x", 10, lambda *a: None, lambda: False)
        argv = seen["argv"]
        for flag in ("--connect-timeout", "--speed-limit", "--speed-time", "--max-time"):
            self.assertIn(flag, argv)


if __name__ == "__main__":
    unittest.main()
