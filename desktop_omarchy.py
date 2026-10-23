"""The Omarchy desktop (Hyprland): the implementation of desktop.py's interface.

- own files: ~/.config/omarchy/ai-agents-map/
- theme: ~/.local/state/omarchy/current/theme/ (colors.toml, hyprland.lua), the
  windows' rounding from Hyprland (its IPC socket, else `hyprctl getoption`)
- windows: Hyprland's Lua dispatchers through `hyprctl dispatch` (0.55+); the
  selector is a fixed string built from our app id
- terminals: `xdg-terminal-exec` (Omarchy's choice of terminal), and
  `omarchy-launch-floating-terminal-with-presentation` for commands that should
  stay open until they are done
- agents: the "setup.default.agent.*" entries of Omarchy's menu, its default
  agent (~/.config/omarchy/defaults/agent), its usage records
  (~/.local/state/omarchy/agents/usage) and `omarchy-agent-usage-update`.

Every program is run as an argv list with bounded output (ai_core.read_bounded_output).
ai_core is imported where it is used: ai_core asks config_dir() while it is imported.
"""

import json
import os
import re
import shutil
import socket
import subprocess
import time
import tomllib
from pathlib import Path

NAME = "omarchy"
APP_NAME = "AI Agents Map"          # the Omarchy plugin's name
APP_ID = "org.omarchy.aiagentsmap"
HOME = Path.home()
CONFIG_DIR = HOME / ".config/omarchy/ai-agents-map"
THEME_DIR = HOME / ".local/state/omarchy/current/theme"
COLORS_TOML = THEME_DIR / "colors.toml"
THEME_LUA = THEME_DIR / "hyprland.lua"
USER_LUA = HOME / ".config/hypr/looknfeel.lua"
AGENT_MENU = Path("/usr/share/omarchy/default/omarchy/omarchy-menu.jsonc")   # its "setup.default.agent.*" entries
DEFAULT_AGENT_FILE = HOME / ".config/omarchy/defaults/agent"
USAGE_DIR = Path(os.environ.get("XDG_STATE_HOME") or HOME / ".local/state") / "omarchy/agents/usage"
USAGE_TIMEOUT = 60
MAX_THEME_BYTES = 256 * 1024
FLOATING_TERMINAL = "omarchy-launch-floating-terminal-with-presentation"


# --- the app's name and own files ----------------------------------------------------------------

def app_name() -> str:
    return APP_NAME


def app_id() -> str:
    return APP_ID


def config_dir() -> Path:
    return CONFIG_DIR


# --- theme ----------------------------------------------------------------------------------------

def theme_dir() -> Path:
    return THEME_DIR


def theme_colors(path: Path = COLORS_TOML) -> dict:
    """The current theme's colors.toml ({} when it cannot be read)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
        with os.fdopen(fd, "rb") as handle:
            data = handle.read(MAX_THEME_BYTES + 1)
        if len(data) > MAX_THEME_BYTES:
            return {}
        theme = tomllib.loads(data.decode("utf-8", "replace"))
    except (OSError, ValueError):
        return {}
    return theme if isinstance(theme, dict) else {}


def _hyprland_socket() -> Path | None:
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    signature = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", "")
    if not os.path.isabs(runtime) or not re.fullmatch(r"[A-Za-z0-9_]{1,200}", signature):
        return None
    return Path(runtime) / "hypr" / signature / ".socket.sock"


def _hyprland_query(command: bytes, max_bytes: int = 4096, timeout: float = 0.5) -> bytes | None:
    """One bounded request on Hyprland's IPC socket."""
    path = _hyprland_socket()
    if path is None:
        return None
    deadline = time.monotonic() + timeout
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(path))
            sock.sendall(command)
            chunks, total = [], 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                sock.settimeout(remaining)
                chunk = sock.recv(4096)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    return None
                chunks.append(chunk)
    except OSError:
        return None
    return b"".join(chunks)


def window_rounding() -> int | None:
    """Hyprland's window rounding (theme or looknfeel.lua), or None if unknown."""
    import ai_core
    import corners
    raw = _hyprland_query(b"j/getoption decoration:rounding")
    if raw is None:
        raw, _ok = ai_core.read_bounded_output(["hyprctl", "getoption", "decoration:rounding", "-j"],
                                               max_bytes=4096, timeout=2.0)
    # {"option": "decoration:rounding", "int": 0, "set": true}
    return corners.parse_rounding(raw or "")


