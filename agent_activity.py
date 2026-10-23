"""Which running agents are working right now (using tokens), not just open at their prompt:
the map's flowing lines follow this.

An agent counts as working when any of these says so (all read-only, cheap, every 2 s):
- herdr shows its pane as "working";
- its process uses the CPU (a coding agent streams and redraws while it works and is all but
  idle at its prompt): the CPU time of its processes and their finished children, from
  /proc/<pid>/stat of this user's own processes;
- its session files were written in the last few seconds (each answer and tool call is
  appended there; its adapter's activity fact lists where);
- the local LLM it uses now is generating (the graphics card is busy with a loaded model);
- the session file it has open is in the middle of a turn: begun, not yet ended (Codex writes
  task_started … task_complete), also while the model thinks or a command runs for a long time
  and nothing else moves.
"""

import os
import re
import time

import agent_details

CPU_BUSY = 0.02                  # share of one core over the last interval that counts as working
RECENT = 8                       # seconds: session files written this recently
TICKS = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
TURN_TAIL = 64 * 1024            # bytes read from the end of an open session file
TURN_MARK = re.compile(rb'"type"\s*:\s*"(task_started|task_complete|turn_aborted)"')
TURN_STALE = 30 * 60             # a turn begun but not written to for this long: ended without a mark


def in_turn(pid: int, proc: str = "/proc", wall: float | None = None) -> bool:
    """One of this user's processes has a session file (.jsonl) open whose last turn mark is a
    beginning: it works on that turn now."""
    wall = time.time() if wall is None else wall
    try:
        if os.stat(f"{proc}/{pid}").st_uid != os.getuid():
            return False
        fds = os.listdir(f"{proc}/{pid}/fd")[:512]
    except OSError:
        return False
    for fd in fds:
        try:
            target = os.readlink(f"{proc}/{pid}/fd/{fd}")
        except OSError:
            continue
        if not target.endswith(".jsonl"):
            continue
        try:
            info = os.stat(target)
            if info.st_uid != os.getuid() or wall - info.st_mtime > TURN_STALE:
                continue
            with open(target, "rb") as handle:
                handle.seek(max(0, info.st_size - TURN_TAIL))
                marks = TURN_MARK.findall(handle.read(TURN_TAIL))
        except OSError:
            continue
        if marks and marks[-1] == b"task_started":
            return True
    return False


def cpu_ticks(pid: int, proc: str = "/proc") -> int | None:
    """utime + stime + cutime + cstime of one of this user's processes, None if gone or not ours."""
    try:
        if os.stat(f"{proc}/{pid}").st_uid != os.getuid():
            return None
        with open(f"{proc}/{pid}/stat", "rb") as handle:
            stat = handle.read(4096)
        fields = stat[stat.rindex(b")") + 2:].split()
        return sum(int(x) for x in fields[11:15])      # fields 14-17 of stat
    except (OSError, ValueError, IndexError):
        return None


class Activity:
    """Remembers the last CPU reading per process, to tell how busy each agent was since."""

    def __init__(self):
        self.last = {}               # pid -> (ticks, monotonic seconds)

    def cpu(self, pids_by_agent: dict, proc: str = "/proc", now: float | None = None) -> dict:
        """{agent: share of one core its processes used since the last call} (0 the first time)."""
        now = time.monotonic() if now is None else now
        shares, seen = {}, {}
        for agent, pids in pids_by_agent.items():
            used = 0.0
            for pid in pids[:32]:
                ticks = cpu_ticks(pid, proc)
                if ticks is None:
                    continue
                seen[pid] = (ticks, now)
                before = self.last.get(pid)
                if before is not None and now > before[1]:
                    used += max(0, ticks - before[0]) / TICKS / (now - before[1])
            shares[agent] = used
        self.last = seen
        return shares

    def working(self, pids_by_agent: dict, herdr_states: dict | None = None,
                generating_for: set | frozenset = frozenset(),
                proc: str = "/proc", now: float | None = None, wall: float | None = None) -> set:
        """The agents (of pids_by_agent, the running ones) that work right now.
        herdr_states: {agent: set of herdr states of its panes}; generating_for: agents whose
        local LLM is generating now."""
        shares = self.cpu(pids_by_agent, proc, now)
        wall = time.time() if wall is None else wall
        busy = set()
        for agent in pids_by_agent:
            states = (herdr_states or {}).get(agent, set())
            if "working" in states or agent in generating_for or shares.get(agent, 0) >= CPU_BUSY:
                busy.add(agent)
                continue
            newest = agent_details.newest_mtime(list(agent_details.activity(agent)))
            if newest is not None and wall - newest.timestamp() <= RECENT:
                busy.add(agent)
            elif any(in_turn(pid, proc, wall) for pid in pids_by_agent[agent][:8]):
                busy.add(agent)
        return busy
