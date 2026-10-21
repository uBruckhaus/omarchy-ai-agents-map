"""Which MCP servers a working agent uses right now: the map's agent → MCP server lines flow for them.

All read-only, from this user's own processes, every 2 s (with agent_activity):
- a server the agent runs (stdio): the agent's process that runs its configured command, found
  among the agent's own processes by its arguments; in use while it reads or writes on the
  pipe the agent talks to it over (/proc/<pid>/io of the processes on that pipe), or its
  processes use the CPU (a call does work; an idle server waits without a tick);
- a server at a URL: the agent's connection to that address moved data since the last check
  (`ss`, as the traffic page reads it).
Only servers of agents that work right now are looked at.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import time
from urllib.parse import urlsplit

import agent_activity
import traffic

CPU_BUSY = 0.02                  # share of one core over the last interval that counts as in use
MAX_DEPTH = 3                    # how far below the agent a server's process may be (a shell, npm exec)
MAX_PROCS = 512                  # processes of one server looked at
RESOLVE_EVERY = 600              # seconds a URL host's addresses are kept
LAUNCHERS = {"npx", "bunx", "pnpx", "uvx"}   # rewrite their own command line (npm exec …, uv tool uvx …)


def wanted_tokens(entry: dict) -> list:
    """The arguments of a configured command that its process shows as they are (no options and no
    placeholders the agent fills in)."""
    return [a for a in entry.get("args", ())
            if a and not a.startswith("-") and not any(c in a for c in "$~{}")][:16]


def matches(entry: dict, argv: list) -> bool:
    """Is this process the configured server command?"""
    if len(argv) == 1 and " " in argv[0]:
        argv = argv[0].split()             # a process that set its own title (npm exec …)
    if not argv:
        return False
    command = os.path.basename(entry.get("command", ""))
    names = {os.path.basename(a) for a in argv[:2]}
    tokens = wanted_tokens(entry)
    if tokens:
        if not all(t in argv for t in tokens):
            return False
        return command in names or command in LAUNCHERS or command == "npm"
    return command in names


def server_processes(agent_pids, entries: dict, processes, parent_of) -> dict:
    """{server name: [[pids of one copy], …]} of one agent: per stdio entry each of the agent's
    processes (at most MAX_DEPTH below an instance) that runs it, with everything it started; an
    agent with several instances runs a copy for each.
    processes: (pid, comm, argv); parent_of(pid) -> parent pid."""
    children = {}
    argv_of = {}
    for pid, _comm, argv in processes:
        argv_of[pid] = argv
        children.setdefault(parent_of(pid), []).append(pid)
    stdio = {name: entry for name, entry in entries.items() if entry.get("transport") == "stdio"}
    roots = {}
    level = list(agent_pids)
    for _depth in range(MAX_DEPTH):
        below = []
        for pid in (k for parent in level for k in children.get(parent, ())):
            name = next((n for n, entry in stdio.items() if matches(entry, argv_of.get(pid, []))), None)
            if name is None:
                below.append(pid)          # a shell or launcher: its children may be the server
            else:
                roots.setdefault(name, []).append(pid)
        level = below
    result = {}
    for name, found in roots.items():
        for root in found[:8]:
            pids, todo = [], [root]
            while todo and len(pids) < MAX_PROCS:
                pid = todo.pop()
                pids.append(pid)
                todo.extend(children.get(pid, ()))
            result.setdefault(name, []).append(pids)
    return result


def stdin_pipe(pid: int, proc: str = "/proc") -> str:
    try:
        return os.readlink(f"{proc}/{pid}/fd/0")
    except OSError:
        return ""


def io_chars(pid: int, proc: str = "/proc") -> int | None:
    """rchar + wchar of one of this user's processes, None if gone or not ours."""
    try:
        if os.stat(f"{proc}/{pid}").st_uid != os.getuid():
            return None
        with open(f"{proc}/{pid}/io", "rb") as handle:
            text = handle.read(1024).decode("ascii", "replace")
        values = dict(line.split(": ", 1) for line in text.splitlines() if ": " in line)
        return int(values["rchar"]) + int(values["wchar"])
    except (OSError, ValueError, KeyError):
        return None


