"""Find programs on this PC that offer an MCP server, without running them.

Where it looks:
- the user's own programs: ~/.local/bin, ~/bin, ~/Applications/<app>/
  (extracted AppImages: usr/bin), /opt/*/bin;
- AppImages in ~/Applications, ~/.local/bin and ~/Downloads: only their
  usr/bin is unpacked (the AppImage's own --appimage-extract, a fraction of
  a second) into ~/.cache/ai-agents-map/appimages, kept while the file is
  unchanged;
- anything on PATH with "mcp" in its name.

A program counts when its file holds the MCP protocol's own words (two of
MARKERS). Agents and other MCP clients have them too, so their programs are
left out (CLIENTS, the agent catalog). How to start it comes from its own
usage text, also read from the file: "<cli> mcp [...]" means the `mcp`
subcommand; options for read/write folders get a folder that fits the app.
Programs already set up for an agent are marked.
"""

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

HOME = Path.home()
CACHE = HOME / ".cache/ai-agents-map/appimages"
OPT = Path("/opt")
MARKERS = (b"notifications/initialized", b"protocolVersion", b"tools/list", b"modelcontextprotocol")
CLIENTS = {"claude", "codex", "copilot", "crush", "cursor-agent", "gemini", "grok", "hermes", "muse", "omp",
           "openclaw", "opencode", "pi", "lms", "lmstudio", "ollama", "llama-server", "node", "bun", "deno",
           "python", "python3", "uv", "uvx", "npx", "npm", "docker", "podman", "code", "cursor", "zed",
           "ai-agents-map-toggle", "chromium", "chrome", "firefox", "electron"}
MAX_BYTES = 400 * 1024 * 1024
CHUNK = 8 * 1024 * 1024
FOLDERS = ((r"photo|image|pic|paint|draw|vector|design|fig|light", "Pictures"),
           (r"film|video|movie|effect", "Videos"), (r"sound|audio|music", "Music"))


@dataclass
class Candidate:
    name: str                 # suggested server name ("wordcraft")
    program: str              # the program found
    command: list             # how to start the server (program + args)
    usage: str = ""           # its usage line for MCP, if the file has one
    appimage: str = ""        # the AppImage it lives in (to unpack on adding)
    configured: list = field(default_factory=list)   # agents that have it already
    sure: bool = True         # the usage text names an mcp mode


def _is_elf_or_script(path: Path) -> bool:
    try:
        with open(path, "rb") as handle:
            head = handle.read(4)
    except OSError:
        return False
    return head == b"\x7fELF" or head[:2] == b"#!"


def scan_file(path: Path) -> tuple[int, bytes]:
    """(how many MARKERS, the MCP usage block or b"") from the file's bytes.
    Kept per file (size, time) in SCAN_CACHE: these programs are large."""
    try:
        info = path.stat()
    except OSError:
        return 0, b""
    key = f"{path}|{info.st_size}|{int(info.st_mtime)}"
    if key in _cache():
        count, usage = _cache()[key]
        return count, usage.encode("utf-8", "replace")
    if info.st_size > MAX_BYTES or info.st_size == 0:
        return 0, b""
    import mmap
    try:
        with open(path, "rb") as handle, mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as data:
            count = sum(1 for marker in MARKERS if data.find(marker) >= 0)
            usage = b""
            hit = re.search(rb"[\w.-]{1,64} mcp +\[", data)
            if hit:
                block = data[hit.start():hit.start() + 900]
                # The mcp usage: its first line and the indented lines after it.
                lines = block.split(b"\n")
                kept = [lines[0]]
                for line in lines[1:]:
                    if not line.startswith(b"   ") or re.match(rb"\s+[\w.-]+ \w", line) and b"--" not in line:
                        break
                    kept.append(line.strip())
                usage = b" ".join(kept).split(b"\x00")[0][:600]
            if not usage and data.find(b"MCP server") >= 0 and "mcp" in path.name.lower():
                count = max(count, 1)
    except (OSError, ValueError):
        return 0, b""
    _cache()[key] = (count, usage.decode("utf-8", "replace"))
    return count, usage


SCAN_CACHE = CACHE.parent / "mcp-scan-2.json"
_CACHE = None


def _cache() -> dict:
    global _CACHE
    if _CACHE is None:
        import json
        try:
            data = json.loads(SCAN_CACHE.read_text(encoding="utf-8"))
            _CACHE = {k: tuple(v) for k, v in data.items()} if isinstance(data, dict) else {}
        except (OSError, ValueError):
            _CACHE = {}
    return _CACHE


def _save_cache():
    import json
    if _CACHE is None:
        return
    try:
        SCAN_CACHE.parent.mkdir(parents=True, exist_ok=True)
        SCAN_CACHE.write_text(json.dumps(dict(list(_CACHE.items())[-2000:])), encoding="utf-8")
    except OSError:
        pass


def folder_for(name: str) -> str:
    for pattern, folder in FOLDERS:
        if re.search(pattern, name, re.IGNORECASE):
            return str(HOME / folder)
    return str(HOME / "Documents")


def command_for(program: Path, usage: str) -> tuple[list, bool]:
    """The start command from the usage text ("x-cli mcp [--automation-read-root DIR] …")."""
    if "mcp" in program.name.lower() and " mcp" not in usage:
        return [str(program)], True
    if not usage:
        return [str(program), "mcp"], False
    command = [str(program), "mcp"]
    folder = folder_for(program.name)
    for option in re.findall(r"--(?:[\w-]*(?:read|write)[\w-]*-)?root\b", usage):
        if option not in command:
            command += [option, folder]
    return command, True


