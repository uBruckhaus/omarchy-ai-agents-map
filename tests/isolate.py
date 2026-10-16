"""Tests that move HOME to a temp folder move the desktop's folders with it, so
nothing a test writes can reach the real ~/.config/omarchy/ai-agents-map."""
from pathlib import Path
from unittest import mock

import desktop_generic
import desktop_omarchy


def desktop_in(testcase, home: Path):
    home = Path(home)
    for name, value in (("CONFIG_DIR", home / ".config/omarchy/ai-agents-map"),
                        ("THEME_DIR", home / ".local/state/omarchy/current/theme"),
                        ("USAGE_DIR", home / ".local/state/omarchy/agents/usage")):
        patcher = mock.patch.object(desktop_omarchy, name, value)
        patcher.start()
        testcase.addCleanup(patcher.stop)
    patcher = mock.patch.object(desktop_generic, "CONFIG_DIR", home / ".config/asterism")
    patcher.start()
    testcase.addCleanup(patcher.stop)
