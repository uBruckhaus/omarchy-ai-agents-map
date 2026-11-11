"""Tests that move HOME also move identity and desktop data, so config stays in the temporary HOME."""
from pathlib import Path
from unittest import mock

import desktop_omarchy
import identity


def desktop_in(testcase, home: Path):
    home = Path(home)
    for name, value in (("THEME_DIR", home / ".local/state/omarchy/current/theme"),
                        ("USAGE_DIR", home / ".local/state/omarchy/agents/usage")):
        patcher = mock.patch.object(desktop_omarchy, name, value)
        patcher.start()
        testcase.addCleanup(patcher.stop)
    patcher = mock.patch.object(identity, "HOME", home)
    patcher.start()
    testcase.addCleanup(patcher.stop)
    # plugin_ai's shared log of AI runs (every plugin's)
    patcher = mock.patch.dict("os.environ", {"XDG_STATE_HOME": str(home / ".local/state"),
                                          "XDG_CONFIG_HOME": str(home / ".config")})
    patcher.start()
    testcase.addCleanup(patcher.stop)
