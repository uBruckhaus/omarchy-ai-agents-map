"""Tests that move HOME also move identity and desktop data, so config stays in the temporary HOME."""
from pathlib import Path
from unittest import mock
import os

import desktop_omarchy
import identity


def sandbox_ancestors_in(testcase):
    """Model immutable sandbox / and sticky /tmp as root, including pinned fds."""
    original_lstat, original_fstat, original_stat = os.lstat, os.fstat, os.stat
    ancestors = {(s.st_dev, s.st_ino) for s in (original_lstat('/'), original_lstat('/tmp')) if s.st_uid == 65534}
    def trusted(info):
        if (info.st_dev, info.st_ino) in ancestors:
            parts = list(info)
            parts[4] = 0
            return os.stat_result(parts)
        return info
    testcase.enterContext(mock.patch('os.lstat', side_effect=lambda *a, **k: trusted(original_lstat(*a, **k))))
    testcase.enterContext(mock.patch('os.fstat', side_effect=lambda *a, **k: trusted(original_fstat(*a, **k))))
    testcase.enterContext(mock.patch('os.stat', side_effect=lambda *a, **k: trusted(original_stat(*a, **k))))


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
