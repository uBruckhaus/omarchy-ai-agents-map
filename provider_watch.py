#!/usr/bin/env python3
"""Start an agent's local LLMs when the agent starts, stop them when it closes.

Runs as the user service ai-agents-map-providers.service (only while the
setting "Start local LLMs with their agents" is on). Once a second it looks at
this user's processes (/proc only):

- an agent that is connected to a local LLM starts → `systemctl --user start`
  that LLM's user service (LM Studio: `lms server start`); nothing happens if
  it is running already;
- the last running agent connected to it closes → after a short grace time the
  LLM is stopped, which frees its GPU memory. It is not stopped while another
  program still has a connection open to it (a chat app, Simple Recipes, …).

What is connected to what comes from providers-with-agents.json, which the
popup writes each time it looks at the agents' settings. Only runtime keys and
unit names are read from it (both checked), never a command line: the
commands are built here.

Plugin side: write_config() and ensure_service() (stdlib only too).
"""

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
CONFIG = HOME / ".config/omarchy/ai-agents-map/providers-with-agents.json"
STATE = HOME / ".local/state/ai-agents-map/provider-watch.json"   # what we still have to stop (survives restarts)
UNIT_NAME = "ai-agents-map-providers.service"
UNIT_FILE = HOME / ".config/systemd/user" / UNIT_NAME
RUNTIMES = ("llamacpp", "ollama", "lmstudio", "vllm")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}$")
UNIT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9@_.-]{0,99}$")
INTERPRETERS = ("node", "bun", "deno", "python", "python3")
BACKGROUND_ARGS = {"serve", "server", "daemon", "--service", "--daemon"}
NO_PROVIDERS_ENV = b"AI_AGENTS_MAP_NO_PROVIDERS=1"   # the popup's own agent runs (looking up agents)
INTERVAL = 1.0
GRACE = 5.0            # an agent restarted at once keeps its LLM
RETRY = 30.0           # a start that failed is tried again after this
TIMEOUT = 60
MAX_CONFIG = 64 * 1024


def log(text: str):
    print(text, flush=True)


# --- the configuration ----------------------------------------------------------------------

def _entry(key, entry) -> dict | None:
    """A checked {"service": unit, "port": int} for a runtime key, or None."""
    if key not in RUNTIMES or not isinstance(entry, dict):
        return None
    service = entry.get("service") if isinstance(entry.get("service"), str) else ""
    port = entry.get("port") if isinstance(entry.get("port"), int) and 0 < entry.get("port") < 65536 else 0
    if service and not UNIT_RE.fullmatch(service):
        return None
    return {"service": service, "port": port} if service or key == "lmstudio" else None


def read_config(path: Path = CONFIG) -> tuple[dict, dict]:
    """({agent: [runtime keys]}, {runtime key: {"service": unit, "port": int}}), checked."""
    try:
        with open(path, "rb") as handle:
            data = json.loads(handle.read(MAX_CONFIG + 1)[:MAX_CONFIG])
    except (OSError, ValueError):
        return {}, {}
    if not isinstance(data, dict):
        return {}, {}
    agents, runtimes = {}, {}
    for key, entry in (data.get("runtimes") or {}).items() if isinstance(data.get("runtimes"), dict) else ():
        checked = _entry(key, entry)
        if checked:
            runtimes[key] = checked
    for agent, keys in (data.get("agents") or {}).items() if isinstance(data.get("agents"), dict) else ():
        if NAME_RE.fullmatch(agent) and isinstance(keys, list):
            wanted = [k for k in keys if k in runtimes]
            if wanted:
                agents[agent] = wanted
    return agents, runtimes