def rounding_source(theme_lua: Path = THEME_LUA, user_lua: Path = USER_LUA) -> str:
    """Who set the windows' rounding: "user" (looknfeel.lua), "theme" or "default" (Omarchy's: square)."""
    import corners
    if corners.lua_rounding(user_lua) is not None:
        return "user"
    if corners.lua_rounding(theme_lua) is not None:
        return "theme"
    return "default"


def corners_note() -> str:
    """Where the windows' rounding comes from, for the Corners setting."""
    from i18n import _
    source = rounding_source()
    if source == "user":
        origin = _("Your windows' rounding is set in ~/.config/hypr/looknfeel.lua.")
    elif source == "theme":
        origin = _("Your windows' rounding is set by the current theme (its hyprland.lua).")
    else:
        origin = _("Your windows use Omarchy's default rounding (square); a theme or "
                   "~/.config/hypr/looknfeel.lua can change it.")
    return _("Corners of the window, its cards and the map's boxes. “Like windows” follows Hyprland's "
             "window rounding.") + " " + origin


def stays_in_background() -> bool:
    """Closing hides the window: the bar icon opens it again at once."""
    return True


def header_moves_window() -> bool:
    """Hyprland tiles the window: a header that dragged it would only make it float."""
    return False


def decorated() -> bool:
    return False            # Hyprland draws the border; the header is the app's own


# --- windows --------------------------------------------------------------------------------------

def attach_window(window):
    """Nothing to keep: windows are found by app id through Hyprland."""

