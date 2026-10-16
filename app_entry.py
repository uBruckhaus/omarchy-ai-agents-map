"""AI Agents Map as an app: its launcher entry (window actions: desktop.py).

The entry (~/.local/share/applications/org.omarchy.aiagentsmap.desktop) is this
plugin's own file: written when the window opens (Settings → "In the app
launcher" removes it again) and kept only while the plugin is installed:
`TryExec` points at the plugin's own launcher, so a launcher ignores the entry
once the plugin folder is gone.
"""

from __future__ import annotations

import os
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
