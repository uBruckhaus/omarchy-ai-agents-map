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


def _default_config() -> Path:
    """Plugin side: in the desktop's folder (desktop.config_dir()). The service runs with
    `python3 -I` (the plugin folder is not on sys.path) and gets the file with --config
    from its unit; a unit written before --config existed is an Omarchy one."""
    try:
        import desktop
    except ImportError:
        return HOME / ".config/omarchy/ai-agents-map/providers-with-agents.json"
    return desktop.config_dir() / "providers-with-agents.json"


CONFIG = _default_config()
_UNIT_SAFE = re.compile(r"[A-Za-z0-9/._+-]{1,1024}")       # a path systemd takes as one word, unquoted
STATE = HOME / ".local/state/ai-agents-map/provider-watch.json"   # what we still have to stop (survives restarts)
def _unit_name() -> str:
    """The plugin's watcher, or Asterism's: two installs on one machine never share a unit."""
    try:
        import desktop
    except ImportError:
        return "ai-agents-map-providers.service"      # the service itself (python3 -I) never needs it
    return "ai-agents-map-providers.service" if desktop.NAME == "omarchy" else "asterism-providers.service"


UNIT_NAME = _unit_name()
UNIT_FILE = HOME / ".config/systemd/user" / UNIT_NAME
# What the watcher knows of each local LLM comes with its config entry (written by the app from
# its provider adapters, providers.py): its service and port, and whether it stays up between
# agents (KoboldCpp: too slow to start for an agent's first request, no video memory without a
# model), then its model is unloaded with the "unload" request instead of stopping it.
UNLOAD_PATH_RE = re.compile(r"^/[A-Za-z0-9/_.-]{1,100}$")
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

def _unload(value) -> dict | None:
    """A checked {"path": "/…", "body": {str: str}} unload request, or None."""
    if not isinstance(value, dict) or not isinstance(value.get("path"), str) \
            or not UNLOAD_PATH_RE.fullmatch(value["path"]) or not isinstance(value.get("body"), dict):
        return None
    body = value["body"]
    if len(body) > 8 or not all(isinstance(k, str) and NAME_RE.fullmatch(k) and isinstance(v, str) and len(v) <= 100
                                for k, v in body.items()):
        return None
    return {"path": value["path"], "body": dict(body)}


# How a local LLM without a user service is started and stopped: controllers built in here (the
# config names one, never a command line).
CONTROLS = ("lms",)                  # LM Studio's own CLI: `lms server start|stop`


