"""The AI coding agents Omarchy offers, and which of them are installed.

The catalog is Omarchy's own: the "setup.default.agent.*" entries of its menu
(name and icon), so a new agent there shows up here too. Installed or not is
decided the way omarchy-default-agent decides it, from the file system only
(nothing is started):

- OpenClaw is the `openclaw` pacman package; Hermes is either the
  `hermes-desktop` package or its mise copy. Another install of either (its own
  installer, pipx, npm, a wrapper of the user's) counts only when it is proven
  that agent's: its program's real path, or a small script's text, names one of
  the adapter's install_marks (another program can have the same name).
- Every other agent: the user's own install at ~/.local/bin/<agent> (a
  symlink or anything but Omarchy's mise wrapper), else mise's install folder
  for the agent's tool spec.

Omarchy writes a wrapper into ~/.local/bin for every agent it offers, so a
command on PATH says nothing about whether the agent is installed.
"""

import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import harnesses

BIN_DIR = Path.home() / ".local/bin"
MISE_DIR = Path.home() / ".local/share/mise"
PACMAN_DB = Path("/var/lib/pacman/local")
MAX_MENU_BYTES = 1024 * 1024
MAX_WRAPPER_BYTES = 4096
MAX_DIR_ENTRIES = 4000

_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_MENU_RE = re.compile(r'^\s*"setup\.default\.agent\.([a-z0-9-]{1,64})"\s*:\s*(\{.*\})\s*,?\s*$', re.MULTILINE)
_EXEC_RE = re.compile(r'^exec mise x "([^"\n]{1,512})" -- "([A-Za-z0-9._-]{1,64})" "\$@"$', re.MULTILINE)
_SEMVERISH = re.compile(r"[0-9][0-9A-Za-z.+_-]{0,63}")


def _rebuild():
    """Tables from the agent adapters (harnesses.py); run again when add-ons add or remove agents."""
    global FALLBACK, NAMES, SPECS, EXTRA
    adapters = harnesses.all_harnesses()
    # Omarchy's list as of 4.0 (omarchy-default-agent), used when its menu file cannot be read
    # (icons: Nerd Font, or Omarchy's own "omarchy" font).
    FALLBACK = [(h.command, h.label, h.icon, h.icon_font) for h in adapters if h.offered]
    # Fuller names than the menu's short labels.
    NAMES = {h.command: h.full_name for h in adapters if h.full_name}
    # mise tool specs that differ from the command (omarchy-default-agent's agent_package).
    SPECS = {h.command: h.spec for h in adapters if h.spec}
    # Agents Omarchy's menu does not offer (yet): this app installs them itself
    # (install_ctl.AGENT_INSTALLERS) into a version folder, the command in ~/.local/bin.
    EXTRA = [(h.command, h.label, h.icon, h.icon_font) for h in adapters if not h.offered]


_rebuild()
harnesses.on_change(_rebuild)

ROBOT = "\U000f06a9"


@dataclass
class Agent:
    command: str
    name: str
    icon: str
    icon_font: str = ""        # "omarchy" for Omarchy's own glyphs, "" for the Nerd Font
    installed: bool = False
    version: str = ""
    source: str = ""           # "mise", "package", "own"
    default: bool = False


def read_capped(path: Path, limit: int) -> str | None:
    try:
        with open(path, "rb") as handle:
            data = handle.read(limit + 1)
    except OSError:
        return None
    return None if len(data) > limit else data.decode("utf-8", "replace")


def catalog(menu_file: Path) -> list[tuple[str, str, str, str]]:
    """[(command, label, icon, icon font)] in the order of an Omarchy menu file (desktop_omarchy passes its own)."""
    text = read_capped(menu_file, MAX_MENU_BYTES)
    found = []
    for match in _MENU_RE.finditer(text or ""):
        try:
            entry = json.loads(match.group(2))
        except ValueError:
            continue
        command = match.group(1)
        if not isinstance(entry, dict) or not _NAME_RE.fullmatch(command):
            continue
        label, icon = entry.get("label"), entry.get("icon")
        if not isinstance(label, str) or not isinstance(icon, str):
            continue
        font = "omarchy" if entry.get("iconFont") == "omarchy" else ""
        found.append((command, label[:40], icon[:2], font))
    return found or list(FALLBACK)


