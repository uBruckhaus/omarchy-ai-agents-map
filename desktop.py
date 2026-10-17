"""Everything that depends on the desktop, behind one small interface.

The rest of AI Agents Map asks this module, never Omarchy or Hyprland directly:
where its own files live, the theme's colours and window rounding, window
actions (focus, maximize, make room for the map), terminals, the agents the
desktop offers and their usage records.

Two implementations: desktop_omarchy (Omarchy, Hyprland) and desktop_generic
(any other desktop: GNOME, KDE, …; Asterism). current() picks one at start:
ASTERISM_DESKTOP=omarchy|generic decides when set (for testing), else Omarchy
when its files are there (/usr/share/omarchy or $OMARCHY_PATH), else generic.

The interface (each implementation provides all of it):

    NAME                          "omarchy" or "generic"
    app_name() -> str             "AI Agents Map" (the Omarchy plugin) or "Asterism"
    app_id() -> str               the GTK application id, window class and icon name
    config_dir() -> Path          the app's own settings folder
    theme_dir() -> Path | None    a folder to watch for theme changes (None: none)
    theme_colors() -> dict        the theme's colours as a TOML-like dict ({} when unknown)
    window_rounding() -> int|None the windows' corner radius, None when unknown
    rounding_source() -> str      who set it: "user", "theme", "default" or "desktop"
    corners_note() -> str         the Corners setting's explanation (translated)
    decorated() -> bool           whether the desktop draws the window's title bar
    stays_in_background() -> bool whether closing only hides the window (else it quits when nothing runs in it)
    attach_window(window)         the app's Gtk.Window, once it exists
    focus_window(app_id) -> bool  bring the app's window to the front (switching workspaces)
    focus_process(pids) -> bool   bring the window of one of these programs (or of a parent: its terminal)
                                  to the front; False when the desktop cannot
    is_maximized(app_id) -> bool|None
    widen(app_id) -> bool         room for the map: move to a free workspace, else maximize
    maximize(app_id) -> bool      maximize where it is
    open_terminal(argv, cwd) -> bool              a program in the user's terminal (a new window)
    run_in_terminal(command) -> bool              a fixed command in a terminal that waits at the end
    agent_catalog() -> list[(command, label, icon, icon font)] | None   None: use the built-in list
    default_agent() -> str        the desktop's default agent command ("" when none)
    usage_dir() -> Path | None    where usage records are (None: the desktop keeps none)
    has_usage_collector(command) -> bool
    refresh_usage(command) -> bool                ask the desktop for fresh usage (blocking)
    install_command(package) -> str               the command that installs "vte4" or "xdg-terminal-exec" ("" unknown)
    text(key) -> str              a translated text that says how something is done on this desktop:
                                  install_agent, install_agent_first, install_agent_short,
                                  install_coding_agent, install_terminal, widen_tip
    launcher_entry() -> bool      whether the app offers its own app-launcher entry (Settings)
"""

import os
from pathlib import Path

OMARCHY_ROOT = Path("/usr/share/omarchy")


def detect(environ=None) -> str:
    """"omarchy" or "generic"."""
    environ = os.environ if environ is None else environ
    wanted = environ.get("ASTERISM_DESKTOP", "")
    if wanted in ("omarchy", "generic"):
        return wanted
    # Omarchy's own folder is enough: the watcher service (systemd --user) may not see Hyprland's variables.
    omarchy = Path(environ.get("OMARCHY_PATH") or OMARCHY_ROOT)
    return "omarchy" if omarchy.is_dir() else "generic"


_current = None


def current():
    """The implementation for this desktop (chosen once)."""
    global _current
    if _current is None:
        if detect() == "omarchy":
            import desktop_omarchy as chosen
        else:
            import desktop_generic as chosen
        _current = chosen
    return _current


def __getattr__(name):
    """desktop.<anything in the interface> → the current implementation's."""
    if name.startswith("__"):
        raise AttributeError(name)
    return getattr(current(), name)
