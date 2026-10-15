"""The AI setup around the agents: local LLM runtimes and the models they
serve, MCP servers and skills, and which agent uses what.

Everything is read from the file system and /proc (this user's processes
only), size- and count-capped; nothing is started and no request is sent.
Only names, model ids, folders and local addresses are taken from config
files: never environment values, headers, tokens or keys.
"""

import os
import re
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import agent_details as ad

HOME = Path.home()
MAX_ITEMS = 500          # models, skills or servers listed per source
MAX_PROCS = 4000
MAX_CMDLINE = 64 * 1024
MAX_ENVIRON = 256 * 1024
NAME_RE = re.compile(r"[\w.@:+/ -]{1,120}")


@dataclass
class Runtime:
    key: str                 # "llamacpp", "ollama", "lmstudio", "vllm"
    name: str
    installed: bool = False
    running: bool = False
    service: str = ""        # the user service that starts it, if any
    address: str = ""        # host:port it serves on
    models_dir: str = ""
    models: list = field(default_factory=list)
    agents: list = field(default_factory=list)   # agents with a provider pointing at it


@dataclass
class McpServer:
    name: str
    kind: str = ""           # "command" or "url"
    target: str = ""         # command name / package, or the URL's host
    agents: list = field(default_factory=list)


@dataclass
class SkillSet:
    path: str                # where the skills really live (~ for home)
    skills: list = field(default_factory=list)
    agents: list = field(default_factory=list)


@dataclass
class Inventory:
    runtimes: list = field(default_factory=list)
    mcp: list = field(default_factory=list)
    skill_sets: list = field(default_factory=list)
    agent_mcp: dict = field(default_factory=dict)      # command -> [server names]
    agent_skills: dict = field(default_factory=dict)   # command -> [skill set paths]
    agent_tools: dict = field(default_factory=dict)    # command -> [built-in tool names], where configured


def tilde(path) -> str:
    text = str(path)
    home = str(HOME)
    return "~" + text[len(home):] if text == home or text.startswith(home + "/") else text


def clean(name) -> str:
    return name.strip()[:120] if isinstance(name, str) and NAME_RE.fullmatch(name.strip() or "-") else ""


# --- processes ---------------------------------------------------------------------

def _read_bytes(path: Path, limit: int) -> bytes:
    try:
        with open(path, "rb") as handle:
            return handle.read(limit)
    except OSError:
        return b""


def own_processes() -> list[tuple[int, str, list[str]]]:
    """(pid, comm, argv) of this user's processes."""
    found, uid = [], os.getuid()
    try:
        pids = [int(p) for p in os.listdir("/proc") if p.isdigit()][:MAX_PROCS]
    except OSError:
        return found
    for pid in pids:
        try:
            if os.stat(f"/proc/{pid}").st_uid != uid:
                continue
        except OSError:
            continue
        comm = _read_bytes(Path(f"/proc/{pid}/comm"), 64).decode("utf-8", "replace").strip()
        argv = [a.decode("utf-8", "replace") for a in _read_bytes(Path(f"/proc/{pid}/cmdline"), MAX_CMDLINE).split(b"\0") if a]
        found.append((pid, comm, argv))
    return found


def process_env(pid: int, names: tuple[str, ...]) -> dict:
    """Only the named variables from a process of this user."""
    values = {}
    for item in _read_bytes(Path(f"/proc/{pid}/environ"), MAX_ENVIRON).split(b"\0"):
        key, _sep, value = item.partition(b"=")
        name = key.decode("utf-8", "replace")
        if name in names:
            values[name] = value.decode("utf-8", "replace")[:4096]
    return values


def arg_value(argv: list[str], *flags: str) -> str:
    for i, arg in enumerate(argv):
        for flag in flags:
            if arg == flag and i + 1 < len(argv):
                return argv[i + 1]
            if arg.startswith(flag + "="):
                return arg.split("=", 1)[1]
    return ""


# --- user services -------------------------------------------------------------------

def unit_files() -> list[Path]:
    root = HOME / ".config/systemd/user"
    try:
        return sorted(p for p in root.glob("*.service") if p.is_file())[:200]
    except OSError:
        return []


