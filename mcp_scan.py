"""Find programs on this PC that offer an MCP server, without running them.

Where it looks:
- the user's own programs: ~/.local/bin, ~/bin, ~/Applications/<app>/
  (extracted AppImages: usr/bin), /opt/*/bin, and the folders added on
  Setup (extra_folders: the folder itself and its <app>/ folders, as
  ~/Applications);
- AppImages in ~/Applications and ~/.local/bin: never run by the scan.
  Only when the user clicks "Look inside" (look_inside) is each one's
  usr/bin unpacked (the AppImage's own --appimage-extract) into a private
  temporary folder, read, and deleted again; what was found is kept per
  file (path, size, time) in the scan cache. Downloads are not looked at;
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
from dataclasses import dataclass, field
from pathlib import Path

HOME = Path.home()
CACHE = HOME / ".cache/ai-agents-map/appimages"     # old versions unpacked here; removed (cleanup_old_cache)
TEMP = HOME / ".cache/ai-agents-map/tmp"
MAX_BIN_BYTES = 2 * 1024 ** 3      # usr/bin of one AppImage, unpacked to look inside
MAX_APP_BYTES = 10 * 1024 ** 3     # a whole AppImage unpacked for a server in it
OPT = Path("/opt")
MARKERS = (b"notifications/initialized", b"protocolVersion", b"tools/list", b"modelcontextprotocol")
# Programs that use MCP servers or run them, never offered as one; the agents' commands and the
# local LLMs' programs come on top (clients()).
CLIENTS = {"lms", "node", "bun", "deno",
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
    inner: str = ""           # an AppImage's program: its name in usr/bin
    version: str = ""         # the app's version ("0.7.0"), from its folder or AppImage name
    update_from: str = ""     # a newer version of an app found in an older one: the older version
    replaces: list = field(default_factory=list)     # the older versions' programs (links and real paths)
    old_folders: list = field(default_factory=list)  # their unpacked folders in ~/Applications
    users: list = field(default_factory=list)        # agents (and "list") that start an older version


def _is_elf_or_script(path: Path) -> bool:
    import ai_core
    try:
        head = ai_core.read_head(path, 4)
    except OSError:
        return False
    return head == b"\x7fELF" or head[:2] == b"#!"


USAGE_SPAN = 900        # the usage block read after a hit
OVERLAP = 2048          # kept from one chunk to the next: a marker or a usage block across the seam


def _scan_bytes(fd: int, size: int, name: str) -> tuple[int, bytes]:
    """The markers and the MCP usage block from an open file, read in CHUNKs (never mapped: a file
    someone truncates meanwhile then reads short instead of killing the process) and never more
    than `size` bytes."""
    seen, first_hit, second_hit = set(), None, None
    automation = server_text = False
    tail, done = b"", 0

    def read() -> bytes:
        nonlocal done
        chunk = os.read(fd, min(CHUNK, size - done)) if done < size else b""
        done += len(chunk)
        return chunk

    chunk = read()
    while chunk:
        following = read()           # read one ahead: the last buffer is known as the last one
        data = tail + chunk
        last = not following
        chunk = following
        seen.update(m for m in MARKERS if m not in seen and m in data)
        automation = automation or b"--automation-read-root" in data
        server_text = server_text or b"MCP server" in data
        # A hit whose block would run past this buffer is taken from the next one (it is in its
        # overlap); at the end of the file the block is what is there.
        usable = len(data) if last else len(data) - USAGE_SPAN
        if first_hit is None:
            hit = re.search(rb"[\w.-]{1,64} mcp +\[", data)
            if hit and (hit.start() < usable):
                first_hit = data[hit.start():hit.start() + USAGE_SPAN]
        if first_hit is None and second_hit is None:
            hit = re.search(rb"mcp +\[--[a-z]", data)
            if hit and hit.start() < usable:
                second_hit = data[hit.start():hit.start() + USAGE_SPAN]
        tail = data[-OVERLAP:]
    count = len(seen)
    usage = b""
    block = first_hit if first_hit is not None else second_hit
    if block is not None:
        # The mcp usage: its first line and the indented lines after it.
        lines = block.split(b"\n")
        kept = [lines[0]]
        for line in lines[1:]:
            if not line.startswith(b"   ") or re.match(rb"\s+[\w.-]+ \w", line) and b"--" not in line:
                break
            kept.append(line.strip())
        usage = b" ".join(kept).split(b"\x00")[0][:600]
    if not usage and count >= 2 and automation:
        # A usage text made when the program runs (clap): its root options are still there.
        usage = b"mcp [--automation-read-root DIR] [--automation-write-root DIR]"
    if not usage and server_text and "mcp" in name.lower():
        count = max(count, 1)
    return count, usage


def scan_file(path: Path, keep: bool = True) -> tuple[int, bytes]:
    """(how many MARKERS, the MCP usage block or b"") from the file's bytes.
    Kept per file (size, time) in SCAN_CACHE: these programs are large."""
    try:
        info = path.stat()
    except OSError:
        return 0, b""
    key = f"{path}|{info.st_size}|{int(info.st_mtime)}"
    if keep and key in _cache():
        count, usage = _cache()[key]
        return count, usage.encode("utf-8", "replace")
    if info.st_size > MAX_BYTES or info.st_size == 0:
        return 0, b""
    import ai_core
    # "x-cli mcp [--root DIR]"; or only "mcp [--…" where the program's name is put in front when it
    # runs (the help text right after another string: "…mcpmcp [--connect PORT]").
    try:
        fd = ai_core.open_regular(path)
        try:
            size = min(os.fstat(fd).st_size, MAX_BYTES)
            count, usage = _scan_bytes(fd, size, path.name)
        finally:
            os.close(fd)
    except (OSError, ValueError):
        return 0, b""
    if keep:
        _cache()[key] = (count, usage.decode("utf-8", "replace"))
    return count, usage


SCAN_CACHE = CACHE.parent / "mcp-scan-3.json"
_CACHE = None


def _cache() -> dict:
    global _CACHE
    if _CACHE is None:
        import json
        try:
            import ai_core
            data = json.loads(ai_core.read_head(SCAN_CACHE, 4_000_000, follow=False))
            _CACHE = {k: tuple(v) for k, v in data.items()} if isinstance(data, dict) else {}
        except (OSError, ValueError):
            _CACHE = {}
    return _CACHE


def _save_cache():
    import json
    if _CACHE is None:
        return
    import ai_core
    try:
        ai_core.atomic_write(SCAN_CACHE, json.dumps(dict(list(_CACHE.items())[-2000:])))
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
    if re.search(r"\|\s*--headless\b|\[--headless\]", usage):
        command.append("--headless")          # its default is a running app (vectorcraft --connect)
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
    import ai_core
    try:
        entries = sorted(Path(e.path) for e in ai_core.scandir_head(folder, 500))
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


def added_folders() -> list[Path]:
    """The folders added on Setup › MCP servers (Add folder…)."""
    try:
        import extra_folders
        return extra_folders.folders("mcp")
    except (OSError, ValueError):
        return []


def own_programs() -> list[Path]:
    places = [HOME / ".local/bin", HOME / "bin"]
    added = added_folders()
    for folder in added:
        places += [folder, folder / "bin", folder / "usr/bin"]
    import ai_core
    for root in (HOME / "Applications", OPT, *added):
        try:
            apps = sorted(Path(e.path) for e in ai_core.scandir_head(root, 1000) if e.is_dir())[:100]
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
            import ai_core
            found += [Path(e.path) for e in ai_core.scandir_head(folder, 20000) if "mcp" in e.name.lower()][:50]
        except OSError:
            continue
    return found


def appimages() -> list[Path]:
    """AppImages the user keeps as programs (executable, in ~/Applications, ~/.local/bin or a
    folder added on Setup)."""
    found = []
    for folder in (HOME / "Applications", HOME / ".local/bin", *added_folders()):
        try:
            import ai_core
            found += sorted(Path(e.path) for e in ai_core.scandir_head(folder, 5000)
                            if e.name.lower().endswith(".appimage") and e.is_file()
                            and os.access(e.path, os.X_OK))[:100]
        except OSError:
            continue
    return found


def appimage_stem(path: Path) -> str:
    """"wordcraft-0.6.1-linux-x86_64.AppImage" -> "wordcraft-0.6.1"."""
    stem = re.sub(r"\.appimage$", "", path.name, flags=re.IGNORECASE)
    return re.sub(r"[-_.](linux|x86_64|amd64|aarch64|arm64|x64)([-_.](x86_64|amd64|aarch64|arm64))?$", "", stem,
                  flags=re.IGNORECASE)


VERSION_RE = re.compile(r"^(?P<app>.+?)[-_]v?(?P<version>\d+(?:\.\d+)+)$")


def app_version(stem: str) -> tuple[str, tuple] | None:
    """"gridcraft-0.7.0" -> ("gridcraft", (0, 7, 0)); None without a version."""
    hit = VERSION_RE.match(stem)
    if not hit:
        return None
    return hit["app"].lower(), tuple(int(part) for part in hit["version"].split("."))


def version_text(version: tuple) -> str:
    return ".".join(str(part) for part in version)


def _unpacked_versions() -> dict:
    """app -> versions unpacked in ~/Applications/<app>-<version>/."""
    found = {}
    try:
        import ai_core
        folders = [Path(e.path) for e in ai_core.scandir_head(HOME / "Applications", 5000)
                   if e.is_dir() and not e.name.startswith(".")]
    except OSError:
        return found
    for folder in folders[:200]:
        info = app_version(folder.name)
        if info:
            found.setdefault(info[0], []).append(info[1])
    return found


def _looked_into_versions() -> dict:
    """app -> versions of its AppImages looked into before (the files may be gone)."""
    found = {}
    for key in _cache():
        if key.startswith("appimage:"):
            info = app_version(appimage_stem(Path(key[len("appimage:"):].split("|")[0])))
            if info:
                found.setdefault(info[0], []).append(info[1])
    return found


def new_versions_found() -> list[Path]:
    """AppImages not looked into yet that are newer versions of apps looked into
    or unpacked before. Only listed (file names and stat, nothing is run): a
    newer file of the same name is not proof of an authentic update, so it is
    looked into like any other AppImage, after the user's click (look_inside)."""
    known = _unpacked_versions()
    for app, versions in _looked_into_versions().items():
        known.setdefault(app, []).extend(versions)
    result = []
    for image in not_looked_into():
        info = app_version(appimage_stem(image))
        if info and any(v < info[1] for v in known.get(info[0], [])):
            result.append(image)
    return result


