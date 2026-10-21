"""An agent's network traffic: what it and the programs it started send and receive.

Read from the TCP connections of this user's own processes (`ss -tinpeHn`: no root needed, each
connection with its owner and its bytes sent and received). Every sample adds what each connection
moved since the last one to the agent that owns it (the agent's process or one it started: its MCP
servers, its tools), per destination (an online API, a local LLM on this PC, the local network,
others) and per program, and keeps a few minutes of history for the chart. Only while the app is
open; UDP (QUIC) is not counted.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass, field

SAMPLE_EVERY = 3                 # seconds
HISTORY = 100                    # samples kept for the chart (5 minutes)
MAX_OUTPUT = 4 * 1024 * 1024
MAX_SOCKETS = 4000
MAX_PLACES = 200                 # destinations kept per agent

# Online APIs agents talk to, found by their addresses (looked up once, again every 10 minutes).
KNOWN_HOSTS = {
    "api.anthropic.com": "Anthropic API", "claude.ai": "Claude", "statsig.anthropic.com": "Anthropic",
    "api.openai.com": "OpenAI API", "chatgpt.com": "ChatGPT", "auth.openai.com": "OpenAI",
    "api.githubcopilot.com": "GitHub Copilot", "api.github.com": "GitHub",
    "generativelanguage.googleapis.com": "Google Gemini API", "cloudcode-pa.googleapis.com": "Google Gemini",
    "openrouter.ai": "OpenRouter", "api.deepseek.com": "DeepSeek API", "api.x.ai": "xAI API",
    "api.mistral.ai": "Mistral API", "ollama.com": "Ollama cloud", "api.cursor.sh": "Cursor",
    "registry.npmjs.org": "npm registry", "pypi.org": "PyPI", "huggingface.co": "Hugging Face",
}

LINE = re.compile(r"^(?P<state>\S+)\s+\d+\s+\d+\s+(?P<local>\S+)\s+(?P<remote>\S+)\s*(?P<rest>.*)$")
USER = re.compile(r'\("(?P<comm>[^"]{1,64})",pid=(?P<pid>\d+),fd=\d+\)')


@dataclass
class Sock:
    local: str
    remote: str
    pid: int
    comm: str
    sent: int = 0
    received: int = 0
    inode: str = ""


def parse_ss(text: str) -> list:
    """Connections from `ss -tinpeHn`: a line per connection, its counters on the indented line
    after it. Only ones with an owner (ss shows owners of this user's own processes)."""
    socks, current = [], None
    for line in text.splitlines()[:MAX_SOCKETS * 2]:
        if line[:1] in (" ", "\t"):
            if current is not None:
                for key, attr in (("bytes_sent", "sent"), ("bytes_received", "received")):
                    found = re.search(rf"\b{key}:(\d+)", line)
                    if found:
                        setattr(current, attr, int(found.group(1)))
            continue
        current = None
        match = LINE.match(line.strip())
        if not match:
            continue
        rest = match.group("rest")
        user = USER.search(rest)
        if not user:
            continue
        inode = re.search(r"\bino:(\d+)", rest)
        current = Sock(match.group("local"), match.group("remote"), int(user.group("pid")), user.group("comm"),
                       inode=inode.group(1) if inode else "")
        for key, attr in (("bytes_sent", "sent"), ("bytes_received", "received")):
            found = re.search(rf"\b{key}:(\d+)", rest)
            if found:
                setattr(current, attr, int(found.group(1)))
        socks.append(current)
    return socks[:MAX_SOCKETS]


def read_sockets() -> list:
    import ai_core
    out, ok = ai_core.read_bounded_output(["ss", "-tinpeHn"], max_bytes=MAX_OUTPUT, timeout=5)
    return parse_ss(out.decode("utf-8", "replace")) if ok or out else []


def split_address(text: str) -> tuple[str, int]:
    """"[::1]:8080" / "1.2.3.4:443" / "[::ffff:1.2.3.4]:443" -> (address, port)."""
    host, _sep, port = text.rpartition(":")
    host = host.strip("[]")
    if host.startswith("::ffff:"):
        host = host[7:]
    host = host.split("%")[0]
    return host, int(port) if port.isdigit() else 0


# --- where it goes --------------------------------------------------------------------------------

class Names:
    """Names for remote addresses: known online APIs (by their addresses), local LLMs (by their
    port), the local network; the rest by reverse lookup, done on a thread and kept."""

    def __init__(self):
        self.known = {}             # address -> API name
        self.reverse = {}           # address -> host name ("" while not known)
        self.known_at = 0.0
        self.lock = threading.Lock()

    def refresh_known(self, resolve=socket.getaddrinfo):
        if time.monotonic() - self.known_at < 600 and self.known_at:
            return
        self.known_at = time.monotonic()
        found = {}
        for host, label in KNOWN_HOSTS.items():
            try:
                for info in resolve(host, 443, proto=socket.IPPROTO_TCP)[:8]:
                    found.setdefault(info[4][0], label)       # the first name of an address wins
            except OSError:
                continue
        with self.lock:
            self.known = found

    def lookup_later(self, address: str):
        with self.lock:
            if address in self.reverse or len(self.reverse) > 2000:
                return
            self.reverse[address] = ""

        def work():
            try:
                name = socket.gethostbyaddr(address)[0]
            except (OSError, UnicodeError):
                name = ""
            with self.lock:
                self.reverse[address] = name[:120]
        threading.Thread(target=work, daemon=True).start()

    def place(self, remote: str, local_llms: dict) -> tuple[str, str]:
        """(name, kind) of a connection's other end; kind: "api", "local-llm", "local", "lan", "web"."""
        address, port = split_address(remote)
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return remote, "web"
        if ip.is_loopback:
            if port in local_llms:
                return local_llms[port], "local-llm"
            return f"localhost:{port}", "local"
        if ip.is_private or ip.is_link_local:
            return address, "lan"
        with self.lock:
            known = self.known.get(address)
            name = self.reverse.get(address)
        if known:
            return known, "api"
        if name is None:
            self.lookup_later(address)
        return name or address, "web"


# --- the meter ------------------------------------------------------------------------------------

@dataclass
class Totals:
    received: int = 0
    sent: int = 0
    connections: int = 0


@dataclass
class Stats:
    """One agent's traffic since the app saw it start (or since the app opened)."""
    since: float = field(default_factory=time.time)
    received: int = 0
    sent: int = 0
    connections: int = 0                                   # open now
    places: dict = field(default_factory=dict)             # (name, kind) -> Totals
    programs: dict = field(default_factory=dict)           # program name -> Totals
    history: deque = field(default_factory=lambda: deque(maxlen=HISTORY))   # (time, in B/s, out B/s)

    @property
    def rate(self) -> tuple[float, float]:
        """(in, out) bytes per second over the last sample."""
        return self.history[-1][1:] if self.history else (0.0, 0.0)


class Meter:
    def __init__(self, names: Names | None = None):
        self.names = names or Names()
        self.stats = {}             # agent command -> Stats
        self.last = {}              # connection key -> (sent, received)
        self.when = 0.0

    def update(self, socks: list, owners: dict, local_llms: dict, now: float | None = None):
        """owners: pid -> (agent command, program name) for the running agents' processes."""
        now = time.monotonic() if now is None else now
        dt = now - self.when if self.when else 0.0
        self.when = now
        seen, moved, open_now = {}, {}, {}
        for sock in socks:
            owner = owners.get(sock.pid)
            if owner is None:
                continue
            agent, program = owner
            key = (sock.pid, sock.inode or f"{sock.local}>{sock.remote}")
            seen[key] = (sock.sent, sock.received)
            old_sent, old_received = self.last.get(key, (0, 0))
            sent, received = max(0, sock.sent - old_sent), max(0, sock.received - old_received)
            stats = self.stats.setdefault(agent, Stats())
            open_now[agent] = open_now.get(agent, 0) + 1
            place = self.names.place(sock.remote, local_llms)
            if place not in stats.places and len(stats.places) >= MAX_PLACES:
                place = ("…", "web")
            for table, name in ((stats.places, place), (stats.programs, program)):
                totals = table.setdefault(name, Totals())
                totals.sent += sent
                totals.received += received
            tally = moved.setdefault(agent, [0, 0])
            tally[0] += received
            tally[1] += sent
        self.last = seen
        for agent, stats in self.stats.items():
            received, sent = moved.get(agent, (0, 0))
            stats.received += received
            stats.sent += sent
            stats.connections = open_now.get(agent, 0)
            for name in list(stats.places):
                stats.places[name].connections = 0
            if dt > 0:
                stats.history.append((time.time(), received / dt, sent / dt))
        for sock in socks:
            owner = owners.get(sock.pid)
            if owner is not None:
                place = self.names.place(sock.remote, local_llms)
                totals = self.stats[owner[0]].places.get(place)
                if totals is not None:
                    totals.connections += 1


def agent_processes(roots: dict, processes, parent_of) -> dict:
    """pid -> (agent command, program name): each agent's own processes and everything they started.
    roots: agent command -> [pids]; processes: (pid, comm, argv); parent_of(pid) -> parent pid."""
    owner_of = {pid: command for command, pids in roots.items() for pid in pids}
    found = {}
    for pid, comm, argv in processes:
        current, steps = pid, 0
        while current and steps < 64:
            if current in owner_of:
                program = (argv[0].rsplit("/", 1)[-1] if argv else comm) or comm
                found[pid] = (owner_of[current], program[:60])
                break
            current, steps = parent_of(current), steps + 1
    return found


def size_text(size: float) -> str:
    for unit, step in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if size >= step:
            return f"{size / step:.1f} {unit}"
    return f"{int(size)} B"


def rate_text(rate: float) -> str:
    return size_text(rate) + "/s"