def _entry(key, entry) -> dict | None:
    """A checked {"service": unit, "port": int[, "control": name][, "stays_up": True, "unload": {...}]}
    for a runtime key, or None."""
    if not isinstance(key, str) or not NAME_RE.fullmatch(key) or not isinstance(entry, dict):
        return None
    service = entry.get("service") if isinstance(entry.get("service"), str) else ""
    port = entry.get("port") if isinstance(entry.get("port"), int) and 0 < entry.get("port") < 65536 else 0
    if service and not UNIT_RE.fullmatch(service):
        return None
    control = entry.get("control") if entry.get("control") in CONTROLS else ""
    if not service and not control:
        return None
    checked = {"service": service, "port": port, **({"control": control} if control else {})}
    unload_request = _unload(entry.get("unload"))
    if entry.get("stays_up") is True and unload_request is not None:
        checked.update(stays_up=True, unload=unload_request)
    return checked


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
    """Plugin side: which local LLM each agent uses now (inventory.Runtime.active_agents:
    only the provider of its current model is started with it, not every one it is
    connected to). Every connected LLM is listed under "runtimes", so the watcher
    knows how to stop it. Returns True when the file changed."""
    import harnesses      # app side only: the watcher itself never writes its config
    import llm_engine
    import providers
    self_served = {h.command for h in harnesses.all_harnesses() if h.self_served}
    runtimes, agents = {}, {}
    for rt in inventory.runtimes:
        control = llm_engine.of(rt).watch_control
        if not rt.installed or not rt.agents or not (rt.service or control):
            continue
        port = rt.address.rpartition(":")[2]
        runtimes[rt.key] = {"service": rt.service, "port": int(port) if port.isdigit() else 0,
                            **({"control": control} if control and not rt.service else {})}
        provider = providers.get(rt.key)
        if provider is not None and provider.stays_up and provider.unload:
            runtimes[rt.key].update(stays_up=True, unload={"path": provider.unload[0], "body": provider.unload[1]})
        for agent in getattr(rt, "active_agents", rt.agents):
            if agent not in self_served:
                agents.setdefault(agent, []).append(rt.key)
    text = json.dumps({"agents": agents, "runtimes": runtimes}, indent=2, sort_keys=True) + "\n"
    try:
        if path.read_text() == text:
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.new")
    tmp.write_text(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return True


# --- the user service (plugin side) ---------------------------------------------------------

def unit_text(script: Path, config: Path = None) -> str:
    config = config or CONFIG
    APP = "AI Agents Map" if UNIT_NAME.startswith("ai-agents-map") else "Asterism"   # noqa: N806
    exec_start = f"/usr/bin/python3 -I {script}" + (f" --config {config}" if _UNIT_SAFE.fullmatch(str(config)) else "")
    return f"""[Unit]
Description={APP}: start local LLMs with their agents, stop them when the agents close

[Service]
Type=simple
ExecStart={exec_start}
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
    instances, background = _scan_agents(names, proc)
    return set(instances), background


def agent_instances(names, proc: str = "/proc", pids: dict | None = None) -> dict:
    """{agent: [the arguments of each open instance]} (background servers left out);
    with pids, also {agent: [their process ids]} into it."""
    return _scan_agents(names, proc, pids)[0]


# Options that name the model an instance runs with ("-m" only where it means the model).
MODEL_OPTIONS = ("--model",)


def instance_model(agent: str, args, short_model=None) -> str:
    """The model an instance was started with (`--model x`, `--model=x`), "" = its settings'.
    short_model: agents where -m names the model (default: from the agent adapters; app side
    only, the watcher never asks)."""
    if short_model is None:
        import harnesses
        short_model = {h.command for h in harnesses.all_harnesses() if h.short_model_flag}
    options = MODEL_OPTIONS + (("-m",) if agent in short_model else ())
    for index, arg in enumerate(args):
        if arg in options and index + 1 < len(args):
            return args[index + 1].strip()[:200]
        for option in options:
            if option.startswith("--") and arg.startswith(option + "="):
                return arg[len(option) + 1:].strip()[:200]
    return ""


def _scan_agents(names, proc: str = "/proc", pids_out: dict | None = None) -> tuple[dict, set]:
    names = set(names)
    found, background, uid = {}, set(), os.getuid()
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
            found.setdefault(name, []).append(args)
            if pids_out is not None:
                pids_out.setdefault(name, []).append(int(pid))
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


def own_listener(port: int, proc: str = "/proc/net") -> bool:
    """127.0.0.1:port is listened on, by this user only (never send a request
    to a port another account could have taken)."""
    owners = set()
    for name in ("tcp", "tcp6"):
        try:
            lines = Path(proc, name).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) > 7 and fields[3] == "0A" and int(fields[1].rpartition(":")[2], 16) == port:
                owners.add(int(fields[7]) if fields[7].isdigit() else -1)
    return bool(owners) and owners == {os.getuid()}


def unload(entry: dict, port: int) -> bool:
    """Free the GPU memory of a runtime that stays up (its config entry's "unload" request)."""
    import http.client
    path, body = entry["unload"]["path"], entry["unload"]["body"]
    if not port or not own_listener(port):
        return False
    try:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        connection.request("POST", path, body=json.dumps(body), headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        response.read(64 * 1024)
        connection.close()
        return response.status < 400
    except (OSError, http.client.HTTPException):
        return False


# --- start / stop ---------------------------------------------------------------------------

def lms_binary() -> str | None:
    found = shutil.which("lms")
    if found:
        return found
    candidate = HOME / ".lmstudio/bin/lms"
    return str(candidate) if candidate.is_file() else None


def _own_processes(proc: str = "/proc") -> list[tuple[int, int, list[bytes]]]:
    """(pid, parent pid, argv) of this user's processes."""
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
        fields = _read(f"{proc}/{pid}/stat", 1024).rpartition(b")")[2].split()
        parent = int(fields[1]) if len(fields) > 1 and fields[1].isdigit() else 0
        found.append((int(pid), parent, _read(f"{proc}/{pid}/cmdline", 4096).split(b"\0")))
    return found


def _is_lmstudio(argv: list[bytes]) -> bool:
    """LM Studio's app (AppImage, its mount), its engines (~/.lmstudio/extensions/backends/
    …/llama-server) and helpers (~/.lmstudio/.internal/utils/node)."""
    exe = argv[0] if argv else b""
    if b"/.lmstudio/apps/" in exe:
        return False           # LM Studio's other apps (Bionic) keep their helpers there
    return b"/.lmstudio/" in exe or b"lm-studio" in os.path.basename(exe).lower() or b"/.mount_lm-stu" in exe


def _is_other_app(argv: list[bytes]) -> bool:
    """An LM Studio app that is not LM Studio (Bionic): it shares ~/.lmstudio and its engines."""
    exe = argv[0] if argv else b""
    return os.path.basename(exe).lower() == b"bionic" or b"/.lmstudio/apps/" in exe


def _other_app_family(processes) -> set:
    """Bionic and everything it started (its engines too): never LM Studio's to stop."""
    family = {pid for pid, _parent, argv in processes if _is_other_app(argv)}
    parents = {pid: parent for pid, parent, _argv in processes}
    for pid in parents:
        seen, current = 0, pid
        while current and seen < 64:
            if current in family:
                family.add(pid)
                break
            current, seen = parents.get(current, 0), seen + 1
    return family


def lmstudio_up(proc: str = "/proc") -> bool:
    """LM Studio (its app, daemon or an engine) runs already: we did not start it, so we leave it running."""
    processes = _own_processes(proc)
    others = _other_app_family(processes)
    return any(_is_lmstudio(argv) and pid not in others for pid, _parent, argv in processes)


def lmstudio_pids(proc: str = "/proc") -> list[int]:
    """Everything of the LM Studio we started: the hidden `lm-studio --run-as-service`,
    its children, and its engines even once they lost their parent (an engine holds
    the loaded model, so it must go for the GPU memory to be free). Nothing when an
    LM Studio window is open (an lm-studio main process without --run-as-service):
    then the user is using it."""
    processes = _own_processes(proc)
    for _pid, _parent, argv in processes:
        exe = os.path.basename(argv[0]).lower() if argv else b""
        if b"lm-studio" in exe and b"--run-as-service" not in argv and not any(a.startswith(b"--type=") for a in argv):
            return []
    others = _other_app_family(processes)
    picked = {pid for pid, _parent, argv in processes if _is_lmstudio(argv) and pid not in others}
    children = {}
    for pid, parent, _argv in processes:
        children.setdefault(parent, []).append(pid)
    todo = list(picked)
    while todo:
        for child in children.get(todo.pop(), []):
            if child not in picked and child not in others:
                picked.add(child)
                todo.append(child)
    return sorted(picked)


def _lms_role(pid: int, proc: str = "/proc") -> str:
    """"runtime": the AppImage's own process (lm-studio.AppImage), which serves the
    /tmp/.mount_lm-stu… mount every LM Studio program runs from; "engine": a model
    engine (~/.lmstudio/extensions/backends/…); "main": the lm-studio program itself;
    "outlives": helpers that keep running when the program ends (crash handler, node
    helper); "helper": its zygotes, GPU and utility processes, which end with it."""
    argv = _read(f"{proc}/{pid}/cmdline", 4096).split(b"\0")
    exe = argv[0] if argv else b""
    name = os.path.basename(exe).lower()
    if name.endswith(b".appimage"):
        return "runtime"
    if b"/.lmstudio/extensions/backends/" in exe:
        return "engine"
    if name == b"chrome_crashpad_handler" or (b"/.lmstudio/" in exe and b"lm-studio" not in name):
        return "outlives"      # not ended with the program: the crash handler, LM Studio's node helper
    if b"lm-studio" in name and not any(a.startswith(b"--type=") for a in argv):
        return "main"
    return "helper"


def _wait_gone(fds: list, seconds: float) -> list:
    """Wait until these processes have exited (or the time is up); the ones still running."""
    deadline = time.monotonic() + seconds
    while True:
        fds = [fd for fd in fds if not _exited(fd)]
        if not fds or time.monotonic() >= deadline:
            return fds
        time.sleep(0.2)


def _signal(fds: list, sig):
    for fd in fds:
        try:
            signal.pidfd_send_signal(fd, sig)
        except OSError:
            pass


def _end(fds: list, grace: float = 5.0):
    """SIGTERM, SIGKILL after `grace` seconds (LM Studio's main process ignores
    SIGTERM; SIGKILL never leaves a crash report), then wait until they are gone."""
    _signal(fds, signal.SIGTERM)
    fds = _wait_gone(fds, grace)
    if fds:
        _signal(fds, signal.SIGKILL)
        _wait_gone(fds, 3.0)


def stop_lmstudio_service(argv0: str):
    """After `lms server stop`: the background LM Studio we started goes too. Each
    process is held by a pidfd from the moment it is found, so a signal can never
    reach another process that got the same PID meanwhile.

    Order matters: LM Studio is an AppImage. Its programs run from a FUSE mount
    that the AppImage's own process serves; once that process is gone, any program
    still reading from the mount dies of SIGBUS (a crash report), and the program
    aborts (SIGTRAP) when a helper dies under it. So: engines first (SIGTERM),
    then the program and all its helpers at once with SIGKILL, the AppImage
    process last (it exits by itself and unmounts once its program is gone)."""
    run([argv0, "daemon", "down"])
    opened = {}
    for pid in lmstudio_pids():
        try:
            opened[pid] = os.pidfd_open(pid)
        except OSError:
            continue           # gone already
    # Look again with the pidfds held (they pin their process): only what is still
    # LM Studio's is signalled, so a PID reused before pidfd_open is never hit.
    still = set(lmstudio_pids())
    for pid in [pid for pid in opened if pid not in still]:
        os.close(opened.pop(pid))
    roles = {}
    for pid, fd in opened.items():
        roles.setdefault(_lms_role(pid), []).append(fd)
    try:
        # The engines hold the model and stop cleanly: SIGTERM first.
        _end(roles.get("engine", []))
        # The rest is LM Studio's program, its helpers and the AppImage process that
        # serves the mount they all run from. Ending them one by one crashes the
        # ones left (SIGBUS once the mount goes, SIGTRAP when a helper dies under
        # the program: crash reports). So all of them get SIGKILL at once, the
        # AppImage process last: every program already has its SIGKILL pending
        # when the mount goes, and SIGKILL never leaves a crash report. Its server
        # was stopped with `lms server stop` before, and this is the hidden LM
        # Studio the watcher started itself.
        program = roles.get("main", []) + roles.get("helper", []) + roles.get("outlives", [])
        _signal(program, signal.SIGKILL)
        _wait_gone(program, 3.0)
        _end(_wait_gone(roles.get("runtime", []), 2.0))
    finally:
        for fd in opened.values():
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
    if entry.get("control") == "lms" and lms_binary():
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
        if entry.get("stays_up"):
            ok = unload(entry, entry.get("port", 0))
            log(f"unload {key} ({why}): {'ok' if ok else 'not running'}")
            self.started.pop(key, None)
            self.started.pop(f"{key}:ok", None)
            return True
        argv = command(key, entry, "stop")
        if argv:
            ok = self.run(argv)
            log(f"stop {key} ({why}): {'ok' if ok else 'failed'}")
            if entry.get("control") == "lms" and not self.lms_was_up:
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
                if argv and self.runtimes[key].get("control") == "lms" and key not in self.needed:
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


UPDATED = 75           # exit code when the plugin was updated: systemd starts the new code (Restart=on-failure)


def source_stamp() -> tuple:
    try:
        info = os.stat(__file__)
        return info.st_mtime_ns, info.st_size, info.st_ino
    except OSError:
        return ()


def config_from(argv: list) -> Path:
    """--config <file> from the unit, else CONFIG."""
    if len(argv) >= 3 and argv[1] == "--config" and _UNIT_SAFE.fullmatch(argv[2]) and argv[2].startswith("/"):
        return Path(argv[2])
    return CONFIG


def main():
    watcher = Watcher(config=config_from(sys.argv))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    log("watching agents and their local LLMs")
    started = source_stamp()
    try:
        while True:
            try:
                watcher.tick()
            except Exception as error:      # keep watching; systemd would restart us anyway
                log(f"error: {error}")
            if source_stamp() != started:
                # The plugin was updated (new providers, fixes): what is needed keeps
                # running, the state is saved, and systemd starts the new watcher.
                log("plugin updated: restarting with the new version")
                sys.exit(UPDATED)
            time.sleep(INTERVAL)
    finally:
        watcher.shutdown()


if __name__ == "__main__":
    sys.exit(main())
