"""AI Agents Map as an app: its launcher entry and bringing its window forward.

The entry (~/.local/share/applications/org.omarchy.aiagentsmap.desktop) is this
plugin's own file: written when the window opens (Settings → "In the app
launcher" removes it again) and kept only while the plugin is installed:
`TryExec` points at the plugin's own launcher, so a launcher ignores the entry
once the plugin folder is gone.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

APP_ID = "org.omarchy.aiagentsmap"
ICON_NAME = APP_ID
HERE = Path(__file__).resolve().parent
ENTRY = Path.home() / ".local/share/applications" / f"{APP_ID}.desktop"
MARK = "X-AI-Agents-Map=1"        # only an entry carrying this is ever replaced or removed


def entry_text(here: Path = HERE) -> str:
    launcher = here / "ai-agents-map-toggle"
    icon = here / "assets/ai-agents-map.svg"
    for path in (launcher, icon):
        if any(c in str(path) for c in '\n\r"\\$`%') or " " in str(path):
            raise ValueError("the plugin folder has characters a desktop entry cannot hold")
    return f"""[Desktop Entry]
Type=Application
Name=AI Agents Map
GenericName=AI setup
Comment=Coding agents, local LLMs, MCP servers and skills as cards and a map
Exec={launcher}
TryExec={launcher}
Icon={icon}
Terminal=false
StartupWMClass={APP_ID}
Categories=Development;Utility;
Keywords=AI;agents;LLM;MCP;Claude;Codex;Ollama;llama.cpp;
{MARK}
"""


def ours(path: Path = ENTRY) -> bool:
    try:
        with open(path, "rb") as handle:
            return MARK.encode() in handle.read(16384)
    except OSError:
        return False


def ensure_entry(wanted: bool = True, path: Path = ENTRY, here: Path = HERE) -> bool:
    """Write (or remove) the launcher entry. A file of the same name we did not write is left alone."""
    exists = path.exists() or path.is_symlink()
    if not wanted:
        if exists and not path.is_symlink() and ours(path):
            path.unlink()
            return True
        return False
    try:
        text = entry_text(here)
    except ValueError:
        return False
    if exists and (path.is_symlink() or not ours(path)):
        return False
    try:
        if exists and path.read_text(encoding="utf-8") == text:
            return False
    except OSError:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.new")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(tmp, path)
    return True


def focus_window() -> bool:
    """present() alone does not switch workspaces under Hyprland: ask it to focus our window."""
    hyprctl = shutil.which("hyprctl")
    if hyprctl is None:
        return False
    try:
        # Hyprland 0.55+ takes a Lua dispatcher; the selector is a fixed string.
        result = subprocess.run([hyprctl, "dispatch", f'hl.dsp.focus({{ window = "{SELECTOR}" }})'],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


SELECTOR = f"class:^({APP_ID.replace('.', '[.]')})$"


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
    import json
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


def own_window(clients: list) -> dict | None:
    return next((c for c in clients if c.get("class") == APP_ID and c.get("pid") == os.getpid()), None)


def maximized() -> bool | None:
    """Whether Hyprland shows our window maximized or full screen (None: unknown)."""
    own = own_window(_clients())
    return None if own is None else bool(own.get("fullscreen"))


def shares_workspace(clients: list, own: dict) -> bool:
    workspace = (own.get("workspace") or {}).get("id")
    return any(c is not own and (c.get("workspace") or {}).get("id") == workspace and c.get("mapped", True)
               for c in clients)


def widen() -> bool:
    """Room for the map: other windows on its workspace → to the first free workspace of this
    monitor (alone there, it gets the whole width; the view follows it). Alone already
    (floating, say) → maximized, as Omarchy's "Full width" (Super + Alt + F)."""
    clients = _clients()
    own = own_window(clients)
    if own is not None and shares_workspace(clients, own):
        moved = _dispatch(f'hl.dsp.window.move({{ workspace = "emptym", window = "{SELECTOR}" }})')
        if moved and not own.get("floating"):
            return True
    return _dispatch(f'hl.dsp.window.fullscreen({{ mode = "maximized", window = "{SELECTOR}" }})')


def maximize_here() -> bool:
    """Settings → "Maximize where it is": over the other windows of its workspace."""
    return _dispatch(f'hl.dsp.window.fullscreen({{ mode = "maximized", window = "{SELECTOR}" }})')
