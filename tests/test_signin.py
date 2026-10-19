import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import isolate  # noqa: E402,F401
import agent_status  # noqa: E402
import signin  # noqa: E402

KEY = "sk-test-0123456789abcdef"


class KeyTest(unittest.TestCase):
    def test_only_one_word_of_plain_characters(self):
        self.assertTrue(signin.valid_key(KEY))
        for bad in ("", "short", "with space 12345", "quote\"12345678", "line\n12345678", "$(rm -rf ~)", "a" * 513):
            self.assertFalse(signin.valid_key(bad), bad)

    def test_bad_key_is_refused_before_anything_runs(self):
        step = signin.KeyStep("X_API_KEY", stdin=("false",))
        with self.assertRaises(ValueError):
            signin.save_key(step, "two words here")


class EnvFileTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.step = signin.KeyStep("GEMINI_API_KEY", env_file="~/.gemini/.env")
        self.path = self.home / ".gemini" / ".env"

    def test_new_file_is_owner_only(self):
        self.assertEqual(signin.save_key(self.step, KEY, home=self.home), (True, ""))
        self.assertEqual(self.path.read_text(), f"GEMINI_API_KEY={KEY}\n")
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_other_lines_stay_and_the_old_key_goes(self):
        self.path.parent.mkdir()
        self.path.write_text("# mine\nOTHER=1\nexport GEMINI_API_KEY=old\n")
        os.chmod(self.path, 0o644)
        signin.save_key(self.step, KEY, home=self.home)
        self.assertEqual(self.path.read_text(), f"# mine\nOTHER=1\nGEMINI_API_KEY={KEY}\n")
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_a_link_is_replaced_not_followed(self):
        self.path.parent.mkdir()
        target = self.home / "elsewhere"
        target.write_text("untouched\n")
        self.path.symlink_to(target)
        signin.save_key(self.step, KEY, home=self.home)
        self.assertEqual(target.read_text(), "untouched\n")
        self.assertFalse(self.path.is_symlink())
        self.assertIn(KEY, self.path.read_text())

    def test_file_outside_home_is_refused(self):
        with self.assertRaises(ValueError):
            signin.save_key(signin.KeyStep("X_API_KEY", env_file="/etc/x.env"), KEY, home=self.home)


class StdinTest(unittest.TestCase):
    def test_key_goes_on_stdin_and_never_into_the_message(self):
        step = signin.KeyStep("X_API_KEY", stdin=("sh", "-c", 'read k; echo "got $k"; test ${#k} -gt 7'))
        ok, message = signin.save_key(step, KEY)
        self.assertTrue(ok)
        self.assertNotIn(KEY, message)

    def test_failure_is_reported(self):
        ok, _message = signin.save_key(signin.KeyStep("X_API_KEY", stdin=("sh", "-c", "read k; exit 3")), KEY)
        self.assertFalse(ok)


class AdaptersTest(unittest.TestCase):
    def test_sign_ins_are_plain_commands_and_keys_go_where_the_agent_reads_them(self):
        table = agent_status.access_table()
        self.assertEqual(table["claude"].logins[1].argv, ("claude", "auth", "login", "--console"))
        self.assertIsNone(table["claude"].key)          # API billing is Claude's own login, no key here
        self.assertEqual(table["codex"].key.stdin, ("codex", "login", "--with-api-key"))
        self.assertEqual(table["gemini"].key.env_file, "~/.gemini/.env")
        for command, access in table.items():
            for login in access.logins:
                self.assertTrue(login.argv and all(isinstance(w, str) and " " not in w for w in login.argv), command)
            if access.key is not None:
                self.assertTrue(bool(access.key.stdin) != bool(access.key.env_file), command)


if __name__ == "__main__":
    unittest.main()
