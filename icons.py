"""The app's own icons, so nothing depends on the desktop's icon theme or an installed icon font.

- icons/Asterism/ next to this module is a small icon theme ("Asterism", inheriting Adwaita): every
  icon name the app uses, and every glyph of its cards and map as an SVG (tools/make-glyph-icons.py).
  setup() selects it for this app only (Gtk.Settings of this process); the desktop keeps its own.
- An add-on may bring icon.svg: checked (shapes only: no scripts, links, images or styles that load
  anything) and kept in the cache, named am-addon-<id>-symbolic.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

DIR = Path(__file__).resolve().parent / "icons"
THEME = "Asterism"
THEME_DIR = DIR / THEME
ACTIONS = THEME_DIR / "symbolic/actions"
ADDON_DIR = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "ai-agents-map/addon-icons"
MAX_SVG = 64 * 1024

_KNOWN: set = set()


def glyph_name(char: str, font: str = "") -> str:
    """The icon name of a glyph (a Nerd Font one, or Omarchy's own font with font="omarchy")."""
    return f"am-{'omarchy' if font == 'omarchy' else 'glyph'}-{ord(char):x}-symbolic"


def _known() -> set:
    if not _KNOWN:
        try:
            _KNOWN.update(p.stem for p in ACTIONS.glob("*.svg"))
        except OSError:
            pass
    return _KNOWN


def name_for(text: str, font: str = "") -> str:
    """The stored icon for a glyph, an add-on icon name (font "icon"), or "" (draw the text)."""
    if font == "icon":
        return text if text.startswith("am-addon-") and path_for(text) else ""
    if not text or len(text) != 1:
        return ""
    name = glyph_name(text, font)
    return name if name in _known() else ""


def path_for(name: str) -> Path | None:
    """The SVG file of one of the app's own icons (the map draws it with librsvg)."""
    for folder in (ACTIONS, ADDON_DIR):
        path = folder / f"{name}.svg"
        if re.fullmatch(r"[a-z0-9-]{1,80}", name) and path.is_file():
            return path
    return None


def setup(display) -> None:
    """Use the app's own icons in this process: its theme first, Adwaita behind it, add-on icons."""
    from gi.repository import Gtk
    theme = Gtk.IconTheme.get_for_display(display)
    for folder in (DIR, ADDON_DIR):
        if str(folder) not in theme.get_search_path():
            theme.add_search_path(str(folder))
    if THEME_DIR.is_dir():
        Gtk.Settings.get_for_display(display).set_property("gtk-icon-theme-name", THEME)


# --- an add-on's icon.svg ----------------------------------------------------------------------------

_SHAPES = {"svg", "g", "path", "circle", "ellipse", "rect", "line", "polyline", "polygon", "defs", "title",
           "desc", "clippath", "mask", "lineargradient", "radialgradient", "stop"}


def safe_svg(data: bytes) -> bool:
    """Shapes only: no script, no element that loads or embeds anything (image, use, style,
    foreignObject), no href but an internal "#id", no url() but "#id", no entities."""
    if not isinstance(data, bytes) or not data or len(data) > MAX_SVG:
        return False
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    lowered = text.lower()
    if "<!doctype" in lowered or "<!entity" in lowered or "<?xml-stylesheet" in lowered:
        return False
    for tag in re.findall(r"<\s*([a-zA-Z][\w:.-]*)", text):
        if tag.split(":")[-1].lower() not in _SHAPES:
            return False
    for value in re.findall(r"href\s*=\s*[\"']([^\"']*)[\"']", text, re.I):
        if not value.startswith("#"):
            return False
    for value in re.findall(r"url\(\s*([^)]*)\)", text, re.I):
        if not value.strip(" '\"").startswith("#"):
            return False
    if re.search(r"\son\w+\s*=", text, re.I) or "javascript:" in lowered or "@import" in lowered:
        return False
    return "<svg" in lowered


def add_addon_icon(addon_id: str, data: bytes) -> str:
    """An add-on's verified icon.svg, when safe: written to the cache as am-addon-<id>-symbolic;
    returns that name, or "" (then the app's own icon for its kind is used)."""
    if not re.fullmatch(r"[a-z0-9-]{1,60}", addon_id) or not safe_svg(data):
        return ""
    name = f"am-addon-{addon_id}-symbolic"
    try:
        ADDON_DIR.mkdir(parents=True, exist_ok=True)
        target = ADDON_DIR / f"{name}.svg"
        tmp = ADDON_DIR / f".{name}.{os.getpid()}.tmp"
        tmp.write_bytes(data)
        os.replace(tmp, target)
    except OSError:
        return ""
    return name