def default_agent(agent_file: Path) -> str:
    text = read_capped(agent_file, 256) or ""
    value = text.strip().splitlines()[0] if text.strip() else ""
    return value if _NAME_RE.fullmatch(value) else ""


def wrapper_spec(command: str, bin_dir: Path = BIN_DIR) -> str | None:
    """The mise tool spec in Omarchy's wrapper ~/.local/bin/<command>, if it is one."""
    path = bin_dir / command
    if path.is_symlink() or not path.is_file():
        return None
    text = read_capped(path, MAX_WRAPPER_BYTES)
    match = _EXEC_RE.search(text or "")
    return match.group(1) if match and match.group(2) == command else None


def own_install(command: str, bin_dir: Path = BIN_DIR) -> bool:
    """Anything at the wrapper's path other than Omarchy's wrapper (e.g. the
    symlink Cursor's installer leaves) is the user's own install."""
    path = bin_dir / command
    if path.is_symlink():
        return path.exists()
    if not path.is_file():
        return False
    text = read_capped(path, MAX_WRAPPER_BYTES)
    return text is None or not re.search(r"^mise use -g", text, re.MULTILINE) and wrapper_spec(command, bin_dir) is None


def install_dir_candidates(spec: str) -> list[str]:
    """mise's folder names for a tool spec: "opencode", "npm:@a/b" -> "npm-a-b",
    "http:muse[...]" -> "http-muse"."""
    base = spec.split("[", 1)[0]
    plain = re.sub(r"[:/@]+", "-", base).strip("-")
    names = [plain, base.rsplit("/", 1)[-1].split(":", 1)[-1].lstrip("@")]
    return [n for n in dict.fromkeys(names)
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}", n) and ".." not in n]


def mise_version(folder: Path) -> str:
    """The version mise uses: where "global" (or "latest") points, else the
    newest version folder."""
    for link in ("global", "latest"):
        path = folder / link
        if path.is_symlink():
            try:
                target = Path(path.readlink()).name
            except OSError:
                continue
            if _SEMVERISH.fullmatch(target):
                return target
    try:
        versions = [p for p in list(folder.iterdir())[:MAX_DIR_ENTRIES]
                    if p.is_dir() and not p.is_symlink() and _SEMVERISH.fullmatch(p.name)]
    except OSError:
        return ""
    return max(versions, key=lambda p: p.stat().st_mtime).name if versions else ""


def mise_install(spec: str, mise_dir: Path = MISE_DIR) -> Path | None:
    """mise's install folder for the spec, when it holds at least one real version (after
    `mise uninstall --all` the folder can stay, with only links to versions that are gone)."""
    installs = mise_dir / "installs"
    for name in install_dir_candidates(spec):
        folder = installs / name
        if folder.is_dir() and not folder.is_symlink():
            try:
                if any(p.is_dir() and not p.is_symlink() for p in list(folder.iterdir())[:MAX_DIR_ENTRIES]):
                    return folder
            except OSError:
                continue
    return None


def package_version(name: str, db: Path = PACMAN_DB) -> str | None:
    """Version of an installed pacman package, or None when it is not installed."""
    try:
        entries = list(db.iterdir())[:MAX_DIR_ENTRIES * 10]
    except OSError:
        return None
    prefix = name + "-"
    for entry in entries:
        if not entry.name.startswith(prefix):
            continue
        # <name>-<pkgver>-<pkgrel>: exactly two parts left ("openclaw-desktop-1.0-1"
        # is another package).
        parts = entry.name[len(prefix):].split("-")
        if len(parts) == 2 and parts[0] and parts[1].replace(".", "").isdigit():
            return parts[0].split(":", 1)[-1]   # without an epoch
    return None


def system_program(command: str, bin_dir: Path = BIN_DIR, mise_dir: Path = MISE_DIR) -> str | None:
    """A program of a distribution package or another installer: on PATH, but not
    mise's shims and wrappers or ~/.local/bin, which can be stubs that install the
    agent at its first start (detect reads those itself)."""
    skipped = {bin_dir, BIN_DIR}
    mises = (mise_dir, MISE_DIR)
    folders = [f for f in os.environ.get("PATH", "").split(":") if f]
    kept = [f for f in folders if Path(f) not in skipped
            and not any(Path(f) == m or m in Path(f).parents for m in mises)]
    return shutil.which(command, path=":".join(kept)) if kept else None