def server_name(program: Path) -> str:
    name = re.sub(r"(-cli|-mcp|-mcp-server|-server)$", "", program.name.lower())
    name = re.sub(r"^mcp-server-|^mcp-", "", name)
    return re.sub(r"[^a-z0-9_.-]", "-", name)[:64] or "server"


# --- where to look -------------------------------------------------------------------------------

def _executables(folder: Path, depth: int = 0) -> list[Path]:
    found = []
    try:
        entries = sorted(folder.iterdir())[:500]
    except OSError:
        return found
    for entry in entries:
        try:
            if entry.is_file() and os.access(entry, os.X_OK):
                found.append(entry)
            elif depth and entry.is_dir() and not entry.is_symlink():
                found += _executables(entry, depth - 1)
        except OSError:
            continue
    return found


def own_programs() -> list[Path]:
    places = [HOME / ".local/bin", HOME / "bin"]
    for root in (HOME / "Applications", OPT):
        try:
            apps = sorted(p for p in root.iterdir() if p.is_dir())[:100]
        except OSError:
            continue
        for app in apps:
            places += [app / "usr/bin", app / "bin", app]
    found = []
    for place in places:
        found += [p for p in _executables(place) if not p.name.lower().endswith(".appimage")]
    return found


def path_programs() -> list[Path]:
    found = []
    for folder in os.environ.get("PATH", "").split(":"):
        try:
            found += [Path(folder) / n for n in os.listdir(folder) if "mcp" in n.lower()][:50]
        except OSError:
            continue
    return found


def appimages() -> list[Path]:
    found = []
    for folder in (HOME / "Applications", HOME / ".local/bin", HOME / "Downloads"):
        try:
            found += sorted(p for p in folder.iterdir() if p.name.lower().endswith(".appimage") and p.is_file())[:100]
        except OSError:
            continue
    return found


def appimage_stem(path: Path) -> str:
    """"wordcraft-0.6.1-linux-x86_64.AppImage" -> "wordcraft-0.6.1"."""
    stem = re.sub(r"\.appimage$", "", path.name, flags=re.IGNORECASE)
    return re.sub(r"[-_.](linux|x86_64|amd64|aarch64|arm64|x64)([-_.](x86_64|amd64|aarch64|arm64))?$", "", stem,
                  flags=re.IGNORECASE)


def appimage_bin(path: Path) -> Path | None:
    """usr/bin of an AppImage, unpacked into the cache (again only when the file changed)."""
    try:
        info = path.stat()
    except OSError:
        return None
    target = CACHE / f"{appimage_stem(path)}-{int(info.st_mtime)}-{info.st_size}"
    if not (target / "squashfs-root/usr/bin").is_dir():
        shutil.rmtree(target, ignore_errors=True)
        target.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run([str(path), "--appimage-extract", "usr/bin/*"], cwd=str(target), timeout=60,
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           env={**os.environ, "APPIMAGE_EXTRACT_AND_RUN": ""})
        except (OSError, subprocess.TimeoutExpired):
            return None
    folder = target / "squashfs-root/usr/bin"
    return folder if folder.is_dir() else None


# --- all together -------------------------------------------------------------------------------

def scan(configured_commands: dict | None = None, agent_commands=()) -> list[Candidate]:
    """MCP servers offered by programs on this PC. configured_commands: real
    program path -> [agents that have a server starting it]."""
    configured_commands = configured_commands or {}
    skip = CLIENTS | set(agent_commands)
    seen, found = set(), []

    def consider(program: Path, appimage: str = ""):
        try:
            real = program.resolve()
        except OSError:
            return
        if real in seen or program.name in skip or real.name in skip or "/mise/installs/" in str(real):
            return
        seen.add(real)
        if not _is_elf_or_script(real):
            return
        count, usage = scan_file(real)
        named = "mcp" in program.name.lower()
        # Its usage text has an "mcp" mode and the protocol shows up, or the name says MCP.
        if not ((usage and count >= 1) or count >= 2 or (named and count >= 1)):
            return
        text = usage.decode("utf-8", "replace")
        command, sure = command_for(program, text)
        found.append(Candidate(server_name(program), str(program), command, text, appimage,
                               configured_commands.get(str(real), []), sure))

    extracted = set()
    for program in own_programs():
        extracted.add(program.parent.parent.parent.name if program.parent.name == "bin" else "")
        consider(program)
    for program in path_programs():
        consider(program)
    for image in appimages():
        if appimage_stem(image) in extracted:
            continue          # already unpacked by hand (~/Applications/<app>-<version>/)
        folder = appimage_bin(image)
        for program in _executables(folder) if folder else []:
            consider(program, appimage=str(image))
    _save_cache()
    return found


def unpack(appimage: Path, into: Path | None = None) -> Path:
    """Unpack a whole AppImage into ~/Applications/<app>-<version>/ (as the
    PhotoCraft CLI is), for a server that lives in it. Returns that folder."""
    into = into or HOME / "Applications" / appimage_stem(appimage)
    if into.exists():
        return into
    work = into.with_name(f".{into.name}.unpacking")
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    try:
        subprocess.run([str(appimage), "--appimage-extract"], cwd=str(work), timeout=600, check=True,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        os.replace(work / "squashfs-root", into)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return into
