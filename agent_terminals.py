"""Where each running instance of an agent can be seen, for the console on its box on the map: in
the terminal here (embedded), in a terminal window of its own, or in the background (started by
another program, e.g. `pi -p` by Claude Code's shell tool: no window shows it).

An instance counts as in a window when it works in a terminal (its input is a terminal device) and,
going up through its parents, a window's process comes before any other agent: a background `pi -p`
started by Claude Code has Claude's terminal window among its parents too, but Claude comes first
(and its input is no terminal). /proc only, this account's own processes; the windows come from the
desktop (desktop.windows(): {process id: title}).

A background instance is watched in a new terminal: session_follow.py follows what it writes.
The stop button left of an agent's start button ends one instance (stop()): its terminal or the
program that started it may stay, the agent itself goes.
"""

from __future__ import annotations

import os
import signal
import time
from dataclasses import dataclass

MAX_DEPTH = 32
TICKS = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100


@dataclass
class Instance:
    pid: int
    kind: str                # "window" or "background"
    window: int = 0          # the window's process (its terminal), for "window"
    title: str = ""          # the window's title
    cwd: str = ""            # the instance's working folder
    started: float = 0.0     # when it was started (seconds since the epoch), 0 = unknown


def _stat_fields(pid: int, proc: str) -> list | None:
    """The fields of /proc/<pid>/stat after "(comm)", of this account's own processes only."""
    try:
        if os.stat(f"{proc}/{pid}").st_uid != os.getuid():
            return None
        with open(f"{proc}/{pid}/stat", "rb") as handle:
            stat = handle.read(4096)
        return stat[stat.rindex(b")") + 2:].split()
    except (OSError, ValueError):
        return None


def parent(pid: int, proc: str = "/proc") -> int:
    fields = _stat_fields(pid, proc)
    try:
        return int(fields[1]) if fields else 0
    except (ValueError, IndexError):
        return 0


def ancestors(pid: int, proc: str = "/proc") -> list[int]:
    """Its parents, nearest first (stops at init or another account's process)."""
    found = []
    for _step in range(MAX_DEPTH):
        pid = parent(pid, proc)
        if pid <= 1:
            break
        found.append(pid)
    return found


def started(pid: int, proc: str = "/proc") -> float:
    fields = _stat_fields(pid, proc)
    try:
        with open(f"{proc}/stat", "rb") as handle:
            boot = next(int(line.split()[1]) for line in handle.read(65536).splitlines() if line.startswith(b"btime "))
        return boot + int(fields[19]) / TICKS if fields else 0.0      # field 22: starttime
    except (OSError, ValueError, IndexError, StopIteration):
        return 0.0


def cwd(pid: int, proc: str = "/proc") -> str:
    try:
        if os.stat(f"{proc}/{pid}").st_uid != os.getuid():
            return ""
        return os.readlink(f"{proc}/{pid}/cwd")
    except OSError:
        return ""


def on_terminal(pid: int, proc: str = "/proc") -> bool:
    """Its input is a terminal device (a TUI in a terminal; not `pi -p </dev/null` from a tool)."""
    try:
        target = os.readlink(f"{proc}/{pid}/fd/0")
    except OSError:
        return False
    return target.startswith("/dev/pts/") or target.startswith("/dev/tty")


def instances(pids, windows: dict, embedded: int | None = None, agent_pids=(), proc: str = "/proc") -> list[Instance]:
    """The instances outside this app's own terminal: pids are the agent's instances, windows the
    desktop's {process id: title}, embedded the process of the terminal here (left out with what
    runs in it), agent_pids every agent instance's process (another agent between an instance and
    a window makes it a background one)."""
    others = set(agent_pids)
    found = []
    for pid in list(pids)[:20]:
        chain = ancestors(pid, proc)
        if embedded and (pid == embedded or embedded in chain):
            continue
        instance = Instance(pid, "background", cwd=cwd(pid, proc), started=started(pid, proc))
        if on_terminal(pid, proc):
            for up in chain:
                if up in windows:
                    instance.kind, instance.window, instance.title = "window", up, str(windows[up] or "")[:200]
                    break
                if up in others:
                    break
        found.append(instance)
    return found


def when(epoch: float, now: float | None = None) -> str:
    """"14:05" today, else "Thu 14:05" ("" unknown)."""
    if not epoch:
        return ""
    moment, today = time.localtime(epoch), time.localtime(now or time.time())
    same = moment.tm_yday == today.tm_yday and moment.tm_year == today.tm_year
    return time.strftime("%H:%M" if same else "%a %H:%M", moment)


STOP_WAIT = 3.0          # seconds an instance has to end after SIGTERM before it gets SIGKILL


def alive(pid: int, since: float = 0.0, proc: str = "/proc") -> bool:
    """This account's process `pid` runs, and (since: when it was started) is still that one, not
    a later process with the same number: the start time to the clock tick (/proc/<pid>/stat field 22)."""
    fields = _stat_fields(pid, proc)
    if not fields or fields[0] == b"Z":          # gone, or ended and only not yet reaped
        return False
    return not since or abs(started(pid, proc) - since) < 0.5 / TICKS


def stop(pid: int, since: float, wait: float = STOP_WAIT, proc: str = "/proc",
         kill=None, sleep=time.sleep, open_pidfd=None) -> bool:
    """End one instance: SIGTERM (it can save and end what it started, e.g. its MCP servers), SIGKILL
    when it is still there after `wait` seconds. Only this account's own process, and only while it
    is still the one that was shown: its start time (since) must be known and the same to the tick.
    The process is held by a pidfd before that check and every signal goes through it, so a number
    the kernel gave to another process meanwhile is never hit (a held pidfd also keeps the number
    from being given out again). Blocking: call it off the main thread. True when it is gone."""
    if pid <= 1 or not since:
        return False                             # unknown start time: who it is cannot be told, leave it
    open_pidfd = open_pidfd or os.pidfd_open
    kill = kill or signal.pidfd_send_signal
    try:
        handle = open_pidfd(pid)
    except OSError:
        return False                             # gone already
    try:
        if not alive(pid, since, proc):          # checked with the process held: still the one shown
            return False
        try:
            kill(handle, signal.SIGTERM)
        except OSError:
            return not alive(pid, since, proc)
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if not alive(pid, since, proc):
                return True
            sleep(0.1)
        if alive(pid, since, proc):
            try:
                kill(handle, signal.SIGKILL)
            except OSError:
                pass
            sleep(0.2)
        return not alive(pid, since, proc)
    finally:
        if isinstance(handle, int) and open_pidfd is os.pidfd_open:
            os.close(handle)
