"""Look up on the web: a look-up that fails with an exception (a profile that cannot be saved) still
comes back to the window, so the agent is not left marked as being looked up. Headless; no network."""

import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import gi  # noqa: E402
gi.require_version("Gtk", "4.0")
from gi.repository import GLib  # noqa: E402

import agent_profiles  # noqa: E402
import ai_popup  # noqa: E402


class Window:
    """The parts of AgentsMap that start_research uses."""

    def __init__(self):
        self.researching = set()
        self.agent_list = [SimpleNamespace(command="newagent", installed=True)]
        self.notes = []

    def refresh_overview(self):
        pass

    def collect(self):
        pass

    def refresh_map(self):
        pass

    def show_note(self, text):
        self.notes.append(text)


class ResearchFailureTest(unittest.TestCase):
    def test_a_failing_look_up_is_reported_and_not_left_running(self):
        window = Window()
        agent = SimpleNamespace(command="newagent", name="New Agent")

        def broken(*_args, **_kwargs):
            raise PermissionError("profile folder not writable")
        with patch.object(agent_profiles, "research", broken):
            ai_popup.AgentsMap.start_research(window, agent)
            self.assertIn("newagent", window.researching)
            context = GLib.MainContext.default()
            deadline = time.monotonic() + 5
            while not window.notes and time.monotonic() < deadline:
                context.iteration(False)
                time.sleep(0.01)
        self.assertEqual(window.researching, set())
        self.assertEqual(len(window.notes), 1)
        self.assertIn("profile folder not writable", window.notes[0])


if __name__ == "__main__":
    unittest.main()