def _image_key(path: Path) -> str | None:
    try:
        info = path.stat()
    except OSError:
        return None
    return f"appimage:{path}|{info.st_size}|{int(info.st_mtime)}"


def looked_into(path: Path):
    """What look_inside found in this AppImage ([(program, count, usage)]), or None if not looked yet."""
    key = _image_key(path)
    found = _cache().get(key) if key else None
    return [tuple(item) for item in found] if isinstance(found, (list, tuple)) else None


def _folder_size(folder: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(folder):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def unpack_bin(path: Path) -> tuple[Path, Path]:
    """usr/bin of an AppImage unpacked into a new private temporary folder:
    (that folder, its usr/bin). The caller removes the folder (shutil.rmtree)."""
    import tempfile
    import ai_core
    TEMP.mkdir(parents=True, exist_ok=True, mode=0o700)
    work = Path(tempfile.mkdtemp(prefix="appimage-", dir=str(TEMP)))
    try:
        ok = ai_core.run_with_disk_budget([str(path), "--appimage-extract", "usr/bin/*"], str(work),
                                          MAX_BIN_BYTES, timeout=60)          # stopped as soon as it gets larger
        folder = work / "squashfs-root/usr/bin"
        if not ok or not folder.is_dir() or _folder_size(work) > MAX_BIN_BYTES:
            raise RuntimeError(f"could not look inside {path.name}")
    except BaseException:                  # nothing of a failed look stays
        shutil.rmtree(work, ignore_errors=True)
        raise
    return work, folder


def look_inside(paths) -> dict:
    """The user's "Look inside": each AppImage's programs are read once
    (unpacked to a temporary folder that is removed right after)."""
    results = {}
    for path in paths:
        key = _image_key(path)
        if key is None:
            continue
        found = []
        try:
            work, folder = unpack_bin(path)
        except RuntimeError:
            _cache()[key] = []
            continue
        try:
            for program in _executables(folder):
                if not program.is_symlink() and _is_elf_or_script(program):
                    count, usage = scan_file(program, keep=False)
                    found.append((program.name, count, usage.decode("utf-8", "replace")))
        finally:
            shutil.rmtree(work, ignore_errors=True)
        _cache()[key] = found
        results[str(path)] = found
    _save_cache()
    return results


def not_looked_into() -> list[Path]:
    """AppImages whose programs were never looked at (and not unpacked by hand)."""
    import ai_core
    try:
        extracted = {e.name for e in ai_core.scandir_head(HOME / "Applications", 5000) if e.is_dir()}
    except OSError:
        extracted = set()
    return [i for i in appimages() if appimage_stem(i) not in extracted and looked_into(i) is None]


def cleanup_old_cache():
    """Earlier versions kept unpacked usr/bin folders here (large): gone."""
    if CACHE.is_dir() and not CACHE.is_symlink():
        shutil.rmtree(CACHE, ignore_errors=True)


# --- all together -------------------------------------------------------------------------------

def clients() -> set:
    import harnesses
    import llm_engine
    import providers
    return CLIENTS | {h.command for h in harnesses.all_harnesses()} | {
        name for p in providers.local() for name in (p.key, *llm_engine.of(p.key).programs)}


def scan(configured_commands: dict | None = None, agent_commands=()) -> list[Candidate]:
    """MCP servers offered by programs on this PC. configured_commands: real
    program path -> [agents that have a server starting it]."""
    configured_commands = configured_commands or {}
    skip = clients() | set(agent_commands)
    seen, found = set(), []

    def offer(program: Path, count: int, text: str, appimage: str = "", inner: str = ""):
        named = "mcp" in program.name.lower()
        # Its usage text has an "mcp" mode and the protocol shows up, or the name says MCP.
        if not ((text and count >= 1) or count >= 2 or (named and count >= 1)):
            return
        command, sure = command_for(program, text)
        found.append(Candidate(server_name(program), str(program), command, text, appimage,
                               configured_commands.get(str(program), []), sure, inner))

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
        for name, count, text in looked_into(image) or []:
            # Where it will live once added (unpacked into ~/Applications/<app>-<version>/).
            program = HOME / "Applications" / appimage_stem(image) / "usr/bin" / name
            offer(program, count, text, appimage=str(image), inner=name)
    _save_cache()
    return fold_versions(found)


def _app_of(candidate: Candidate) -> tuple[str, tuple, Path | None] | None:
    """(app, version, its unpacked folder or None) of a candidate living in an
    app folder ~/Applications/<app>-<version>/ or in an AppImage."""
    if candidate.appimage:
        info = app_version(appimage_stem(Path(candidate.appimage)))
        return (*info, None) if info else None
    try:
        real = Path(candidate.program).resolve()
        rest = real.relative_to(HOME.resolve() / "Applications")
    except (OSError, ValueError):
        return None
    if len(rest.parts) < 2:
        return None
    info = app_version(rest.parts[0])
    return (*info, HOME / "Applications" / rest.parts[0]) if info else None


def fold_versions(found: list[Candidate]) -> list[Candidate]:
    """One entry per app program: the newest version. When older versions are
    there too (unpacked, or linked from ~/.local/bin), the newest says which
    version it updates and who starts the older ones."""
    groups, result = {}, []
    for candidate in found:
        info = _app_of(candidate)
        if info is None:
            result.append(candidate)
            continue
        app, version, folder = info
        candidate.version = version_text(version)
        name = candidate.inner or Path(candidate.program).resolve().name
        groups.setdefault((app, name), []).append((version, folder, candidate))
    for items in groups.values():
        items.sort(key=lambda item: item[0])
        _version, _folder, newest = items[-1]
        older = [item for item in items[:-1] if item[0] < items[-1][0]]
        if older:
            used = [item for item in older if item[2].configured]
            newest.update_from = version_text((used or older)[-1][0])
            for _v, folder, candidate in older:
                paths = [candidate.program]
                try:
                    paths.append(str(Path(candidate.program).resolve()))
                except OSError:
                    pass
                for path in paths:
                    if path not in newest.replaces:
                        newest.replaces.append(path)
                if folder is not None and str(folder) not in newest.old_folders:
                    newest.old_folders.append(str(folder))
                newest.users += [a for a in candidate.configured if a not in newest.users]
        result.append(newest)
    return result


def unpack(appimage: Path, into: Path | None = None) -> Path:
    """Unpack a whole AppImage into ~/Applications/<app>-<version>/ (as the
    PhotoCraft CLI is), for a server that lives in it. Returns that folder."""
    into = into or HOME / "Applications" / appimage_stem(appimage)
    if into.exists():
        return into
    import ai_core
    work = into.with_name(f".{into.name}.unpacking")
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, mode=0o700)
    try:
        ok = ai_core.run_with_disk_budget([str(appimage), "--appimage-extract"], str(work), MAX_APP_BYTES,
                                          timeout=600)                        # stopped as soon as it gets larger
        if not ok or not (work / "squashfs-root").is_dir():
            raise RuntimeError(f"could not unpack {appimage.name}")
        if _folder_size(work) > MAX_APP_BYTES:
            raise RuntimeError(f"{appimage.name} is too large to unpack")
        os.replace(work / "squashfs-root", into)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return into
