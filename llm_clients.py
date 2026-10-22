"""Which programs use a local LLM: the client ends of the connections to its port, named.

- /proc/net/tcp and tcp6: connections to the runtime's port that are ESTABLISHED and belong to
  this user (the uid column), their socket inodes;
- /proc/<pid>/fd of this user's processes: which process holds each of those sockets (only
  looked at while there is such a connection);
- the name: a plugin's manifest name when the process (or a parent of it) runs from a plugin
  folder (Video Trans), an agent's name, else the program's name.

Not counted: this app itself (its own status requests, the watcher) and monitors that only
read the runtime's status (LLM Monitor). Every request opens a short connection, so a client
is remembered for KEEP seconds after it was last seen. Only reads /proc; bounded.
"""

import json
import os
import re
import threading
import time
from pathlib import Path

HOME = Path.home()
APP_DIR = str(Path(__file__).resolve().parent)
PLUGINS = str(HOME / ".config/omarchy/plugins")
MONITORS = {"ubruckhaus.llm-usage"}          # LLM Monitor: reads the runtime's status only
INTERPRETERS = ("node", "bun", "deno", "python", "python3", "java", "ruby", "perl")
# Programs that run on behalf of the one that started them: their parent is asked (a plugin's
# python helper, its curl). An app started from a launcher is never named after the launcher.
HELPERS = ("sh", "bash", "dash", "zsh", "fish", "curl", "wget", "ffmpeg", "python", "python3", "node", "bun",
           "deno", "uv", "uvx", "npx")
KEEP = 30.0                                  # seconds a client stays listed after its last connection
ACTIVE = 10.0                                # seconds: seen this recently, it is using the LLM now (an app such
                                             # as Video Trans asks in bursts, a sentence at a time)
MAX_PIDS = 4096
MAX_FDS = 4096
MAX_PARENTS = 8
MAX_TCP = 8 * 1024 * 1024                    # bytes read of /proc/net/tcp and tcp6 each
MAX_NAMES = 16                               # programs remembered per local LLM
MAX_MANIFESTS = 64
PLUGIN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

_manifests = {}


def _read(path: str, limit: int = 8192) -> bytes:
    try:
        with open(path, "rb") as handle:
            return handle.read(limit)
    except OSError:
        return b""


def client_inodes(ports, net: str = "/proc/net", uid: int | None = None) -> dict:
    """{port: {socket inode}} of this user's established connections to these local ports."""
    ports = {p for p in ports if p}
    uid = os.getuid() if uid is None else uid
    found = {}
    for name in ("tcp", "tcp6"):
        lines = _read(f"{net}/{name}", MAX_TCP).decode("ascii", "replace").splitlines()[1:]
        for line in lines:
            fields = line.split()
            if len(fields) <= 9 or fields[3] != "01" or fields[7] != str(uid) or fields[9] == "0":
                continue
            try:
                port = int(fields[2].rpartition(":")[2], 16)
            except ValueError:
                continue
            if port in ports:
                found.setdefault(port, set()).add(fields[9])
    return found


def socket_owners(inodes, proc: str = "/proc") -> dict:
    """{socket inode: pid} for this user's processes holding these sockets."""
    wanted, owners, uid = set(inodes), {}, os.getuid()
    if not wanted:
        return owners
    try:
        pids = [p for p in os.listdir(proc) if p.isdigit()][:MAX_PIDS]
    except OSError:
        return owners
    for pid in pids:
        try:
            if os.stat(f"{proc}/{pid}").st_uid != uid:
                continue
            fds = os.listdir(f"{proc}/{pid}/fd")
        except OSError:
            continue
        for fd in fds[:MAX_FDS]:
            try:
                target = os.readlink(f"{proc}/{pid}/fd/{fd}")
            except OSError:
                continue
            if target.startswith("socket:[") and target[8:-1] in wanted:
                owners[target[8:-1]] = int(pid)
                wanted.discard(target[8:-1])
        if not wanted:
            break
    return owners


def _argv(pid: int, proc: str) -> list[str]:
    return [a.decode("utf-8", "replace") for a in _read(f"{proc}/{pid}/cmdline").split(b"\0") if a]


def _own(pid: int, proc: str) -> bool:
    try:
        return os.stat(f"{proc}/{pid}").st_uid == os.getuid()
    except OSError:
        return False


def _parent(pid: int, proc: str) -> int:
    fields = _read(f"{proc}/{pid}/stat", 1024).decode("utf-8", "replace").rpartition(")")[2].split()
    return int(fields[1]) if len(fields) > 1 and fields[1].isdigit() else 0


def plugin_name(plugin_id: str) -> str:
    """A plugin's own name from its manifest (cached), else its id."""
    if plugin_id not in _manifests:
        if len(_manifests) >= MAX_MANIFESTS:
            _manifests.clear()
        name = ""
        try:
            data = json.loads(_read(f"{PLUGINS}/{plugin_id}/manifest.json", 65536) or b"{}")
            name = data.get("name") if isinstance(data, dict) and isinstance(data.get("name"), str) else ""
        except ValueError:
            pass
        _manifests[plugin_id] = " ".join(name.split())[:60] or plugin_id
    return _manifests[plugin_id]