def _selector(app_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", app_id):
        raise ValueError("not an app id")
    return f"class:^({app_id.replace('.', '[.]')})$"


def _dispatch(lua: str) -> bool:
    hyprctl = shutil.which("hyprctl")
    if hyprctl is None:
        return False
    try:
        result = subprocess.run([hyprctl, "dispatch", lua], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _clients() -> list:
    import ai_core
    hyprctl = shutil.which("hyprctl")
    if hyprctl is None:
        return []
    out, ok = ai_core.read_bounded_output([hyprctl, "clients", "-j"], max_bytes=4 * 1024 * 1024, timeout=3)
    try:
        clients = json.loads(out) if ok else []
    except ValueError:
        return []
    return [c for c in clients if isinstance(c, dict)] if isinstance(clients, list) else []


def own_window(clients: list, app_id: str) -> dict | None:
    return next((c for c in clients if c.get("class") == app_id and c.get("pid") == os.getpid()), None)


def shares_workspace(clients: list, own: dict) -> bool:
    workspace = (own.get("workspace") or {}).get("id")
    return any(c is not own and (c.get("workspace") or {}).get("id") == workspace and c.get("mapped", True)
               for c in clients)


def focus_window(app_id: str) -> bool:
    """present() alone does not switch workspaces under Hyprland: ask it to focus our window."""
    return _dispatch(f'hl.dsp.focus({{ window = "{_selector(app_id)}" }})')


def _parent(pid: int) -> int:
    """The parent of one of this user's processes (0: unknown, or not this user's)."""
    try:
        if os.stat(f"/proc/{pid}").st_uid != os.getuid():
            return 0
        with open(f"/proc/{pid}/stat", "rb") as handle:
            stat = handle.read(4096)
        return int(stat[stat.rindex(b")") + 2:].split()[1])    # after "(comm)": state, ppid
    except (OSError, ValueError, IndexError):
        return 0


def focus_process(pids: list[int]) -> bool:
    """Bring the window of one of these programs to the front: the window whose process is
    the program or one of its parents (the terminal a TUI runs in), switching workspaces."""
    windows = {c.get("pid") for c in _clients() if isinstance(c.get("pid"), int)}
    for pid in pids[:20]:
        for _step in range(32):
            if pid <= 1:
                break
            if pid in windows:
                return _dispatch(f'hl.dsp.focus({{ window = "pid:{int(pid)}" }})')
            pid = _parent(pid)
    return False


def windows() -> dict:
    """{process id: title} of Hyprland's windows (a terminal's title names what runs in it)."""
    return {c["pid"]: str(c.get("title") or "")[:200] for c in _clients() if isinstance(c.get("pid"), int)}


def is_maximized(app_id: str) -> bool | None:
    """Whether Hyprland shows our window maximized or full screen (None: unknown)."""
    own = own_window(_clients(), app_id)
    return None if own is None else bool(own.get("fullscreen"))


def maximize(app_id: str) -> bool:
    """Over the other windows of its workspace."""
    return _dispatch(f'hl.dsp.window.fullscreen({{ mode = "maximized", window = "{_selector(app_id)}" }})')



# --- apps and terminals ---------------------------------------------------------------------------

def launch_app(argv: list) -> bool:
    """An app window of its own (LM Studio), started as Omarchy starts apps (uwsm-app)."""
    launcher = shutil.which("uwsm-app")
    try:
        subprocess.Popen(([launcher, "--"] if launcher else []) + [str(a) for a in argv], cwd=str(Path.home()),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError:
        return False
    return True


def open_terminal(argv: list, cwd: str) -> bool:
    """A program in the user's terminal, a new window (no vte4 for the pane inside)."""
    launcher = shutil.which("xdg-terminal-exec")
    if launcher is None:
        return False
    try:
        subprocess.Popen([launcher, *argv], cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return False
    return True


def run_in_terminal(argv: list, env: dict | None = None) -> bool:
    """A fixed command in a terminal window of its own that waits at the end (Omarchy's
    floating terminal), else xdg-terminal-exec. The floating terminal runs its argument as one
    line through bash, so every word is quoted here (shlex): no word can become shell syntax."""
    import shlex
    env = env or {}
    words = [f"{k}={v}" for k, v in env.items()]
    floating = shutil.which(FLOATING_TERMINAL)
    if floating:
        launcher = [floating, shlex.join((["env", *words] if words else []) + [str(a) for a in argv])]
    elif shutil.which("xdg-terminal-exec"):
        launcher = [shutil.which("xdg-terminal-exec"), *(["env", *words] if words else []), *argv]
    else:
        return False
    try:
        subprocess.Popen(launcher, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return False
    return True


def run_steps_in_terminal(steps: list) -> bool:
    """Several fixed commands, one after the other while each succeeds, in one terminal window that
    waits at the end. They run as one line through bash, so every word is quoted here (shlex)."""
    import shlex
    steps = [list(map(str, s)) for s in steps if s]
    if not steps:
        return False
    floating = shutil.which(FLOATING_TERMINAL)
    if floating:
        launcher = [floating, " && ".join(shlex.join(s) for s in steps)]
    elif shutil.which("xdg-terminal-exec"):
        launcher = [shutil.which("xdg-terminal-exec"), "bash", "-c", " && ".join(shlex.join(s) for s in steps)]
    else:
        return False
    try:
        subprocess.Popen(launcher, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return False
    return True


# --- texts that name the desktop -------------------------------------------------------------------

def text(key: str) -> str:
    """A translated text that says how things are done on this desktop."""
    from i18n import _
    return {
        "install_agent": _("Install it from Omarchy's menu: Setup → Defaults → Agent."),
        "install_agent_first": _("Install one from Omarchy's menu: Setup → Defaults → Agent (Claude Code, "
                                 "Codex or OpenCode to start with)."),
        "install_agent_short": _("Omarchy's menu: Setup → Defaults → Agent."),
        "install_coding_agent": _("Claude Code, Codex or OpenCode: Omarchy's menu → Setup → Defaults → Agent."),
        "install_terminal": _("Install the terminal launcher Omarchy uses:"),
    }[key]


def launcher_entry() -> bool:
    """The plugin offers its own entry in Omarchy's app launcher (Settings › Omarchy app)."""
    return True


# --- packages -------------------------------------------------------------------------------------

def install_command(package: str) -> str:
    """The command that installs vte4 or xdg-terminal-exec (Omarchy is Arch: pacman)."""
    return f"sudo pacman -S {package}" if package in ("vte4", "xdg-terminal-exec") else ""


# --- agents ---------------------------------------------------------------------------------------

def agent_catalog():
    """Omarchy's agent menu, in its order (None: unreadable, use the built-in list)."""
    import agents
    found = agents.catalog(AGENT_MENU)
    return found if found != list(agents.FALLBACK) else None


def default_agent() -> str:
    import agents
    return agents.default_agent(DEFAULT_AGENT_FILE)


def usage_dir() -> Path:
    return USAGE_DIR


def has_usage_collector(command: str) -> bool:
    if not re.fullmatch(r"[a-z0-9-]{1,64}", command):
        return False
    root = Path(os.environ.get("OMARCHY_PATH") or "/usr/share/omarchy")
    return any((d / f"omarchy-agent-usage-{command}").is_file() for d in (root / "bin", Path("/usr/bin")))


def refresh_usage(command: str) -> bool:
    """Ask Omarchy for this agent's usage (blocking; run it on a thread)."""
    if not has_usage_collector(command):
        return False
    try:
        result = subprocess.run(["omarchy-agent-usage-update", command], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=USAGE_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0
