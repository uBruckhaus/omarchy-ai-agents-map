"""Disk space: how much room the agents, the local LLMs and the models take, and where.

For each installed agent: its program folder (mise's tool folder with every version
kept, the app's own version folder, or the file a package installed) and its data
folder (settings, sessions, history). For each installed local LLM: its program, its
own data, and its models folder. Sizes are space on disk (blocks), each file counted
once even when two places hold it (hard links) or one folder lies inside another;
symbolic links are not followed. Nothing is changed; walking stops after
MAX_ENTRIES entries per place or DEADLINE seconds in all (the size is then shown as
"at least").
"""

import os
import re
import shutil
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path

import agent_details
import agents as agents_mod

HOME = Path.home()
MAX_ENTRIES = 400_000
DEADLINE = 60.0
MAX_SCRIPT = 16 * 1024

# More data folders than agent_details.CONFIG_DIRS names.
AGENT_DATA = {"opencode": ["~/.local/share/opencode"], "crush": ["~/.local/share/crush"],
              "cursor-agent": ["~/.config/cursor"], "claude": ["~/.claude.json"], "pi": ["~/.pi"]}
# Program folders inside a data folder (measured as the program, not twice).
AGENT_PROGRAM = {"hermes": "~/.hermes/hermes-agent"}
SYSTEM_DIRS = {Path("/"), Path("/usr"), Path("/usr/local"), Path("/opt"), HOME, HOME / ".local"}


@dataclass
class Place:
    kind: str                 # program, data, models
    path: Path
    size: int = 0
    files: int = 0
    partial: bool = False     # stopped early: at least this much
    missing: bool = False
    links: int = 0            # symbolic links in it (a folder of links to models elsewhere)


@dataclass
class Item:
    group: str                # agent, runtime, models
    key: str
    name: str
    places: list = field(default_factory=list)
    users: list = field(default_factory=list)    # models folder: the local LLMs using it
    versions: int = 0         # program folders with several versions kept

    def total(self) -> int:
        return sum(p.size for p in self.places)

    def partial(self) -> bool:
        return any(p.partial for p in self.places)


@dataclass
class Disk:
    mount: str
    free: int
    size: int
    ai: int = 0               # what the items on it take


def _expand(path: str) -> Path:
    return Path(os.path.expanduser(path))


def program_folder(binary: Path) -> Path:
    """The folder a program lives in: past bin/, and past a version folder next to a
    `current` link (installs that keep versions)."""
    path = Path(os.path.realpath(binary))
    folder = path.parent
    if folder.name == "bin":
        folder = folder.parent
    if (folder.parent / "current").exists() and folder.parent not in SYSTEM_DIRS:
        folder = folder.parent
    return path if folder in SYSTEM_DIRS or str(folder).startswith(("/usr/", "/bin")) else folder


def script_target(path: Path) -> Path | None:
    """A small wrapper script's program: the first absolute path in it that is an
    executable file (e.g. binary=/mnt/ai/llama.cpp-rocm/current/llama-server)."""
    try:
        if path.stat().st_size > MAX_SCRIPT:
            return None
        with open(path, "rb") as handle:
            head = handle.read(MAX_SCRIPT)
    except OSError:
        return None
    if not head.startswith(b"#!"):
        return None
    for match in re.finditer(r"(/[A-Za-z0-9._+/-]{2,400})", head.decode("utf-8", "replace").split("\n", 1)[-1]):
        candidate = Path(match.group(1))
        if candidate.is_file() and os.access(candidate, os.X_OK) and not candidate.name.endswith(".so"):
            return candidate
    return None


def locate(command: str) -> Path | None:
    found = shutil.which(command)
    if not found:
        return None
    path = Path(found)
    target = script_target(Path(os.path.realpath(path)))
    return program_folder(target or path)


def agent_items(agent_list) -> list:
    import install_ctl
    items = []
    for agent in agent_list:
        if not agent.installed:
            continue
        item = Item("agent", agent.command, agent.name)
        program = None
        if agent.command in AGENT_PROGRAM and _expand(AGENT_PROGRAM[agent.command]).is_dir():
            program = _expand(AGENT_PROGRAM[agent.command])
        elif agent.command in install_ctl.AGENT_INSTALLERS:
            program = install_ctl.AGENT_INSTALLERS[agent.command].installed_folder()
        elif agent.source == "mise" or agent.command not in ("openclaw", "hermes"):
            spec = agents_mod.wrapper_spec(agent.command) or agents_mod.SPECS.get(agent.command, agent.command)
            program = agents_mod.mise_install(spec)
            if program is not None:
                try:
                    item.versions = sum(1 for p in program.iterdir() if p.is_dir() and not p.is_symlink())
                except OSError:
                    pass
        if program is None and agent.source != "mise":
            program = locate(agent.command)
        if program is not None:
            item.places.append(Place("program", program))
        data = [agent_details.CONFIG_DIRS.get(agent.command)] + AGENT_DATA.get(agent.command, [])
        paths = [_expand(d) for d in dict.fromkeys(d for d in data if d) if _expand(d).exists()]
        for path in paths:
            if not any(other != path and path.is_relative_to(other) for other in paths):
                item.places.append(Place("data", path))
        items.append(item)
    return items