def url_place(url: str) -> tuple[str, int]:
    """(host, port) of an MCP server URL, ("", 0) if it has none."""
    try:
        parts = urlsplit(url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        return "", 0
    return (parts.hostname.lower()[:253], port) if parts.hostname else ("", 0)


class Activity:
    """Remembers the last readings, to tell which servers were used since."""

    def __init__(self, resolve=socket.getaddrinfo):
        self.last = {}               # ((agent, name), root pid) -> (cpu ticks, io chars, monotonic seconds)
        self.sent = {}               # (pid, socket key) -> bytes sent + received
        self.addresses = {}          # host -> (addresses, looked up at)
        self.resolve = resolve

    def addresses_of(self, host: str, now: float) -> set:
        known = self.addresses.get(host)
        if known is not None and now - known[1] < RESOLVE_EVERY:
            return known[0]
        try:
            ipaddress.ip_address(host)
            found = {host}
        except ValueError:
            try:
                found = {info[4][0] for info in self.resolve(host, None, proto=socket.IPPROTO_TCP)[:16]}
            except (OSError, UnicodeError):
                found = set()
            if host == "localhost":
                found |= {"127.0.0.1", "::1"}
        self.addresses[host] = (found, now)
        return found

    def stdio_busy(self, servers: dict, now: float, proc: str = "/proc") -> set:
        """servers: {(agent, name): [[pids of one copy], …]} -> the keys in use since the last call."""
        busy, seen = set(), {}
        for key, copies in servers.items():
            for pids in copies:
                pipe = stdin_pipe(pids[0], proc)
                ticks = chars = 0
                for pid in pids:
                    ticks += agent_activity.cpu_ticks(pid, proc) or 0
                    if pipe and stdin_pipe(pid, proc) == pipe:
                        chars += io_chars(pid, proc) or 0
                copy = (key, pids[0])
                seen[copy] = (ticks, chars, now)
                before = self.last.get(copy)
                if before is None or now <= before[2]:
                    continue
                share = max(0, ticks - before[0]) / agent_activity.TICKS / (now - before[2])
                if chars != before[1] or share >= CPU_BUSY:
                    busy.add(key)
        self.last = seen
        return busy

    def url_busy(self, urls: dict, owners: dict, socks: list, now: float) -> set:
        """urls: {(agent, name): url}; owners: pid -> (agent, program) (traffic.agent_processes);
        socks: traffic.read_sockets() -> the keys whose connection moved data since the last call."""
        places = {}
        for key, url in urls.items():
            host, port = url_place(url)
            if host:
                places[key] = (self.addresses_of(host, now), port)
        busy, seen = set(), {}
        for sock in socks:
            owner = owners.get(sock.pid)
            if owner is None:
                continue
            address, port = traffic.split_address(sock.remote)
            for key, (addresses, wanted) in places.items():
                if key[0] != owner[0] or port != wanted or address not in addresses:
                    continue
                conn = (sock.pid, sock.inode or f"{sock.local}>{sock.remote}")
                moved = sock.sent + sock.received
                seen[conn] = moved
                if conn in self.sent and moved != self.sent[conn]:
                    busy.add(key)
        self.sent = seen
        return busy

    def in_use(self, working_pids: dict, entries: dict, processes, parent_of, read_socks=traffic.read_sockets,
               proc: str = "/proc", now: float | None = None) -> set:
        """working_pids: {agent: [pids]} of the agents working now; entries: {agent: {name: normal
        entry (mcp_ctl.normal)}} -> {(agent, name)} of the servers they use right now."""
        now = time.monotonic() if now is None else now
        stdio, urls = {}, {}
        for agent, pids in working_pids.items():
            servers = entries.get(agent, {})
            for name, copies in server_processes(pids, servers, processes, parent_of).items():
                stdio[(agent, name)] = copies
            for name, entry in servers.items():
                if entry.get("transport") in ("http", "sse") and entry.get("url"):
                    urls[(agent, name)] = entry["url"]
        busy = self.stdio_busy(stdio, now, proc)
        if urls:
            owners = traffic.agent_processes(working_pids, processes, parent_of)
            busy |= self.url_busy(urls, owners, read_socks() if owners else [], now)
        else:
            self.sent = {}
        return busy
