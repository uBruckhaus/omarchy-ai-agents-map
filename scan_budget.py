"""A budget for looking through folders (model scans, the inventory's listings).

Entries are read one at a time (os.scandir) and each one is charged before it is kept, so a
folder with a huge number of names is never listed or sorted as a whole first. One Budget can be
shared by a whole scan (several roots, nested folders): it stops at `visits` entries, after
`seconds`, or when `cancel` (a threading.Event) is set; `stopped` then says that the result is
partial, so the page can say so.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

VISITS = 50_000           # entries looked at in one scan
SECONDS = 20.0            # one scan in all


class Budget:
    def __init__(self, visits: int = VISITS, seconds: float = SECONDS, cancel=None):
        self.left = visits
        self.deadline = time.monotonic() + seconds
        self.cancel = cancel
        self.stopped = False

    def take(self) -> bool:
        """Charge one entry; False (and stopped) once the budget is used up."""
        if self.stopped:
            return False
        if self.left <= 0 or time.monotonic() > self.deadline or (self.cancel is not None and self.cancel.is_set()):
            self.stopped = True
            return False
        self.left -= 1
        return True

    def entries(self, folder: Path, limit: int) -> list[Path]:
        """At most `limit` entries of folder, read one by one and charged, then sorted (by name).
        Raises OSError as Path.iterdir() would when the folder cannot be read."""
        found = []
        with os.scandir(folder) as listing:
            for entry in listing:
                if len(found) >= limit or not self.take():
                    break
                found.append(Path(entry.path))
        return sorted(found)


def entries(folder: Path, limit: int) -> list[Path]:
    """Budget.entries with a budget of its own (one listing)."""
    return Budget().entries(folder, limit)
