"""Ending a program together with everything it started (its process group).

Programs that AI Agents Map runs for a finite job (a look-up, an MCP query, an unpack…) are
started with `start_new_session=True`: the program leads a process group of its own, whose id
is its pid. Signalling that group is only safe while the id cannot belong to somebody else:

- end_child(): the program is our own child (subprocess.Popen). Its exit is waited for with
  WNOWAIT, so it stays a zombie, and its pid (= the group id) stays taken, until the group has
  had SIGTERM, a short grace and SIGKILL. Only then is it reaped. This also covers a program
  that exits at once while a child it started ignores SIGTERM.
- Group: a group whose leader somebody else reaps (VTE reaps the agent in the embedded
  terminal). The leader's pidfd names the group itself: pidfd_send_signal with
  PIDFD_SIGNAL_PROCESS_GROUP (Linux 6.9+) signals every process whose group is the one that
  leader led, also after the leader ended and was reaped, and never a group that got the same
  number later (the kernel resolves the pidfd's own struct pid, not a number). So a member the
  agent starts at any moment, also one its SIGHUP handler starts just before it exits, still
  gets SIGTERM and SIGKILL. Older kernels: each member found is held by a pidfd and signalled
  through it, so a reused pid is never signalled; new members are only looked up while a held
  member still exists (then the group id is still taken and every process in it is ours).
"""

from __future__ import annotations

import os
import signal
import subprocess
import time

GRACE = 2.0                       # seconds between SIGTERM and SIGKILL
PIDFD_SIGNAL_PROCESS_GROUP = 1 << 2   # linux/pidfd.h (6.9+): the pidfd's process group


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
        self.leader_fd = leader_fd        # names the group itself (see the module docstring)
        if leader_fd is not None:
            self.fds[pgid] = leader_fd
        self.refresh()

    def _whole_group(self, sig) -> bool | None:
        """sig to the leader's whole group through its pidfd. True: some member got it; False:
        the group has no member left; None: not possible here (old kernel, no leader pidfd)."""
        fd = self.leader_fd
        if fd is None or fd not in self.fds.values():
            return None
        try:
            signal.pidfd_send_signal(fd, sig, None, PIDFD_SIGNAL_PROCESS_GROUP)
            return True
        except ProcessLookupError:
            return False
        except OSError:                   # EINVAL: the kernel has no PIDFD_SIGNAL_PROCESS_GROUP
            return None

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
        """Every process still in the group, each once: through the leader's pidfd when the
        kernel can (also members never seen before); otherwise every held member that is still
        in the group (one that left it, by setsid, is spared)."""
        self.refresh()
        if self._whole_group(sig) is not None:
            return                        # the kernel reached the whole group: no second copy
        for pid, fd in self.fds.items():
            if not self._in_group(fd, pid):
                continue
            try:
                signal.pidfd_send_signal(fd, sig)
            except OSError:
                pass

    def alive(self) -> bool:
        """Whether a held member still runs (a zombie does not count), or, when the kernel can
        tell, whether any process is still in the leader's group."""
        whole = self._whole_group(0)
        if whole is not None and whole and self._group_runs():
            return True
        for pid, fd in self.fds.items():
            try:
                signal.pidfd_send_signal(fd, 0)
            except OSError:
                continue
            stat = _stat(pid)
            if stat is not None and stat[0] != "Z":
                return True
        return False

    def _group_runs(self) -> bool:
        """A non-zombie process in the group. Only asked right after the leader's pidfd showed
        the group still has a member, so the number is still that group's."""
        return any((_stat(pid) or ("Z", 0))[0] != "Z" for pid in members(self.pgid))

    def close(self) -> None:
        self.leader_fd = None
        for fd in self.fds.values():
            try:
                os.close(fd)
            except OSError:
                pass
        self.fds.clear()
