"""The AI coding agents Omarchy offers, and which of them are installed.

The catalog is Omarchy's own: the "setup.default.agent.*" entries of its menu
(name and icon), so a new agent there shows up here too. Installed or not is
decided the way omarchy-default-agent decides it, from the file system only
(nothing is started):

- OpenClaw is the `openclaw` pacman package; Hermes is either the
  `hermes-desktop` package or its mise copy.
- Every other agent: the user's own install at ~/.local/bin/<agent> (a
  symlink or anything but Omarchy's mise wrapper), else mise's install folder
  for the agent's tool spec.

Omarchy writes a wrapper into ~/.local/bin for every agent it offers, so a
command on PATH says nothing about whether the agent is installed.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path

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

# Omarchy's list as of 4.0 (omarchy-default-agent), used when its menu file
# cannot be read. Icons: Nerd Font, or Omarchy's own "omarchy" font.
FALLBACK = [
    ("claude", "Claude", "\U000f06c4", ""), ("codex", "Codex", "", "omarchy"),
    ("copilot", "Copilot", "", ""), ("crush", "Crush", "\U000f02d1", ""),
    ("cursor-agent", "Cursor CLI", "", "omarchy"), ("gemini", "Gemini", "\U000f0ae2", ""),
    ("grok", "Grok", "", "omarchy"), ("hermes", "Hermes", "", "omarchy"),
    ("muse", "Muse Code", "\U000f06e4", ""), ("omp", "omp", "", "omarchy"),
    ("openclaw", "OpenClaw", "", "omarchy"), ("opencode", "OpenCode", "", "omarchy"),
    ("pi", "Pi", "", "omarchy"),
]
# Fuller names than the menu's short labels.
NAMES = {"claude": "Claude Code", "copilot": "GitHub Copilot", "gemini": "Gemini CLI", "omp": "Oh My Pi",
         "pi": "Pi"}
# mise tool specs that differ from the command (omarchy-default-agent's agent_package).
SPECS = {
    "omp": "github:can1357/oh-my-pi", "grok": "npm:@xai-official/grok",
    "muse": "http:muse[url=https://api.meta.ai/muse-launcher.sh,bin=muse]",
    "hermes": "pipx:hermes-agent[extras=all]",
}
ROBOT = "\U000f06a9"
# Agents Omarchy's menu does not offer (yet): AI Agents Map installs them itself
# (install_ctl.AGENT_INSTALLERS) into a version folder, the command in ~/.local/bin.
EXTRA = [("dsh", "DeepSeek Harness", "\U000f0e7b", ""), ("bionic", "LM Studio Bionic", "\U000f09d1", "")]


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
    installs = mise_dir / "installs"
    for name in install_dir_candidates(spec):
        folder = installs / name
        if folder.is_dir() and not folder.is_symlink():
            return folder
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


def detect(command: str, bin_dir: Path = BIN_DIR, mise_dir: Path = MISE_DIR,
           db: Path = PACMAN_DB) -> tuple[bool, str, str]:
    """(installed, version, source) for one agent."""
    if command in dict((c, 1) for c, *_rest in EXTRA):
        import install_ctl
        inst = install_ctl.AGENT_INSTALLERS.get(command)
        if inst is not None and inst.installed_folder() is not None:
            version = inst.current_version().lstrip("v")
            return True, version.split("-")[0] if command == "bionic" else version, "own"
        return (True, "", "own") if own_install(command, bin_dir) else (False, "", "")
    if command == "openclaw":
        version = package_version("openclaw", db)
        return (True, version, "package") if version is not None else (False, "", "")
    if command == "hermes":
        version = package_version("hermes-desktop", db)
        if version is not None:
            return True, version, "package"
    spec = wrapper_spec(command, bin_dir) or SPECS.get(command, command)
    folder = mise_install(spec, mise_dir)
    if command != "hermes" and own_install(command, bin_dir):
        # A script of the user's own may still run mise's copy: show its version.
        return True, mise_version(folder) if folder is not None else "", "own"
    if folder is not None:
        return True, mise_version(folder), "mise"
    return False, "", ""


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
