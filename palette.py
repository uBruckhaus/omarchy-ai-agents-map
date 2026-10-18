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
    """The colour of a local LLM used from outside the map: magenta or cyan, whichever stands
    out more from the map's other colours (the accent of agents' lights, green skills lines,
    yellow MCP lines, red errors)."""
    used = [_rgb(colors.get(name, "")) for name in ("accent", "green", "yellow", "red")]

    def apart(color: str) -> int:
        return min(sum((a - b) ** 2 for a, b in zip(_rgb(color), other)) for other in used)
    return max((colors.get("magenta", ""), colors.get("cyan", "")), key=apart)


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
