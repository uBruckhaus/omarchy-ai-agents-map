"""GPU memory and activity, as LLM Monitor shows them: for the map's header and
the local LLM boxes (which LLM holds how much GPU memory, which models are
loaded, whether the GPU is working).

- totals and activity: the discrete GPU's sysfs (mem_info_vram_used/total,
  gpu_busy_percent; AMD), else `nvidia-smi` (bounded);
- per runtime: DRM fdinfo of its processes and their children (drm-resident-vram,
  one entry per DRM client), so the number is what really sits on the GPU;
- loaded models: the runtimes' own APIs on loopback, only when the listening
  socket is this user's (never starts anything).

Blocking, ~10 ms; the popup runs it on a thread every few seconds while shown.
"""

import glob
import http.client
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import ai_core

HOME = Path.home()
GIB = 1024 ** 3
BUSY_PERCENT = 20          # GPU busy at least this much with a model loaded: generating
MODEL_VRAM = 256 * 1024 ** 2   # a local LLM holding this much GPU memory has its model there
MAX_REPLY = 1024 * 1024


@dataclass
class RuntimeUse:
    running: bool = False
    vram: int = 0
    models: dict = field(default_factory=dict)      # lower-case name -> loaded | loading | sleeping
    serving: bool = True     # its API server answers (False: LM Studio open without its server)


@dataclass
class Usage:
    vram_used: int = 0
    vram_total: int = 0
    busy: float | None = None          # percent
    temp: float | None = None          # °C
    power: float | None = None         # W
    runtimes: dict = field(default_factory=dict)    # key -> RuntimeUse

    @property
    def llm_vram(self) -> int:
        total = sum(r.vram for r in self.runtimes.values())
        return min(total, self.vram_used) if self.vram_used else total

    @property
    def other_vram(self) -> int:
        return max(0, self.vram_used - self.llm_vram)

    @property
    def loaded(self) -> bool:
        return any(r.vram > 512 * 1024 ** 2 or any(s in ("loaded", "loading") for s in r.models.values())
                   for r in self.runtimes.values())

    @property
    def generating(self) -> bool:
        return self.loaded and (self.busy or 0) >= BUSY_PERCENT


def _read(path, limit: int = 65536) -> str:
    try:
        with open(path, "rb") as handle:
            return handle.read(limit).decode("utf-8", "replace")
    except OSError:
        return ""


def discrete_gpu() -> str | None:
    """The card with the largest VRAM (skips the iGPU carve-out)."""
    best = None
    for total in glob.glob("/sys/class/drm/card*/device/mem_info_vram_total"):
        try:
            size = int(_read(total, 64) or 0)
        except ValueError:
            continue
        if not best or size > best[0]:
            best = (size, os.path.dirname(total))
    return best[1] if best else None


GPU_DIR = discrete_gpu()
HWMON = (glob.glob(f"{GPU_DIR}/hwmon/hwmon*") or [None])[0] if GPU_DIR else None
GPU_SLOT = (re.search(r"PCI_SLOT_NAME=(\S+)", _read(f"{GPU_DIR}/uevent")) or [None, ""])[1] if GPU_DIR else ""


def _number(path) -> float | None:
    try:
        return float(_read(path, 64).strip())
    except ValueError:
        return None


def busy_now() -> float | None:
    """The GPU's busy percent right now, when it is a cheap read (sysfs; None otherwise)."""
    return _number(f"{GPU_DIR}/gpu_busy_percent") if GPU_DIR else None


def gpu_totals() -> tuple[int, int, float | None]:
    """(used, total, busy percent)."""
    if GPU_DIR:
        return (int(_number(f"{GPU_DIR}/mem_info_vram_used") or 0), int(_number(f"{GPU_DIR}/mem_info_vram_total") or 0),
                _number(f"{GPU_DIR}/gpu_busy_percent"))
    out, ok = ai_core.read_bounded_output(["nvidia-smi", "--query-gpu=memory.used,memory.total,utilization.gpu",
                                           "--format=csv,noheader,nounits"], max_bytes=4096, timeout=2)
    if ok:
        try:
            used, total, busy = (float(v) for v in out.decode().splitlines()[0].split(","))
            return int(used * 1024 ** 2), int(total * 1024 ** 2), busy
        except (ValueError, IndexError):
            pass
    return 0, 0, None


def _kib(info: str, *keys) -> int:
    for key in keys:
        match = re.search(rf"^{re.escape(key)}:\s*(\d+)\s*KiB", info, re.MULTILINE)
        if match:
            return int(match.group(1)) * 1024
    return 0