def on_system_path(command: str, bin_dir: Path = BIN_DIR, mise_dir: Path = MISE_DIR) -> bool:
    return system_program(command, bin_dir, mise_dir) is not None


_RUNS_MISE = re.compile(r"\bmise\s+(x|exec|use|install|where)\b")


def _mise_data(environ) -> Path:
    data = environ.get("MISE_DATA_DIR", "")
    return Path(data) if data.startswith("/") else Path(environ.get("HOME") or Path.home()) / ".local/share/mise"


def _mise_launcher_folder(folder, environ) -> bool:
    """In mise's folder but not in a version it installed: its shims and command wrappers."""
    mise, folder = _mise_data(environ), Path(os.path.normpath(folder))
    return (folder == mise or mise in folder.parents) and (mise / "installs") not in folder.parents


def lazy_launcher(path, environ=None) -> bool:
    """A program that installs or updates what it stands for when started (omarchy-agent-usage-codex's
    is_lazy_launcher), so never one to run for a look: one of mise's shims or command wrappers, a link
    to mise itself, or a script whose first 4 KiB run mise (Omarchy's `mise use -g …` and
    `exec mise x …`). One whose head cannot be read counts as one. Nothing is started."""
    environ = os.environ if environ is None else environ
    if _mise_launcher_folder(os.path.dirname(os.path.abspath(path)), environ):
        return True
    try:
        real = os.path.realpath(path, strict=True)
        owner = os.stat(real).st_uid
        if owner not in (os.getuid(), 0):
            return True
        with open(real, "rb") as handle:
            head = handle.read(4096).decode("utf-8", "replace")
    except OSError:
        return True
    return (os.path.basename(real) == "mise" or _mise_launcher_folder(os.path.dirname(real), environ)
            or bool(_RUNS_MISE.search(head)))


def real_program(command: str, specs=(), environ=None) -> str | None:
    """The agent's installed program (omarchy-agent-usage-codex's find_codex_binary, offline), never a
    launcher that would install or update it (lazy_launcher): the first on PATH, then in ~/.local/bin
    and ~/.npm-global/bin; else the program in the version mise uses of one of `specs` (its folder's
    bin/ or top). None when none is installed. Nothing is started."""
    environ = os.environ if environ is None else environ
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}", command):
        return None
    home = Path(environ.get("HOME") or Path.home())
    folders = [os.path.normpath(f) for f in environ.get("PATH", "").split(":") if f.startswith("/")]
    folders += [str(home / ".local/bin"), str(home / ".npm-global/bin")]

    def runnable(path) -> bool:
        return os.path.isfile(path) and os.access(path, os.X_OK)
    for folder in dict.fromkeys(folders):
        path = os.path.join(folder, command)
        if not _mise_launcher_folder(folder, environ) and runnable(path) and not lazy_launcher(path, environ):
            return path
    for spec in dict.fromkeys([*specs, command]):
        folder = mise_install(spec, _mise_data(environ))
        version = mise_version(folder) if folder is not None else ""
        if not version:
            continue
        for path in (folder / version / "bin" / command, folder / version / command):
            if runnable(path) and not lazy_launcher(path, environ):
                return str(path)
    return None


def proven(path, marks) -> bool:
    """The program at path is the agent's own (adapter fact install_marks): its real path (links
    followed) or, for a script of at most MAX_WRAPPER_BYTES, its text names one of the marks ("~/"
    is the home folder; in a script's text also "$HOME/", "${HOME}/" or "~/"). A script that runs
    mise is a stub that builds the agent at its first start (mise's own install folder says whether
    that happened), never proof. Nothing is started."""
    if not marks or path is None:
        return False
    home = os.path.expanduser("~")
    wanted = [home + m[1:] if m.startswith("~/") else m for m in marks]
    spelled = wanted + [s + m[1:] for m in marks if m.startswith("~/") for s in ("~", "$HOME", "${HOME}")]
    try:
        real = os.path.realpath(path, strict=True)
    except OSError:
        return False                                  # gone, or a link to nothing
    if any(m in real for m in wanted):
        return True
    if not os.path.isfile(real):
        return False
    text = read_capped(Path(real), MAX_WRAPPER_BYTES)
    return text is not None and not _RUNS_MISE.search(text) and any(m in text for m in spelled)