def write_config(inventory, path: Path = CONFIG) -> bool:
    """Plugin side: which agents use which local LLM (inventory.Runtime.agents).
    Returns True when the file changed."""
    runtimes, agents = {}, {}
    for rt in inventory.runtimes:
        if not rt.installed or not rt.agents or not (rt.service or rt.key == "lmstudio"):
            continue
        port = rt.address.rpartition(":")[2]
        runtimes[rt.key] = {"service": rt.service, "port": int(port) if port.isdigit() else 0}
        for agent in rt.agents:
            agents.setdefault(agent, []).append(rt.key)
    text = json.dumps({"agents": agents, "runtimes": runtimes}, indent=2, sort_keys=True) + "\n"
    try:
        if path.read_text() == text:
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.new")
    tmp.write_text(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return True


# --- the user service (plugin side) ---------------------------------------------------------

def unit_text(script: Path) -> str:
    return f"""[Unit]
Description=AI Agents Map: start local LLMs with their agents, stop them when the agents close

[Service]
Type=simple
ExecStart=/usr/bin/python3 -I {script}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""


def _systemctl(*args) -> bool:
    try:
        return subprocess.run(["systemctl", "--user", *args], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=TIMEOUT).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def ensure_service(on: bool, script: Path | None = None) -> bool:
    """Plugin side: install + enable + start the watcher (on), or stop + disable +
    remove it (off). Rewrites the unit when the plugin folder moved. Blocking."""
    script = (script or Path(__file__)).resolve()
    if not on:
        if not UNIT_FILE.exists():
            return True
        _systemctl("disable", "--now", UNIT_NAME)
        UNIT_FILE.unlink(missing_ok=True)
        _systemctl("daemon-reload")
        return True
    text = unit_text(script)
    try:
        changed = UNIT_FILE.read_text() != text
    except OSError:
        changed = True
    if changed:
        UNIT_FILE.parent.mkdir(parents=True, exist_ok=True)
        UNIT_FILE.write_text(text)
        _systemctl("daemon-reload")
        _systemctl("enable", UNIT_NAME)
        return _systemctl("restart", UNIT_NAME)
    if not _systemctl("is-active", "--quiet", UNIT_NAME):
        _systemctl("enable", UNIT_NAME)
        return _systemctl("start", UNIT_NAME)
    return True


# --- processes ------------------------------------------------------------------------------

def _read(path: str, limit: int) -> bytes:
    try:
        with open(path, "rb") as handle:
            return handle.read(limit)
    except OSError:
        return b""


def _bare(name: str) -> str:
    """opencode.exe (OpenCode's native binary behind its shim) is opencode."""
    return name.removesuffix(".exe")


def running_agents(names, proc: str = "/proc") -> tuple[set, set]:
    """(agents of `names` that have a process of this user right now, pids of
    agents' background servers). A background server (OpenCode 2's
    `opencode serve --service`, a user service running all the time) is not the
    agent being open: it is left out, and its idle connections do not keep an
    LLM running (socket_inodes)."""
    names = set(names)
    found, background, uid = set(), set(), os.getuid()
    try:
        pids = [p for p in os.listdir(proc) if p.isdigit()]
    except OSError:
        return found, background
    for pid in pids:
        try:
            if os.stat(f"{proc}/{pid}").st_uid != uid:
                continue
        except OSError:
            continue
        comm = _bare(_read(f"{proc}/{pid}/comm", 64).decode("utf-8", "replace").strip())
        if comm not in names and not comm.startswith(INTERPRETERS) and len(comm) < 15:
            continue           # most processes: one small read only
        argv = [a.decode("utf-8", "replace") for a in _read(f"{proc}/{pid}/cmdline", 8192).split(b"\0") if a]
        if not argv:
            continue
        name, args = _bare(os.path.basename(argv[0])), argv[1:]
        if name not in names and name.startswith(INTERPRETERS) and len(argv) > 1:
            name, args = _bare(os.path.basename(argv[1])), argv[2:]   # node …/bin/opencode, python3 ~/.local/bin/hermes
        if name not in names and comm in names:
            name = comm
        if name not in names:
            continue
        if any(a in BACKGROUND_ARGS for a in args[:3]):
            background.add(int(pid))
        elif NO_PROVIDERS_ENV not in _read(f"{proc}/{pid}/environ", 256 * 1024).split(b"\0"):
            found.add(name)
    return found, background


def socket_inodes(pids, proc: str = "/proc") -> set:
    """Inodes of the sockets these processes hold (to tell their connections apart)."""
    inodes = set()
    for pid in pids:
        try:
            fds = os.listdir(f"{proc}/{pid}/fd")
        except OSError:
            continue
        for fd in fds[:4096]:
            try:
                target = os.readlink(f"{proc}/{pid}/fd/{fd}")
            except OSError:
                continue
            if target.startswith("socket:["):
                inodes.add(target[8:-1])
    return inodes


def in_use(port: int, ignore=frozenset(), proc: str = "/proc/net") -> bool:
    """Someone has a connection open to this loopback port (ESTABLISHED), not
    counting the client sockets in `ignore` (agents' background servers)."""
    if not port:
        return False
    for name in ("tcp", "tcp6"):
        try:
            lines = Path(proc, name).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) > 9 and fields[3] == "01" and int(fields[2].rpartition(":")[2], 16) == port \
                    and fields[9] not in ignore:
                return True
    return False


# --- start / stop ---------------------------------------------------------------------------

def lms_binary() -> str | None:
    found = shutil.which("lms")
    if found:
        return found
    candidate = HOME / ".lmstudio/bin/lms"
    return str(candidate) if candidate.is_file() else None


def lmstudio_up(proc: str = "/proc") -> bool:
    """LM Studio (its app or daemon) runs already: we did not start it, so we leave it running."""
    uid = os.getuid()
    try:
        pids = [p for p in os.listdir(proc) if p.isdigit()]
    except OSError:
        return False
    for pid in pids:
        try:
            if os.stat(f"{proc}/{pid}").st_uid != uid:
                continue
        except OSError:
            continue
        exe = _read(f"{proc}/{pid}/cmdline", 4096).split(b"\0")[0]
        if b"/.lmstudio/" in exe or b"lm-studio" in exe.lower() or b"/.mount_lm-stu" in exe:
            return True
    return False


def lmstudio_service_pids(proc: str = "/proc") -> list[int]:
    """LM Studio started headless by `lms server start` (lm-studio --run-as-service),
    never an LM Studio window."""
    found, uid = [], os.getuid()
    try:
        pids = [p for p in os.listdir(proc) if p.isdigit()]
    except OSError:
        return found
    for pid in pids:
        try:
            if os.stat(f"{proc}/{pid}").st_uid != uid:
                continue
        except OSError:
            continue
        argv = _read(f"{proc}/{pid}/cmdline", 4096).split(b"\0")
        if argv and b"lm-studio" in os.path.basename(argv[0]).lower() and b"--run-as-service" in argv:
            found.append(int(pid))
    return found


def stop_lmstudio_service(argv0: str):
    """After `lms server stop`: the background LM Studio we started goes too. Each
    process is held by a pidfd from the moment it is found, so a signal can never
    reach another process that got the same PID meanwhile."""
    run([argv0, "daemon", "down"])
    handles = []
    for pid in lmstudio_service_pids():
        try:
            handles.append(os.pidfd_open(pid))
        except OSError:
            continue           # gone already
    try:
        for fd in handles:
            try:
                signal.pidfd_send_signal(fd, signal.SIGTERM)
            except OSError:
                pass
        # It ignores SIGTERM: its server is stopped already, so it may be ended.
        deadline = time.monotonic() + 5
        while handles and time.monotonic() < deadline:
            time.sleep(0.5)
            handles = [fd for fd in handles if not _exited(fd) or os.close(fd)]
        for fd in handles:
            try:
                signal.pidfd_send_signal(fd, signal.SIGKILL)
            except OSError:
                pass
    finally:
        for fd in handles:
            try:
                os.close(fd)
            except OSError:
                pass


def _exited(fd: int) -> bool:
    """A pidfd becomes readable once its process has exited."""
    import select
    readable, _w, _x = select.select([fd], [], [], 0)
    return bool(readable)


def command(key: str, entry: dict, action: str) -> list | None:
    if entry.get("service"):
        return ["systemctl", "--user", action, f"{entry['service']}.service"]
    if key == "lmstudio" and lms_binary():
        return [lms_binary(), "server", action]
    return None


def run(argv) -> bool:
    try:
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, timeout=TIMEOUT)
    except (OSError, subprocess.SubprocessError) as error:
        log(f"{' '.join(argv)}: {error}")
        return False
    if result.returncode:
        log(f"{' '.join(argv)}: {result.stderr.decode('utf-8', 'replace').strip()[-300:]}")
    return result.returncode == 0


