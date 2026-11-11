"""Ending a program together with everything it started (its process group).

Programs that AI Agents Map runs for a finite job (a look-up, an MCP query, an unpack…) are
started with `start_new_session=True`: the program leads a process group of its own, whose id
is its pid. Signalling that group is only safe while the id cannot belong to somebody else:

- end_child(): the program is our own child (subprocess.Popen). Its exit is waited for with
  WNOWAIT, so it stays a zombie, and its pid (= the group id) stays taken, until the group has
  had SIGTERM, a short grace and SIGKILL. Only then is it reaped. This also covers a program
  that exits at once while a child it started ignores SIGTERM.
- Group: a group whose leader somebody else reaps (VTE reaps the agent in the embedded
  terminal). Each member found is held by a pidfd and signalled through it, so a reused pid is
  never signalled; new members are only looked up while a held member still exists (then the
  group id is still taken and every process in the group is one of ours).
"""

from __future__ import annotations

import os
import signal
import subprocess
import time

GRACE = 2.0                       # seconds between SIGTERM and SIGKILL


def end_child(proc: subprocess.Popen, grace: float = GRACE, wait: float = 0.0) -> int | None:
    """End proc's group (proc started with start_new_session=True) and reap proc; its exit code.
    `wait`: seconds the program may still take to end on its own before its group gets SIGTERM
    (a program that has closed its output and is finishing). Safe to call more than once."""
    if proc.returncode is not None:           # already reaped: its pid is no longer ours to signal
        return proc.returncode
    exited = _exited(proc.pid, wait)
    _signal_group(proc.pid, signal.SIGTERM)
    if not exited:
        exited = _exited(proc.pid, grace)
    elif _others_in(proc.pid):
        time.sleep(min(grace, 0.2))           # a moment for the rest to end on SIGTERM
    _signal_group(proc.pid, signal.SIGKILL)   # whatever is still there: the leader is not reaped yet
    try:
        return proc.wait(timeout=5)
    except subprocess.TimeoutExpired:         # cannot happen after SIGKILL short of a stuck kernel
        return None


def _exited(pid: int, seconds: float) -> bool:
    """Whether the child pid has ended within `seconds`, without reaping it (WNOWAIT)."""
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        try:
            if os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None:
                return True
        except ChildProcessError:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)


def _signal_group(pgid: int, sig) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _stat(pid: int) -> tuple[str, int] | None:
    """(state, process group) from /proc/<pid>/stat, or None."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            raw = handle.read(4096)
    except OSError:
        return None
    fields = raw[raw.rfind(b")") + 2:].split()
    try:
        return fields[0].decode(), int(fields[2])
    except (IndexError, ValueError):
        return None


def members(pgid: int) -> list[int]:
    """The pids whose process group is pgid (zombies too)."""
    found = []
    try:
        names = os.listdir("/proc")
    except OSError:
        return found
    for name in names:
        if name.isdigit():
            stat = _stat(int(name))
            if stat is not None and stat[1] == pgid:
                found.append(int(name))
    return found


def _others_in(pgid: int) -> bool:
    return any(pid != pgid and (_stat(pid) or ("Z", 0))[0] != "Z" for pid in members(pgid))


class Group:
    """A process group whose leader is reaped elsewhere (the embedded terminal's agent).
    Take it while the leader is known to run: Group(pid, leader_fd)."""

    def __init__(self, pgid: int, leader_fd: int | None = None):
        self.pgid = pgid
        self.fds: dict[int, int] = {}
        if leader_fd is not None:
            self.fds[pgid] = leader_fd
        self.refresh()

    def _alive_fds(self) -> list[int]:
        """The held processes that still exist AND are still in the group: only such an anchor
        keeps the group id taken (a member that moved to another group does not)."""
        return [fd for pid, fd in self.fds.items() if self._in_group(fd, pid)]

    def refresh(self) -> None:
        """Hold the group's current members. Only while a held member exists (before and after
        the look-up), so the group id was taken all the time and nobody else's pid is taken."""
        if not self._alive_fds():
            return
        new = {}
        for pid in members(self.pgid):
            if pid in self.fds:
                continue
            try:
                fd = os.pidfd_open(pid)
            except OSError:
                continue
            # The pid may have ended and been reused between the listing and pidfd_open: keep the
            # pidfd only when the process it holds is (still) in the group. Its /proc entry is read
            # while the pidfd shows it alive before and after, so the entry is that process's.
            if self._in_group(fd, pid):
                new[pid] = fd
            else:
                os.close(fd)
        if self._alive_fds():
            self.fds.update(new)
        else:
            for fd in new.values():
                os.close(fd)

    def _in_group(self, fd: int, pid: int) -> bool:
        def alive():
            try:
                signal.pidfd_send_signal(fd, 0)
                return True
            except OSError:
                return False
        if not alive():
            return False
        stat = _stat(pid)
        return stat is not None and stat[1] == self.pgid and alive()

    def signal(self, sig) -> None:
        """Every held member that is still in the group (one that left it, by setsid, is spared)."""
        self.refresh()
        for pid, fd in self.fds.items():
            if not self._in_group(fd, pid):
                continue
            try:
                signal.pidfd_send_signal(fd, sig)
            except OSError:
                pass

    def alive(self) -> bool:
        """Whether a held member still runs (a zombie does not count)."""
        for pid, fd in self.fds.items():
            try:
                signal.pidfd_send_signal(fd, 0)
            except OSError:
                continue
            stat = _stat(pid)
            if stat is not None and stat[0] != "Z":
                return True
        return False

    def close(self) -> None:
        for fd in self.fds.values():
            try:
                os.close(fd)
            except OSError:
                pass
        self.fds.clear()
