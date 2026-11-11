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


class AddonReadsTest(unittest.TestCase):
    """Round 3: add-on files and a chosen zip are opened without blocking; a crafted folder is
    refused once it has more files than allowed, before the rest is listed."""

    def setUp(self):
        import addons
        self.addons = addons
        self.tmp = Path(tempfile.mkdtemp())
        os.chmod(self.tmp, 0o700)

    def test_a_fifo_in_an_add_on_or_as_the_chosen_zip(self):
        fifo = self.tmp / "addon.toml"
        os.mkfifo(fifo)
        self.assertIsNone(finishes(self, lambda: self.addons._read(fifo, os.getuid(), 1024)))

        def chosen():
            try:
                addon_store.install_file(fifo)
            except addon_store.InstallError as error:
                return str(error)
        self.assertTrue(finishes(self, chosen))

    def test_too_many_files_stops_early(self):
        folder = self.tmp / "crafted"
        folder.mkdir(mode=0o700)
        for i in range(self.addons.MAX_FILES + 50):
            (folder / f"f{i}").write_text("x")
        listed = []
        real = os.scandir

        class Counting:
            def __init__(self, path):
                self.it = real(path)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                self.it.close()

            def __iter__(self):
                for entry in self.it:
                    listed.append(entry)
                    yield entry
        with mock.patch("os.scandir", Counting):
            self.assertEqual(self.addons._files(folder, os.getuid()), "too many files")
        self.assertLessEqual(len(listed), self.addons.MAX_FILES + 1)


class ProposalRulesTest(unittest.TestCase):
    """Round 3: a kept Ask AI proposal asks the rules again when it is used; an add-on proposal opens
    the Add-ons page (its own switch and confirmations) instead of switching it directly."""

    def page(self, allowed):
        import askai_page

        class Page(askai_page.AskAiPage):
            pass
        page = Page()
        page.calls = []
        page.ask_proposals = type("P", (), {"get": lambda _s, _p, _c: self.candidate})()
        page.rule_allows = lambda *check: page.calls.append(("rule",) + check) or allowed
        page.close_ask_ai = lambda: page.calls.append(("close",))
        page.open_addons = lambda key: page.calls.append(("addons", key))
        return page

    def test_a_changed_rule_refuses_a_kept_proposal(self):
        self.candidate = {"id": "c1", "action": {"kind": "menu_action", "area": "addons", "step": "on",
                                                 "key": "agent-goose"}}
        with mock.patch.object(addon_store, "set_enabled", side_effect=AssertionError("switched directly")):
            page = self.page(allowed=False)
            page.apply_candidate("p1", "c1")
            self.assertEqual(page.calls, [("rule", "install", "agents", "agent-goose")])
            page = self.page(allowed=True)
            page.apply_candidate("p1", "c1")
            self.assertEqual(page.calls[-1], ("addons", "agent-goose"))


if __name__ == "__main__":
    unittest.main()
