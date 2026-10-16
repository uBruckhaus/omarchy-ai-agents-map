"""Any other desktop (GNOME, KDE, …): the generic implementation of desktop.py's interface.

- own files: $XDG_CONFIG_HOME/asterism (~/.config/asterism)
- theme: libadwaita's colours for the system's light or dark style and accent
  colour (`gsettings`, org.gnome.desktop.interface color-scheme / accent-color;
  light and blue when unknown); windows have libadwaita's 12 px corners
- windows: GTK itself (the window is handed over with attach_window); the
  desktop draws the title bar
- terminals: xdg-terminal-exec, $TERMINAL, then the usual ones (Console, Ptyxis,
  GNOME Terminal, Konsole, Xfce Terminal, foot, Alacritty, kitty, WezTerm, xterm)
- agents: the built-in list (agents.FALLBACK), no default agent, no usage
  records (agents' own stores still count, agent_status)
- install hints: the command of the package manager found (apt, dnf, zypper,
  pacman), with that distribution's package name.

Programs are run as argv lists with bounded output; a command that waits at the end
runs through `bash -c` with every word quoted (shlex.join).
"""

import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

NAME = "generic"
HOME = Path.home()
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or HOME / ".config") / "asterism"
RADIUS = 12          # libadwaita's window corners

# libadwaita's palette (Adwaita named colours), light and dark.
ACCENTS = {"blue": "#3584e4", "teal": "#2190a4", "green": "#3a944a", "yellow": "#c88800", "orange": "#ed5b00",
           "red": "#e62d42", "pink": "#d56199", "purple": "#9141ac", "slate": "#6f8396"}
LIGHT = {"background": "#fafafb", "foreground": "#323233", "muted": "#77767b", "selection": "#dedddf",
         "red": "#e01b24", "yellow": "#e5a50a", "orange": "#ff7800", "green": "#2ec27e"}
DARK = {"background": "#222226", "foreground": "#ffffff", "muted": "#9a9996", "selection": "#3a3a3e",
        "red": "#ff7b63", "yellow": "#f8e45c", "orange": "#ffa348", "green": "#8ff0a4"}

# Terminals: (program, how argv follows it).
TERMINALS = (("xdg-terminal-exec", []), ("kgx", ["--"]), ("ptyxis", ["--"]), ("gnome-terminal", ["--"]),
             ("konsole", ["-e"]), ("xfce4-terminal", ["-x"]), ("foot", []), ("alacritty", ["-e"]),
             ("kitty", []), ("wezterm", ["start", "--"]), ("xterm", ["-e"]))

# Package names per package manager.
PACKAGES = {"vte4": {"apt": "gir1.2-vte-3.91", "dnf": "vte291-gtk4", "zypper": "typelib-1_0-Vte-3_91",
                     "pacman": "vte4"},
            "xdg-terminal-exec": {"pacman": "xdg-terminal-exec"}}
MANAGERS = (("apt", "sudo apt install {0}"), ("dnf", "sudo dnf install {0}"), ("zypper", "sudo zypper install {0}"),
            ("pacman", "sudo pacman -S {0}"))

_window = None


# --- own files ------------------------------------------------------------------------------------

def config_dir() -> Path:
    return CONFIG_DIR


# --- theme ----------------------------------------------------------------------------------------

def _gsetting(key: str) -> str:
    import ai_core
    gsettings = shutil.which("gsettings")
    if gsettings is None:
        return ""
    out, ok = ai_core.read_bounded_output([gsettings, "get", "org.gnome.desktop.interface", key],
                                          max_bytes=1024, timeout=2)
    return out.decode("utf-8", "replace").strip().strip("'") if ok else ""


def theme_dir():
    return None             # the style is read on start (and on reload)


def theme_colors(path=None) -> dict:
    """libadwaita's colours for the system style; path: a TOML file instead (tests)."""
    if path is not None:
        import desktop_omarchy
        return desktop_omarchy.theme_colors(path)
    dark = _gsetting("color-scheme") == "prefer-dark"
    colors = dict(DARK if dark else LIGHT)
    colors["accent"] = ACCENTS.get(_gsetting("accent-color"), ACCENTS["blue"])
    return colors


def window_rounding() -> int:
    return RADIUS


def rounding_source() -> str:
    return "desktop"


def corners_note() -> str:
    from i18n import _
    return _("Your windows have the desktop's rounded corners.")


def decorated() -> bool:
    return True             # the desktop (or GTK) draws the title bar with its buttons


# --- windows --------------------------------------------------------------------------------------

def attach_window(window):
    global _window
    _window = window


def _on_window(action):
    from gi.repository import GLib
    if _window is None:
        return False
    GLib.idle_add(lambda: action(_window) and False)
    return True


def focus_window(app_id: str) -> bool:
    return _on_window(lambda w: w.present())


def is_maximized(app_id: str):
    return _window.is_maximized() if _window is not None else None


def widen(app_id: str) -> bool:
    return maximize(app_id)


def maximize(app_id: str) -> bool:
    return _on_window(lambda w: w.maximize())


# --- terminals ------------------------------------------------------------------------------------

def terminal() -> list | None:
    """The terminal to start programs in: [program, argument before argv…], or None."""
    own = os.environ.get("TERMINAL", "")
    if own and re.fullmatch(r"[A-Za-z0-9._+-]{1,64}", own) and shutil.which(own):
        known = dict(TERMINALS).get(own, ["-e"])
        return [shutil.which(own), *known]
    for program, follow in TERMINALS:
        path = shutil.which(program)
        if path:
            return [path, *follow]
    return None


def open_terminal(argv: list, cwd: str) -> bool:
    launcher = terminal()
    if launcher is None:
        return False
    try:
        subprocess.Popen([*launcher, *argv], cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return False
    return True


def run_in_terminal(argv: list, env: dict | None = None) -> bool:
    """argv in a terminal that stays open until Enter (every word quoted for bash)."""
    from i18n import _
    words = (["env", *(f"{k}={v}" for k, v in env.items())] if env else []) + list(argv)
    prompt = _("Done. Press Enter to close.")
    script = f"{shlex.join(words)}; echo; read -r -p {shlex.quote(prompt)} _"
    return open_terminal(["bash", "-c", script], str(HOME))


# --- agents ---------------------------------------------------------------------------------------

def agent_catalog():
    return None             # the built-in list


def default_agent() -> str:
    return ""


def usage_dir():
    return None


def has_usage_collector(command: str) -> bool:
    return False


def refresh_usage(command: str) -> bool:
    return False


# --- packages -------------------------------------------------------------------------------------

def package_manager() -> str:
    return next((name for name, _cmd in MANAGERS if shutil.which(name)), "")


def install_command(package: str) -> str:
    """The command that installs one of the packages in PACKAGES ("" when unknown here)."""
    manager = package_manager()
    name = PACKAGES.get(package, {}).get(manager)
    template = dict(MANAGERS).get(manager)
    return template.format(name) if name and template else ""