class Watcher:
    """One tick a second. `pending` (needed now or waiting to be stopped) is saved
    to STATE with each runtime's service and port, so a restart (reboot, plugin
    update) or an LLM that lost its last connection is still stopped."""

    def __init__(self, config: Path = CONFIG, runner=run, clock=time.monotonic, agents_now=running_agents,
                 busy=in_use, state: Path | None = STATE):
        self.config, self.run, self.clock, self.agents_now, self.busy = config, runner, clock, agents_now, busy
        self.state = state
        self.mtime = None
        self.agents, self.runtimes = {}, {}
        self.known = {}              # runtime -> {"service", "port"}: how to stop it, even once off the list
        self.needed = set()          # runtimes a running agent needs
        self.started = {}            # runtime -> time of our last start
        self.stop_at = {}            # runtime -> when to stop it
        self.lms_was_up = True       # LM Studio ran before we started its server: keep its daemon
        self.lms_up = lmstudio_up
        self.stop_lms = stop_lmstudio_service
        self.sockets = socket_inodes
        self.background = set()
        self.saved = None
        self.load_state()

    # --- saved state ----------------------------------------------------------------------

    def load_state(self):
        if self.state is None:
            return
        try:
            with open(self.state, "rb") as handle:
                data = json.loads(handle.read(MAX_CONFIG + 1)[:MAX_CONFIG])
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        for key, entry in (data.get("pending") or {}).items() if isinstance(data.get("pending"), dict) else ():
            checked = _entry(key, entry)
            if checked:
                self.known[key] = checked
                self.needed.add(key)       # the first tick stops it unless an agent needs it now
        self.lms_was_up = data.get("lms_was_up") is not False

    def save_state(self):
        if self.state is None:
            return
        pending = {key: self.known[key] for key in sorted(self.needed | set(self.stop_at)) if key in self.known}
        data = {"pending": pending, "lms_was_up": self.lms_was_up}
        if data == self.saved:
            return
        try:
            self.state.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state.with_name(f".{self.state.name}.new")
            tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.state)
            self.saved = data
        except OSError as error:
            log(f"could not save {self.state}: {error}")

    def load(self):
        try:
            mtime = self.config.stat().st_mtime_ns
        except OSError:
            mtime = None
        if mtime != self.mtime:
            self.mtime = mtime
            self.agents, self.runtimes = read_config(self.config)
            self.known.update(self.runtimes)

    # --- one look -------------------------------------------------------------------------

    def look(self) -> set:
        """The runtimes the running agents need now."""
        self.load()
        running, background = self.agents_now(self.agents) if self.agents else (set(), set())
        self.background = background
        self.running = running
        return {key for agent in running for key in self.agents.get(agent, ())}

    def stop(self, key, why: str) -> bool:
        """Stop one runtime unless another program is using it. True when done with it."""
        entry = self.runtimes.get(key) or self.known.get(key)
        if entry is None:
            return True
        if self.busy(entry.get("port", 0), self.sockets(self.background)):
            return False            # another program is using it: wait until it is done
        argv = command(key, entry, "stop")
        if argv:
            ok = self.run(argv)
            log(f"stop {key} ({why}): {'ok' if ok else 'failed'}")
            if key == "lmstudio" and not self.lms_was_up:
                # `lms server start` also started LM Studio's daemon (the app in the background).
                self.stop_lms(argv[0])
        self.started.pop(key, None)
        self.started.pop(f"{key}:ok", None)
        return True

    def tick(self):
        needed = self.look()
        now = self.clock()
        for key in sorted(needed):
            self.stop_at.pop(key, None)
            if key not in self.needed or (key in self.started and now - self.started[key] > RETRY
                                          and not self.started.get(f"{key}:ok")):
                argv = command(key, self.runtimes[key], "start")
                if argv and key == "lmstudio" and key not in self.needed:
                    self.lms_was_up = self.lms_up()
                if argv:
                    ok = self.run(argv)
                    self.started[key] = now
                    self.started[f"{key}:ok"] = ok
                    log(f"start {key} for {', '.join(sorted(a for a in self.running if key in self.agents[a]))}: "
                        f"{'ok' if ok else 'failed'}")
        for key in self.needed - needed:
            self.stop_at.setdefault(key, now + GRACE)
        for key, when in list(self.stop_at.items()):
            if key not in needed and now >= when and self.stop(key, "no agent using it is running"):
                del self.stop_at[key]
        self.needed = needed
        self.save_state()

    def shutdown(self):
        """The service stops (setting off, last connection gone, logout, plugin update):
        stop at once what no running agent needs; the rest stays saved for next time."""
        try:
            needed = self.look()
        except Exception:
            needed = set()
        for key in sorted((self.needed | set(self.stop_at)) - needed):
            if self.stop(key, "the watcher stops"):
                self.stop_at.pop(key, None)
        self.needed = needed | set(self.stop_at)
        self.stop_at = {}
        self.save_state()


def main():
    watcher = Watcher()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    log("watching agents and their local LLMs")
    try:
        while True:
            try:
                watcher.tick()
            except Exception as error:      # keep watching; systemd would restart us anyway
                log(f"error: {error}")
            time.sleep(INTERVAL)
    finally:
        watcher.shutdown()


if __name__ == "__main__":
    sys.exit(main())