def runtime_items(runtimes) -> list:
    import install_ctl
    items, folders = [], {}
    for rt in runtimes:
        if not rt.installed:
            continue
        item = Item("runtime", rt.key, rt.name)
        inst = install_ctl.INSTALLERS.get(rt.key)
        program = inst.installed_folder() if inst is not None else None
        if program is None and inst is not None:
            program = locate(inst.command)
        if program is not None:
            item.places.append(Place("program", program))
        # Data a local LLM keeps besides its models (its models folder is measured on its own).
        import llm_engine
        for data in llm_engine.of(rt).data_folders(rt)[:4]:
            if data and _expand(data).exists():
                item.places.append(Place("data", _expand(data)))
        items.append(item)
        if rt.models_dir:
            folders.setdefault(rt.models_dir, []).append(rt.name)
    for folder, users in folders.items():
        path = _expand(folder)
        if path.exists():
            items.append(Item("models", folder, folder, [Place("models", path)], users))
    return items


def plan(agent_list, inventory) -> list:
    return agent_items(agent_list) + runtime_items(inventory.runtimes)


def _walk(place: Place, seen: set, stop_at: float):
    """Size of place.path into place (files once each across all places)."""
    try:
        info = os.lstat(place.path)
    except OSError:
        place.missing = True
        return
    stack, count = [place.path], 0
    if not stat.S_ISDIR(info.st_mode):
        if (info.st_dev, info.st_ino) not in seen:
            seen.add((info.st_dev, info.st_ino))
            place.size, place.files = info.st_blocks * 512, 1
        return
    while stack:
        folder = stack.pop()
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    count += 1
                    if count > MAX_ENTRIES or (count % 2000 == 0 and time.monotonic() > stop_at):
                        place.partial = True
                        return
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    key = (st.st_dev, st.st_ino)
                    if key in seen:
                        continue
                    seen.add(key)
                    if stat.S_ISDIR(st.st_mode):
                        stack.append(entry.path)
                    elif stat.S_ISLNK(st.st_mode):
                        place.links += 1
                    else:
                        place.size += st.st_blocks * 512
                        place.files += 1
        except OSError:
            continue


def mount_of(path: Path) -> Path:
    path = Path(os.path.realpath(path))
    try:
        dev = os.stat(path).st_dev
    except OSError:
        return path
    while path.parent != path:
        try:
            if os.stat(path.parent).st_dev != dev:
                break
        except OSError:
            break
        path = path.parent
    return path


def measure(items, deadline: float = DEADLINE) -> list:
    """Fill in every place's size (models first: the biggest, and a runtime's data
    folder often holds them). Returns the disks the places are on."""
    stop_at = time.monotonic() + deadline
    seen = set()
    order = sorted((p for item in items for p in item.places),
                   key=lambda p: {"models": 0, "program": 1, "data": 2}[p.kind])
    for place in order:
        _walk(place, seen, stop_at)
    disks = {}
    for place in order:
        if place.missing:
            continue
        mount = mount_of(place.path)
        try:
            fs = os.statvfs(mount)
        except OSError:
            continue
        key = (fs.f_blocks, fs.f_frsize)     # one file system mounted twice (btrfs subvolumes) is one disk
        disk = disks.get(key)
        if disk is None:
            disk = disks[key] = Disk(str(mount), fs.f_bavail * fs.f_frsize, fs.f_blocks * fs.f_frsize)
        elif str(mount) not in disk.mount.split(", "):
            disk.mount = ", ".join(sorted(disk.mount.split(", ") + [str(mount)], key=len))
        disk.ai += place.size
    return sorted(disks.values(), key=lambda d: -d.ai)


def totals(items) -> dict:
    """Sums for the bar: agent programs, agent data, local LLM programs and data, models."""
    sums = {"agents": 0, "agent_data": 0, "runtimes": 0, "models": 0}
    for item in items:
        for place in item.places:
            if item.group == "agent":
                sums["agents" if place.kind == "program" else "agent_data"] += place.size
            elif item.group == "runtime":
                sums["runtimes"] += place.size
            else:
                sums["models"] += place.size
    return sums