def unit_for(binary: str) -> tuple[str, dict]:
    """The user service whose ExecStart runs `binary`, and the variables its
    Environment= and EnvironmentFile= lines set (only names we ask for later)."""
    for unit in unit_files():
        text = ad._read(unit) or ""
        drop_in = unit.with_name(unit.name + ".d")
        try:
            for extra in sorted(drop_in.glob("*.conf"))[:20]:
                text += "\n" + (ad._read(extra) or "")
        except OSError:
            pass
        exec_lines = re.findall(r"^ExecStart=\S*?([^/\s]+)(?:\s|$)", text, re.MULTILINE)
        if binary not in exec_lines:
            continue
        env = {}
        for line in re.findall(r"^EnvironmentFile=-?(.+)$", text, re.MULTILINE)[:10]:
            path = Path(os.path.expanduser(line.strip().replace("%h", str(HOME))))
            for key, value in re.findall(r"^\s*(?:export\s+)?([A-Z_][A-Z0-9_]*)=(.*)$", ad._read(path) or "", re.MULTILINE):
                env[key] = value.strip().strip("'\"")
        for line in re.findall(r"^Environment=(.+)$", text, re.MULTILINE)[:20]:
            for key, value in re.findall(r'"?([A-Z_][A-Z0-9_]*)=([^"\s]*)"?', line):
                env[key] = value
        return unit.stem, env
    return "", {}


# --- runtimes -------------------------------------------------------------------------

def gguf_names(folder: Path) -> list[str]:
    """Model names in a folder of GGUF files (or symlinks to them), one level deep."""
    names = []
    try:
        entries = sorted(folder.iterdir())[:MAX_ITEMS * 2]
    except OSError:
        return names
    for entry in entries:
        name = entry.name
        if name.lower().endswith(".gguf") and not name.lower().startswith("mmproj"):
            names.append(name[:-5])
        elif entry.is_dir() and not entry.name.startswith("."):
            try:
                inner = [p.name for p in list(entry.iterdir())[:50] if p.name.lower().endswith(".gguf")
                         and not p.name.lower().startswith("mmproj")]
            except OSError:
                inner = []
            names.extend(n[:-5] for n in inner)
    return list(dict.fromkeys(names))[:MAX_ITEMS]


def llama_presets() -> list[str]:
    """Model sections of llama.cpp's router presets (~/.config/llama.cpp/models.ini)."""
    text = ad._read(HOME / ".config/llama.cpp/models.ini") or ""
    return [s for s in re.findall(r"^\[([^\]\n]{1,120})\]", text, re.MULTILINE) if s != "*"][:MAX_ITEMS]


def llama_context() -> dict:
    """Context size per llama.cpp router preset (ctx-size in models.ini)."""
    text = ad._read(HOME / ".config/llama.cpp/models.ini") or ""
    sizes = {}
    for name, body in re.findall(r"^\[([^\]\n]{1,120})\]\n((?:(?!\[).*\n?)*)", text, re.MULTILINE):
        match = re.search(r"^\s*ctx-size\s*=\s*(\d{1,8})", body, re.MULTILINE)
        if match and name != "*":
            sizes[name] = int(match.group(1))
    return sizes


def llamacpp(processes) -> Runtime:
    rt = Runtime("llamacpp", "llama.cpp", installed=bool(shutil.which("llama-server")))
    rt.service, env = unit_for("llama-server")
    models, folder = [], ""
    for pid, comm, argv in processes:
        if comm != "llama-server":
            continue
        rt.running = rt.installed = True
        penv = process_env(pid, ("LLAMA_ARG_MODELS_DIR", "LLAMA_MODELS_DIR", "LLAMA_ARG_MODEL",
                                 "LLAMA_ARG_HOST", "LLAMA_ARG_PORT", "LLAMA_PORT", "LLAMA_HOST"))
        env = {**env, **penv}
        model = arg_value(argv, "-m", "--model")
        if model:
            models.append(Path(model).name.removesuffix(".gguf"))
        folder = arg_value(argv, "--models-dir") or folder
        host = arg_value(argv, "--host") or env.get("LLAMA_ARG_HOST") or env.get("LLAMA_HOST") or "127.0.0.1"
        port = arg_value(argv, "--port") or env.get("LLAMA_ARG_PORT") or env.get("LLAMA_PORT") or "8080"
        rt.address = f"{host}:{port}"
    folder = folder or env.get("LLAMA_ARG_MODELS_DIR") or env.get("LLAMA_MODELS_DIR") or ""
    if not rt.address and (env.get("LLAMA_PORT") or env.get("LLAMA_ARG_PORT")):
        rt.address = f"{env.get('LLAMA_HOST') or env.get('LLAMA_ARG_HOST') or '127.0.0.1'}:" \
                     f"{env.get('LLAMA_PORT') or env.get('LLAMA_ARG_PORT')}"
    if folder:
        rt.models_dir = tilde(folder)
        models += gguf_names(Path(os.path.expanduser(folder)))
    models += llama_presets()
    models += gguf_names(HOME / ".cache/llama.cpp")
    rt.models = dedupe_models(models)
    rt.installed = rt.installed or bool(rt.service)
    return rt