def detect(command: str, bin_dir: Path = BIN_DIR, mise_dir: Path = MISE_DIR,
           db: Path = PACMAN_DB) -> tuple[bool, str, str]:
    """(installed, version, source) for one agent."""
    h = harnesses.get(command)
    if h is not None and h.detect is not None:              # it knows best (an add-on's agent)
        try:
            installed, version, source = h.detect()
            return bool(installed), str(version or "")[:64], str(source or "own")[:16]
        except Exception:
            return False, "", ""
    if h is not None and (h.installer is not None or h.download is not None):   # the app installs it itself
        import install_ctl
        inst = install_ctl.AGENT_INSTALLERS.get(command)
        if inst is not None and inst.installed_folder() is not None:
            version = inst.current_version().lstrip("v")
            return True, version.split("-")[0] if h.short_version else version, "own"
        if own_install(command, bin_dir):
            return True, "", "own"
        # Its recipe may install through mise first (Antigravity CLI).
        folder = mise_install(SPECS.get(command, command), mise_dir)
        if folder is not None:
            return True, mise_version(folder), "mise"
        return (True, "", "system") if on_system_path(command, bin_dir, mise_dir) else (False, "", "")
    if h is not None and h.package:
        version = package_version(h.package, db)
        if version is not None:
            return True, version, "package"
    only = h is not None and h.package_only
    # Any program by the name, or only one proven the agent's (package_only, own_install_counts off)?
    anything = h is None or (h.own_install_counts and not only)
    marks = h.install_marks if h is not None else ()
    spec = wrapper_spec(command, bin_dir) or SPECS.get(command, command)
    folder = None if only else mise_install(spec, mise_dir)
    if own_install(command, bin_dir) and (anything or proven(bin_dir / command, marks)):
        # A script of the user's own may still run mise's copy: show its version.
        return True, mise_version(folder) if folder is not None else "", "own"
    if folder is not None:
        return True, mise_version(folder), "mise"
    program = system_program(command, bin_dir, mise_dir)
    if program is not None and (anything or proven(program, marks)):
        return True, "", "system"
    return False, "", ""


def install_stamp(bin_dir: Path = BIN_DIR, mise_dir: Path = MISE_DIR, db: Path = PACMAN_DB) -> tuple:
    """When the places agents are installed to last changed (mise's installs, ~/.local/bin, pacman's
    database): a different stamp means an agent was installed or removed, also outside the app."""
    stamp = []
    for path in (mise_dir / "installs", bin_dir, db):
        try:
            stamp.append(path.stat().st_mtime_ns)
        except OSError:
            stamp.append(0)
    return tuple(stamp)


def list_agents(menu_file: Path | None = None, agent_file: Path | None = None, bin_dir: Path = BIN_DIR,
                mise_dir: Path = MISE_DIR, db: Path = PACMAN_DB) -> list[Agent]:
    """Every agent the desktop offers (desktop.agent_catalog(), else FALLBACK; or the
    given menu file): installed ones first (the default agent leading), then the
    others, each group in the desktop's order."""
    import desktop
    chosen = default_agent(agent_file) if agent_file is not None else desktop.default_agent()
    agents = []
    known = catalog(menu_file) if menu_file is not None else list(desktop.agent_catalog() or FALLBACK)
    known += [e for e in EXTRA if e[0] not in {c for c, *_rest in known}]
    for command, label, icon, font in known:
        installed, version, source = detect(command, bin_dir, mise_dir, db)
        agents.append(Agent(command=command, name=NAMES.get(command, label), icon=icon or ROBOT,
                            icon_font=font, installed=installed, version=version, source=source,
                            default=command == chosen))
    order = {a.command: i for i, a in enumerate(agents)}
    return sorted(agents, key=lambda a: (not a.installed, not a.default, order[a.command]))
