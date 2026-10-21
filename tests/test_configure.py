"""Configure… on the map (configure_ctl): an agent's or local LLM's own setup from its add-on's data,
its settings files, the editor, and what an add-on may not ask to run."""

import shutil
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import adapter_data  # noqa: E402
import configure_ctl  # noqa: E402
import harnesses  # noqa: E402
import providers  # noqa: E402
from harnesses import Configure  # noqa: E402
from providers import Provider  # noqa: E402


class ConfigureTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(shutil, "which", lambda name: f"/usr/bin/{name}")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_agent_gets_its_own_setup_commands(self):
        entries = configure_ctl.for_agent("hermes")
        self.assertEqual([e.argv for e in entries][:2], [("hermes", "setup"), ("hermes", "model")])
        self.assertFalse(any("--help" in e.argv for e in entries))

    def test_without_a_setup_command_its_help(self):
        self.assertEqual([e.argv for e in configure_ctl.for_agent("pi")], [("pi", "--help")])
        self.assertEqual([e.argv for e in configure_ctl.for_runtime("llamacpp")], [("llama-server", "--help")])

    def test_a_missing_program_is_not_offered(self):
        with mock.patch.object(shutil, "which", lambda name: None):
            self.assertEqual(configure_ctl.for_agent("hermes"), [])
            self.assertEqual(configure_ctl.for_runtime("llamacpp"), [])

    def test_local_llm_menu_and_window(self):
        self.assertEqual([e.argv for e in configure_ctl.for_runtime("ollama")], [("ollama", "launch")])
        self.assertTrue(configure_ctl.for_runtime("lmstudio")[0].window)

    def test_an_add_on_made_before_these_facts_gets_the_apps_own(self):
        old = SimpleNamespace(key="ollama", configure=())
        with mock.patch.object(providers, "get", return_value=old):
            self.assertEqual(configure_ctl.for_runtime("ollama")[0].argv, ("ollama", "launch"))

    def test_script_quotes_every_word(self):
        entry = configure_ctl.Entry("x", ("hermes", "my arg;rm -rf ~"), note="it's here")
        self.assertEqual(configure_ctl.script(entry, "Done."),
                         "echo 'it'\"'\"'s here'; echo; hermes 'my arg;rm -rf ~'; echo; echo Done.")


class WhatMayRunTest(unittest.TestCase):
    def test_commands_are_the_own_program_and_plain_words(self):
        ok = harnesses.configure_ok
        self.assertTrue(ok(("hermes", "setup"), ("hermes",)))
        for bad in (("bash", "-c", "x"), ("sudo", "hermes"), ("/tmp/hermes",), (), ("hermes", "a\nb")):
            self.assertFalse(ok(bad, (bad[0],) if bad else ()), bad)
        self.assertFalse(ok(("other", "setup"), ("hermes",)))

    def test_agent_toml_refuses_another_program(self):
        data = {"command": "x", "name": "X", "configure": [{"label": "Setup", "argv": ["sh", "-c", "x setup"]}]}
        with self.assertRaises(adapter_data.AdapterError):
            adapter_data.check(data, "x.toml")

    def test_provider_add_on_refuses_a_shell(self):
        self.addCleanup(providers.remove_added, "test")
        with self.assertRaises(ValueError):
            providers.add(Provider("newllm", "NewLLM", "local", connect_key="newllm-local",
                                   configure=(Configure("Setup", ("bash", "-c", "x")),)), "test")
        providers.add(Provider("newllm", "NewLLM", "local", connect_key="newllm-local",
                               configure=(Configure("Menu", ("newllm", "menu")),)), "test")


if __name__ == "__main__":
    unittest.main()
