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


class AddonRulesTest(unittest.TestCase):
    """Round 4: every add-on change asks Settings › What may be done, also on the Add-ons page."""

    def test_every_add_on_step_is_a_rule_check(self):
        import action_rules as r
        self.assertEqual(r.addon_check("on", "agent-goose"), ("install", "agents", "agent-goose"))
        self.assertEqual(r.addon_check("remove", "llm-ollama"), ("remove", "providers", "llm-ollama"))
        self.assertEqual(r.addon_check("update", "x", "agent"), ("update", "agents", "x"))
        self.assertIsNone(r.addon_check("details", "agent-goose"))
        deny = [{"effect": "allow", "action": "*", "category": "*", "item": "*", "agent": "*"},
                {"effect": "deny", "action": "update", "category": "providers", "item": "*", "agent": "*"}]
        self.assertFalse(r.allows(deny, *r.addon_check("update", "llm-ollama")))
        self.assertTrue(r.allows(deny, *r.addon_check("update", "agent-goose")))

    def test_the_add_ons_page_asks_before_it_changes(self):
        import addons_page

        class Page(addons_page.AddonsPage):
            pass
        page = Page()
        asked = []
        page.rule_allows = lambda *check: asked.append(check) or False
        page.addon_locked = lambda ids: False
        page.addons_busy = lambda: False
        item = addons_page.Item("llm-ollama", "Ollama", "provider", "on", "", "", "")
        item.entry = object()
        with mock.patch.object(addon_store, "set_enabled", side_effect=AssertionError("changed")), \
                mock.patch.object(addon_store, "install", side_effect=AssertionError("installed")), \
                mock.patch.object(addon_store, "remove", side_effect=AssertionError("removed")):
            page.switch_addon_on(item)
            page.switch_addon_off(item)
            page.update_addon(item)
            page.install_addon(item)
            page.remove_addon(item)
        self.assertEqual([a[0] for a in asked], ["install", "remove", "update", "install", "remove"])


class FoundAddonsTest(unittest.TestCase):
    def test_a_denied_add_on_is_not_switched_on_at_the_start(self):
        import addons_page

        class Page(addons_page.AddonsPage):
            pass
        page = Page()
        agent = type("A", (), {"id": "agent-goose", "kind": "agent"})()
        llm = type("A", (), {"id": "llm-ollama", "kind": "provider"})()
        page.adapter_addons = lambda: [agent, llm]
        page.installed_adapters = lambda adapters: {"agent-goose", "llm-ollama"}
        page.load_addons = page.reload_addons = lambda: None
        rules = [{"effect": "allow", "action": "*", "category": "*", "item": "*", "agent": "*"},
                 {"effect": "deny", "action": "install", "category": "agents", "item": "agent-goose", "agent": "*"}]
        got = []
        with mock.patch.object(addons_page.ai_core, "setting", lambda key, *a: rules if key == "action_rules" else None), \
                mock.patch.object(addon_store, "enable_found", lambda ids: got.extend(ids) or []):
            page.enable_found_addons()
        self.assertEqual(got, ["llm-ollama"])


class Round6Test(unittest.TestCase):
    def test_a_fifo_skill_file_never_hangs(self):
        with tempfile.TemporaryDirectory() as folder:
            os.mkfifo(Path(folder) / "SKILL.md")
            self.assertEqual(finishes(self, lambda: skill_update.read_skill(Path(folder))), "")

    def test_the_gate_uses_the_verified_kind(self):
        import addons_page
        rules = [{"effect": "allow", "action": "*", "category": "*", "item": "*", "agent": "*"},
                 {"effect": "deny", "action": "install", "category": "agents", "item": "*", "agent": "*"}]
        with mock.patch.object(addons_page.ai_core, "setting", lambda key, *a: rules if key == "action_rules" else None):
            gate = addons_page.AddonsPage.addon_rule_gate("install")
            # The catalog said provider, the signed zip is an agent: the agent's rule decides.
            self.assertTrue(gate("llm-x", "agent", False))
            self.assertIsNone(gate("llm-x", "provider", False))
        # Rules changed while the download ran: the gate reads them when it is asked.
        later = rules + [{"effect": "deny", "action": "install", "category": "providers", "item": "*", "agent": "*"}]
        with mock.patch.object(addons_page.ai_core, "setting", lambda key, *a: later if key == "action_rules" else None):
            self.assertTrue(gate("llm-x", "provider", False))

    def test_a_failed_replace_puts_the_old_add_on_back(self):
        import addons
        with tempfile.TemporaryDirectory() as root:
            user = Path(root) / "addons"
            (user / "demo").mkdir(parents=True)
            (user / "demo" / "keep").write_text("old")
            verified = type("A", (), {"state": "verified", "kind": "agent", "reason": "", "folder": None})()
            real_replace = os.replace

            def replace(src, dst):
                if ".install-" in str(src):
                    raise OSError("disk full")
                return real_replace(src, dst)
            with mock.patch.object(addon_store, "unpack", side_effect=lambda data, staging: (Path(staging) / "demo").mkdir() or "demo"), \
                    mock.patch.object(addons, "verify", return_value=verified), \
                    mock.patch.object(addon_store.os, "replace", replace):
                with self.assertRaises(OSError):
                    addon_store.install_bytes(b"zip", user_dir=user)
            self.assertEqual((user / "demo" / "keep").read_text(), "old")


class TrustedChainTest(unittest.TestCase):
    def test_a_folder_higher_up_that_others_can_write(self):
        import llama_tune
        with tempfile.TemporaryDirectory() as top:
            os.chmod(top, 0o700)
            inner = Path(top) / "a" / "bin"
            inner.mkdir(parents=True, mode=0o755)
            bench = inner / "llama-bench"
            bench.write_text("#!/bin/sh\n")
            os.chmod(bench, 0o755)
            os.chmod(Path(top) / "a", 0o755)
            self.assertTrue(llama_tune.trusted(str(bench)))
            os.chmod(Path(top) / "a", 0o777)                 # two levels up: open to others
            self.assertFalse(llama_tune.trusted(str(bench)))
            os.chmod(Path(top) / "a", 0o755)


class DictationHistoryTest(unittest.TestCase):
    def test_malformed_history_gives_no_text(self):
        import json as _json
        import dictation
        for data in ({"items": ["x", 3, None]}, {"items": "x"}, {"items": [{"timestamp": 10 ** 400}]}, [1]):
            with mock.patch.object(dictation, "_run", return_value=(True, _json.dumps(data) if not isinstance(
                    data, dict) or "timestamp" not in str(data) else '{"items": [{"timestamp": 1' + "0" * 400 + '}]}')):
                self.assertEqual(dictation.newest_text(0), "")


if __name__ == "__main__":
    unittest.main()
