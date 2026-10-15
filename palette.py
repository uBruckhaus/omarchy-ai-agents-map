"""AI Agents Map - Theme colors from Omarchy."""

from pathlib import Path
import tomllib

DEFAULT_COLORS = {
    "background": "#1e1e2e",
    "foreground": "#cdd6f4",
    "accent": "#89b4fa",
    "selection": "#45475a",
    "muted": "#888894",
    "red": "#f04452",
    "yellow": "#f9e2af",
    "orange": "#fab387",
    "green": "#a6e3a1",
}


class ThemePalette:
    """Expose the current Omarchy theme colors to the AI Agents Map."""

    def __init__(self):
        self.colors = DEFAULT_COLORS.copy()
        self.reload()

    def reload(self):
        try:
            path = Path.home() / ".local/state/omarchy/current/theme/colors.toml"
            theme = tomllib.loads(path.read_text())
            colors = {key: theme.get(key, fallback) for key, fallback in DEFAULT_COLORS.items()}
            if "muted" not in theme:
                colors["muted"] = theme.get("dark_foreground") or theme.get("color8") or DEFAULT_COLORS["muted"]
        except (OSError, ValueError):
            colors = DEFAULT_COLORS.copy()
        if colors == self.colors:
            return False
        self.colors = colors
        return True