def _plugin_of(argv) -> str:
    """The plugin folder a command line runs from (a plain id only: no "..", no "/")."""
    for arg in argv[:8]:
        if arg.startswith(PLUGINS + "/"):
            plugin = arg[len(PLUGINS) + 1:].split("/", 1)[0]
            return plugin if PLUGIN_ID.match(plugin) and plugin.strip(".") else ""
    return ""


def program_name(argv) -> str:
    """The program a command line runs: python3 …/backend.py → backend, node …/bin/x → x."""
    if not argv:
        return ""
    name = os.path.basename(argv[0])
    if name.startswith(INTERPRETERS):
        script = next((a for a in argv[1:] if not a.startswith("-")), "")
        name = os.path.basename(script) or name
    for suffix in (".py", ".js", ".mjs", ".exe", ".AppImage"):
        name = name.removesuffix(suffix)
    return name[:60]


def app_name(pid: int, agent_names: dict | None = None, proc: str = "/proc") -> str | None:
    """Who a client process is, or None when it is not counted (this app, a monitor)."""
    argv = _argv(pid, proc)
    if any(a.startswith(APP_DIR + "/") for a in argv[:8]):
        return None                                  # our own status requests, the watcher
    names = agent_names or {}
    program = program_name(argv)
    if program in names:
        return names[program]
    current, current_argv = pid, argv
    for _step in range(MAX_PARENTS):
        plugin = _plugin_of(current_argv)
        if plugin:
            return None if plugin in MONITORS else plugin_name(plugin)
        if not current_argv or not os.path.basename(current_argv[0]).startswith(HELPERS):
            break                                    # a program of its own: named by itself
        current = _parent(current, proc)
        if current <= 1 or not _own(current, proc):
            break                                    # never named after another account's process
        current_argv = _argv(current, proc)
        if any(a.startswith(APP_DIR + "/") for a in current_argv[:8]):
            break                                    # started from here (an agent's terminal)
    return program or None


class Clients:
    """Remembers who used which local LLM lately (update every few seconds)."""

    def __init__(self):
        self.seen = {}           # runtime key -> {name: monotonic time last seen}
        self.lock = threading.Lock()

    def update(self, ports: dict, agent_names: dict | None = None, net: str = "/proc/net",
               proc: str = "/proc", now: float | None = None, samples: int = 1, interval: float = 0.1,
               probe=None, busy: float | None = None) -> float | None:
        """ports: {runtime key: port} of the running local LLMs. A request holds its connection for a
        fraction of a second, so the connections are looked at `samples` times, `interval` apart
        (the processes only for connections not seen before). probe: read at every sample too (the
        GPU's busy percent, which a short reply raises only for a moment); returns its highest value.
        busy: with it, a sample reads the connections only while the probe is at least that high or
        once one was seen in this update: reading /proc/net/tcp costs ~3.5 ms of CPU (the kernel walks
        its whole socket table) and a local LLM on the GPU that answers keeps the GPU busy."""
        names, seen, inodes = {}, {}, {}           # pid -> name; key -> {name: time}; inode -> name
        peak = None
        for sample in range(max(1, samples)):
            if sample:
                time.sleep(interval)
            value = probe() if probe is not None else None
            if value is not None:
                peak = value if peak is None else max(peak, value)
            if busy is not None and value is not None and value < busy and not inodes:
                continue                             # the GPU is idle and nobody connected: nothing served
            at = time.monotonic() if now is None else now
            by_port = client_inodes(ports.values(), net)
            new = {i for found in by_port.values() for i in found} - inodes.keys()
            owners = socket_owners(new, proc)
            for inode in new:
                pid = owners.get(inode)
                if pid is not None and pid != os.getpid() and pid not in names:
                    names[pid] = app_name(pid, agent_names, proc)
                inodes[inode] = names.get(pid) if pid is not None and pid != os.getpid() else None
            for key, port in ports.items():
                for inode in by_port.get(port, ()):
                    if inodes.get(inode):
                        seen.setdefault(key, {})[inodes[inode]] = at
        now = time.monotonic() if now is None else now
        with self.lock:
            for key, found in seen.items():
                rows = self.seen.setdefault(key, {})
                rows.update(found)
                if len(rows) > MAX_NAMES:            # the most recent ones
                    self.seen[key] = dict(sorted(rows.items(), key=lambda r: r[1])[-MAX_NAMES:])
            self.seen = {key: kept for key, rows in self.seen.items()
                         if key in ports and (kept := {n: t for n, t in rows.items() if now - t <= KEEP})}
        return peak

    def recent(self, key: str, now: float | None = None) -> list[tuple[str, float]]:
        """[(name, seconds since last seen)] newest first."""
        now = time.monotonic() if now is None else now
        with self.lock:
            rows = list(self.seen.get(key, {}).items())
        return sorted(((n, max(0.0, now - t)) for n, t in rows), key=lambda x: x[1])

    def active(self, key: str, now: float | None = None) -> bool:
        return any(age <= ACTIVE for _n, age in self.recent(key, now))
