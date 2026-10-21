"""AI Agents Map - Theme colors from the desktop, or Adwaita (Settings › Look; ai_core.look_colors())."""


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
    "magenta": "#cba6f7",
    "cyan": "#94e2d5",
}


def _rgb(hex_code: str) -> tuple:
    text = (hex_code or "").lstrip("#")
    try:
        return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4)) if len(text) >= 6 else (136, 136, 136)
    except ValueError:
        return (136, 136, 136)


def outside_color(colors: dict) -> str:
    """The colour of a local LLM used from outside the map: cyan (magenta, violet, marks agents
    on different models of one local LLM; the accent, green skills lines, yellow MCP lines and
    red errors are taken too)."""
    return colors.get("cyan", "") or DEFAULT_COLORS["cyan"]


class ThemePalette:
    """Expose the desktop theme's colors to the AI Agents Map."""

    def __init__(self):
        self.colors = {**DEFAULT_COLORS, "outside": outside_color(DEFAULT_COLORS)}
        self.reload()

    def reload(self):
        import ai_core
        theme = ai_core.look_colors()
        colors = {key: theme.get(key, fallback) for key, fallback in DEFAULT_COLORS.items()}
        if "muted" not in theme:
            colors["muted"] = theme.get("dark_foreground") or theme.get("color8") or DEFAULT_COLORS["muted"]
        colors["outside"] = outside_color(colors)
        if colors == self.colors:
            return False
        self.colors = colors
        return True