def dedupe_models(names: list[str]) -> list[str]:
    """One entry per model, whatever the spelling ("Qwen3.8-27B-Q4_K_M" and a
    preset "qwen3.8-27b-q4_k_m" are the same)."""
    seen, out = set(), []
    for name in names:
        name = clean(name)
        key = name.lower()
        if name and key not in seen:
            seen.add(key)
            out.append(name)
    return out[:MAX_ITEMS]


def ollama_models(root: Path) -> list[str]:
    """name:tag for every manifest (registry.ollama.ai/library/<name>/<tag>; other
    registries keep their path)."""
    manifests = root / "manifests"
    names = []
    try:
        hosts = sorted(manifests.iterdir())[:20]
    except OSError:
        return names
    for host in hosts:
        try:
            for owner in sorted(host.iterdir())[:200]:
                for model in sorted(owner.iterdir())[:MAX_ITEMS]:
                    for tag in sorted(model.iterdir())[:50]:
                        prefix = "" if host.name == "registry.ollama.ai" and owner.name == "library" \
                            else f"{owner.name}/" if host.name == "registry.ollama.ai" else f"{host.name}/{owner.name}/"
                        names.append(f"{prefix}{model.name}:{tag.name}")
                        if len(names) >= MAX_ITEMS:
                            return names
        except OSError:
            continue
    return names


def ollama(processes) -> Runtime:
    rt = Runtime("ollama", "Ollama", installed=bool(shutil.which("ollama")))
    rt.service, env = unit_for("ollama")
    for pid, comm, argv in processes:
        if comm == "ollama" and "serve" in argv[1:3]:
            rt.running = rt.installed = True
            env = {**env, **process_env(pid, ("OLLAMA_MODELS", "OLLAMA_HOST"))}
    rt.address = env.get("OLLAMA_HOST") or "127.0.0.1:11434"
    folder = env.get("OLLAMA_MODELS") or ""
    candidates = [Path(os.path.expanduser(folder))] if folder else []
    candidates += [HOME / ".ollama/models", Path("/var/lib/ollama/.ollama/models"), Path("/var/lib/ollama")]
    for path in candidates:
        if (path / "manifests").is_dir():
            rt.models_dir = tilde(path)
            rt.models = ollama_models(path)
            break
    rt.installed = rt.installed or bool(rt.service)
    return rt


def has_model_files(folder: Path) -> bool:
    try:
        return any(p.suffix.lower() in (".gguf", ".safetensors") for p in list(folder.iterdir())[:200])
    except OSError:
        return False


def lmstudio(processes) -> Runtime:
    root = HOME / ".lmstudio"
    rt = Runtime("lmstudio", "LM Studio", installed=root.is_dir() or bool(shutil.which("lms")),
                 address="127.0.0.1:1234")
    for _pid, comm, argv in processes:
        exe = Path(argv[0]).name.lower() if argv else ""
        if comm.lower().startswith("lm studio") or comm == "lm-studio" or exe in ("lm-studio", "lm studio"):
            rt.running = True
    settings = ad.read_json(root / "settings.json")
    folder = settings.get("downloadsFolder") if isinstance(settings.get("downloadsFolder"), str) else ""
    path = Path(os.path.expanduser(folder)) if folder else root / "models"
    names = []
    try:
        for publisher in sorted(path.iterdir())[:200]:
            if publisher.is_dir() and not publisher.name.startswith("."):
                for repo in sorted(publisher.iterdir())[:MAX_ITEMS]:
                    # Only folders with model files: the downloads folder may be
                    # shared with Ollama's or llama.cpp's own.
                    if repo.is_dir() and has_model_files(repo):
                        names.append(repo.name)
    except OSError:
        pass
    if names:
        rt.models_dir = tilde(path)
    rt.models = dedupe_models(names)
    return rt