def proc_vram(pid: int) -> int:
    """GPU memory really on the GPU for one process (drm-resident-vram), one entry per DRM client."""
    clients = {}
    try:
        if os.stat(f"/proc/{pid}").st_uid != os.getuid():
            return 0           # only this user's processes (a PID may have been reused)
        fds = os.listdir(f"/proc/{pid}/fd")
    except OSError:
        return 0
    for fd in fds[:4096]:
        try:
            if not os.readlink(f"/proc/{pid}/fd/{fd}").startswith("/dev/dri/"):
                continue
        except OSError:
            continue
        info = _read(f"/proc/{pid}/fdinfo/{fd}", 8192)
        client = re.search(r"drm-client-id:\s*(\d+)", info)
        if not client or (GPU_SLOT and "drm-pdev:" in info and f"drm-pdev:\t{GPU_SLOT}" not in info):
            continue          # another GPU (the iGPU) or no DRM client
        clients[client.group(1)] = _kib(info, "drm-resident-vram", "drm-memory-vram")
    return sum(clients.values())


def runtime_of(comm: str, argv: list) -> str:
    """Which local LLM a process belongs to ("" for none), as its provider's engine says."""
    import llm_engine
    return llm_engine.runtime_of(comm, argv)


def runtime_pids(processes) -> dict:
    """key -> pids: the runtime's own processes and all their children."""
    parent, roots = {}, {}
    for pid, comm, argv in processes:
        stat = _read(f"/proc/{pid}/stat", 1024)
        fields = stat.rpartition(")")[2].split()
        if len(fields) > 1 and fields[1].isdigit():
            parent[pid] = int(fields[1])
        key = runtime_of(comm, argv)
        if key:
            roots[pid] = key
    # A process belongs to the outermost local LLM above it: a model runner Ollama starts may be
    # named like llama.cpp's server, but it is Ollama's.
    groups = {}
    for pid in set(parent) | set(roots):
        seen, current, owner = 0, pid, ""
        while current and seen < 64:
            owner = roots.get(current, owner)
            current, seen = parent.get(current, 0), seen + 1
        if owner:
            groups.setdefault(owner, set()).add(pid)
    return groups


def _own_port(port: int) -> bool:
    import loopback
    owners = loopback.loopback_listeners(port)
    return bool(owners) and all(uid == os.getuid() for uid in owners)


def http_json(port: int, path: str, method: str = "GET", body: dict | None = None, timeout: float = 1.5):
    """A JSON reply from our own server on 127.0.0.1:port, or None."""
    if not port or not _own_port(port):
        return None
    try:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        data = json.dumps(body).encode() if body is not None else None
        connection.request(method, path, body=data, headers={"Content-Type": "application/json"} if data else {})
        response = connection.getresponse()
        raw = response.read(MAX_REPLY + 1)
        connection.close()
        if response.status >= 400 or len(raw) > MAX_REPLY:
            return None
        return json.loads(raw or b"null")
    except (OSError, ValueError, http.client.HTTPException):
        return None


def port_of(rt) -> int:
    port = (rt.address or "").rpartition(":")[2]
    return int(port) if port.isdigit() else 0


def loaded_models(rt) -> dict:
    """lower-case model name -> state, from the runtime's own API (its provider's engine)."""
    import llm_engine
    return llm_engine.of(rt).loaded_models(rt)


def snapshot(runtimes, processes=None) -> Usage:
    """runtimes: inventory.Runtime (running ones are asked for their models)."""
    if processes is None:
        import inventory
        processes = inventory.own_processes()
    used, total, busy = gpu_totals()
    usage = Usage(used, total, busy)
    if HWMON:
        temp, power = _number(f"{HWMON}/temp1_input"), _number(f"{HWMON}/power1_average")
        usage.temp = temp / 1000 if temp else None
        usage.power = power / 1e6 if power else None
    import llm_engine
    groups = runtime_pids(processes)
    for rt in runtimes:
        pids = groups.get(rt.key, set())
        use = RuntimeUse(running=bool(pids), vram=sum(proc_vram(pid) for pid in pids))
        if pids:
            use.models = loaded_models(rt)
            if llm_engine.of(rt).app_without_server:
                use.serving = _own_port(port_of(rt))
        usage.runtimes[rt.key] = use
    return usage


def gib(size: int) -> str:
    return f"{size / GIB:.1f}"
