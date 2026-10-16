"""AI Agents Map - Theme colors from the desktop (desktop.theme_colors())."""

import desktop

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
    """Expose the desktop theme's colors to the AI Agents Map."""

    def __init__(self):
        self.colors = DEFAULT_COLORS.copy()
        self.reload()

    def reload(self):
        theme = desktop.theme_colors()
        colors = {key: theme.get(key, fallback) for key, fallback in DEFAULT_COLORS.items()}
        if "muted" not in theme:
            colors["muted"] = theme.get("dark_foreground") or theme.get("color8") or DEFAULT_COLORS["muted"]
        if colors == self.colors:
            return False
        self.colors = colors
        return True
