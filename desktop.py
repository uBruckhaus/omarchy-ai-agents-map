"""Everything that depends on the desktop, behind one small interface.

The rest of AI Agents Map asks this module, never Omarchy or Hyprland directly:
where its own files live, the theme's colours and window rounding, window
actions (focus, maximize, make room for the map), terminals, the agents the
desktop offers and their usage records.

Only the Omarchy desktop is implemented (desktop_omarchy). This is step 1 of
Asterism, the same app for other desktops: a generic implementation will sit
beside it and current() will pick one at start.

The interface (each implementation provides all of it):

    NAME                          "omarchy"
    config_dir() -> Path          the app's own settings folder
    theme_dir() -> Path | None    a folder to watch for theme changes (None: none)
    theme_colors() -> dict        the theme's colours as a TOML-like dict ({} when unknown)
    window_rounding() -> int|None the windows' corner radius, None when unknown
    rounding_source() -> str      who set it: "user", "theme" or "default"
    focus_window(app_id) -> bool  bring the app's window to the front (switching workspaces)
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
"""

_current = None


def current():
    """The implementation for this desktop (chosen once)."""
    global _current
    if _current is None:
        import desktop_omarchy
        _current = desktop_omarchy
    return _current


def __getattr__(name):
    """desktop.<anything in the interface> → the current implementation's."""
    if name.startswith("__"):
        raise AttributeError(name)
    return getattr(current(), name)
