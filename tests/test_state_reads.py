"""The app's own state and cache files: a FIFO, a link or a huge file put in their place never hangs or
floods a reader, and the add-on icon cache never writes through a planted link (Codex round 2)."""

import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import addon_store  # noqa: E402
import icons  # noqa: E402
import jobs  # noqa: E402
import skill_update  # noqa: E402


def finishes(test, call, seconds=3.0):
    """call() returns within `seconds` (a blocking open of a FIFO would not)."""
    result = {}
    thread = threading.Thread(target=lambda: result.setdefault("value", call()), daemon=True)
    thread.start()
    thread.join(seconds)
    test.assertFalse(thread.is_alive(), "the reader hung")
    return result.get("value")


class StateReadsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        os.chmod(self.tmp, 0o700)

    def fifo(self, name):
        path = self.tmp / name
        os.mkfifo(path)
        return path

    def test_a_fifo_never_hangs_a_reader(self):
        self.assertIsNone(finishes(self, lambda: addon_store._load_cache(self.fifo("catalog.json"))))
        self.assertEqual(finishes(self, lambda: skill_update.load_state(self.fifo("skills.json"))), {})
        queue = jobs.Jobs.__new__(jobs.Jobs)
        queue.state_file = self.fifo("jobs.json")
        self.assertEqual(finishes(self, queue.load), [])

    def test_a_huge_state_file_is_not_read(self):
        big = self.tmp / "skills.json"
        big.write_bytes(b"{" + b" " * (2 * 1024 * 1024) + b"}")
        self.assertEqual(skill_update.load_state(big), {})

    def test_the_icon_cache_never_writes_through_a_link(self):
        victim = self.tmp / "victim.txt"
        victim.write_text("keep me")
        cache = self.tmp / "icons"
        cache.mkdir(mode=0o700)
        os.symlink(victim, cache / "am-addon-demo-symbolic.svg")
        svg = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><path d="M0 0h16v16H0z"/></svg>'
        with mock.patch.object(icons, "ADDON_DIR", cache):
            name = icons.add_addon_icon("demo", svg)
        self.assertEqual(victim.read_text(), "keep me")
        if name:
            target = cache / f"{name}.svg"
            self.assertFalse(target.is_symlink())
            self.assertEqual(target.read_bytes(), svg)


if __name__ == "__main__":
    unittest.main()