def vllm(processes) -> Runtime:
    rt = Runtime("vllm", "vLLM", installed=bool(shutil.which("vllm")), address="127.0.0.1:8000")
    for _pid, _comm, argv in processes:
        if len(argv) > 2 and Path(argv[0]).name.startswith(("vllm", "python")) and "serve" in argv[1:3] \
                and any(Path(a).name == "vllm" for a in argv[:2]):
            rt.running = rt.installed = True
            index = argv.index("serve")
            if index + 1 < len(argv) and not argv[index + 1].startswith("-"):
                rt.models = [clean(argv[index + 1])]
            port = arg_value(argv, "--port")
            if port:
                rt.address = f"127.0.0.1:{port}"
    return rt


# --- MCP servers -----------------------------------------------------------------------

LAUNCHERS = {"npx", "bunx", "uvx", "pnpx", "pipx", "dlx", "docker", "podman", "node", "python", "python3", "uv", "bun"}


def server_target(entry: dict) -> tuple[str, str]:
    """("command", what runs) or ("url", host); never env, headers or args with values."""
    url = entry.get("url") or entry.get("httpUrl") or entry.get("serverUrl")
    if isinstance(url, str) and url:
        try:
            return "url", urlparse(url).hostname or ""
        except ValueError:
            return "url", ""
    command = entry.get("command")
    args = entry.get("args") if isinstance(entry.get("args"), list) else []
    if isinstance(command, list):           # opencode: "command": ["npx", "-y", "pkg"]
        command, args = (command[0] if command else ""), command[1:]
    if not isinstance(command, str) or not command:
        return "", ""
    name = Path(command).name
    if name in LAUNCHERS:
        for arg in args[:10]:
            if isinstance(arg, str) and arg and not arg.startswith("-") and "=" not in arg:
                return "command", f"{name} {Path(arg).name if arg.startswith('/') else arg}"[:80]
    return "command", name[:80]


def servers_in(mapping) -> list[tuple[str, str, str]]:
    found = []
    if isinstance(mapping, dict):
        for name, entry in list(mapping.items())[:100]:
            if isinstance(entry, dict) and clean(name):
                if entry.get("disabled") is True or entry.get("enabled") is False:
                    continue
                kind, target = server_target(entry)
                found.append((clean(name), kind, target))
    return found


def mcp_for(command: str) -> list[tuple[str, str, str]]:
    j = ad.read_json
    x = lambda p: Path(os.path.expanduser(p))   # noqa: E731
    if command == "claude":
        return servers_in(read_big_json(HOME / ".claude.json").get("mcpServers"))
    if command == "codex":
        return servers_in(ad.read_toml(x("~/.codex/config.toml")).get("mcp_servers"))
    if command in ("pi", "omp"):
        return servers_in(j(x(f"~/.{'pi' if command == 'pi' else 'omp'}/agent/mcp.json")).get("mcpServers"))
    if command == "copilot":
        return servers_in(j(x("~/.copilot/mcp-config.json")).get("mcpServers"))
    if command == "cursor-agent":
        return servers_in(j(x("~/.cursor/mcp.json")).get("mcpServers"))
    if command == "gemini":
        return servers_in(j(x("~/.gemini/settings.json")).get("mcpServers"))
    if command == "opencode":
        root = x("~/.config/opencode")
        return servers_in((j(root / "opencode.json") or j(root / "opencode.jsonc")).get("mcp"))
    if command == "crush":
        return servers_in(j(x("~/.config/crush/crush.json")).get("mcp"))
    if command == "hermes":
        text = ad._read(x("~/.hermes/config.yaml")) or ""
        block = re.search(r"^mcp_servers:[ \t]*\n((?:[ \t]+.*\n?|\n)+)", text, re.MULTILINE)
        names = re.findall(r"^  ([A-Za-z0-9_.-]{1,64}):", block.group(1), re.MULTILINE) if block else []
        return [(n, "", "") for n in names[:100]]
    return []


def read_big_json(path: Path) -> dict:
    """~/.claude.json grows with projects: allow more than the usual cap."""
    try:
        if path.stat().st_size > 8 * 1024 * 1024:
            return {}
        import json
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# --- skills -----------------------------------------------------------------------------

SKILL_DIRS = {
    "claude": ["~/.claude/skills"], "codex": ["~/.codex/skills"], "copilot": ["~/.copilot/skills"],
    "cursor-agent": ["~/.cursor/skills"], "opencode": ["~/.config/opencode/skills"], "pi": ["~/.pi/agent/skills"],
    "omp": ["~/.omp/agent/skills"], "hermes": ["~/.hermes/skills"], "gemini": ["~/.gemini/skills"],
    "crush": ["~/.config/crush/skills"],
}


def skill_dirs(command: str) -> list[Path]:
    dirs = [Path(os.path.expanduser(p)) for p in SKILL_DIRS.get(command, [])]
    if command in ("pi", "omp"):
        settings = ad.read_json(Path(os.path.expanduser(f"~/.{command}/agent/settings.json")))
        extra = settings.get("skills")
        if isinstance(extra, list):
            dirs += [Path(os.path.expanduser(p)) for p in extra[:20] if isinstance(p, str) and p]
    return dirs


def skills_in(folder: Path, depth: int = 0) -> list[tuple[Path, Path]]:
    """(skill folder, the set it belongs to) for every folder holding a
    SKILL.md. A linked skill belongs to where it really lives; Hermes's
    category folders count as one set."""
    found = []
    try:
        entries = sorted(folder.iterdir())[:MAX_ITEMS]
    except OSError:
        return found
    for entry in entries:
        if entry.name.startswith(".") or not entry.is_dir():
            continue
        if (entry / "SKILL.md").is_file():
            try:
                home = entry.resolve().parent if entry.is_symlink() else folder
            except OSError:
                continue
            found.append((entry, home))
        elif depth == 0:
            found.extend((skill, home if skill.is_symlink() else folder)
                         for skill, home in skills_in(entry, 1))
    return found[:MAX_ITEMS]


# --- all together -----------------------------------------------------------------------

def collect(agent_list) -> Inventory:
    inv = Inventory()
    processes = own_processes()
    inv.runtimes = [llamacpp(processes), ollama(processes), lmstudio(processes), vllm(processes)]
    by_port = {}
    for rt in inv.runtimes:
        port = rt.address.rsplit(":", 1)[-1]
        if port.isdigit():
            by_port[int(port)] = rt

    servers: dict[str, McpServer] = {}
    sets: dict[str, SkillSet] = {}
    for agent in agent_list:
        if not agent.installed:
            continue
        command = agent.command
        # Local runtimes this agent talks to (its providers' addresses).
        info = ad.details_for(command)
        for provider in info.providers:
            if provider.local:
                for rt in inv.runtimes:
                    if rt.name == provider.runtime and command not in rt.agents:
                        rt.agents.append(command)
        names = []
        for name, kind, target in mcp_for(command):
            server = servers.setdefault(name, McpServer(name, kind, target))
            if command not in server.agents:
                server.agents.append(command)
            names.append(name)
        inv.agent_mcp[command] = names
        paths = []
        for folder in skill_dirs(command):
            for skill, root in skills_in(folder):
                key = tilde(root)
                entry = sets.setdefault(key, SkillSet(key))
                if skill.name not in entry.skills:
                    entry.skills.append(skill.name)
                if command not in entry.agents:
                    entry.agents.append(command)
                if key not in paths:
                    paths.append(key)
        inv.agent_skills[command] = paths
        tools = ad.read_json(Path(os.path.expanduser(f"~/.{command}/agent/settings.json"))).get("defaultTools") \
            if command in ("pi", "omp") else None
        if isinstance(tools, list):
            inv.agent_tools[command] = [clean(t) for t in tools[:50] if clean(t)]
    # LM Studio has MCP servers of its own (its chat uses them).
    for name, kind, target in servers_in(ad.read_json(HOME / ".lmstudio/mcp.json").get("mcpServers")):
        server = servers.setdefault(name, McpServer(name, kind, target))
        if "lmstudio" not in server.agents:
            server.agents.append("lmstudio")
    inv.mcp = sorted(servers.values(), key=lambda s: s.name.lower())
    inv.skill_sets = sorted(sets.values(), key=lambda s: (-len(s.agents), s.path))
    for skill_set in inv.skill_sets:
        skill_set.skills.sort(key=str.lower)
    return inv
