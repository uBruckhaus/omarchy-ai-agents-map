"""Folders the user added on Setup (Add folder…) where MCP servers and skills are looked for too,
beside the places mcp_scan and skills_scan know by themselves: e.g. a shared disk holding unpacked
apps with an MCP server, or a folder of the user's own skills. Settings mcp_folders and
skill_folders (~-paths); a folder that is gone (a disk not mounted) is left out, not forgotten."""

from __future__ import annotations

import os
from pathlib import Path

KEYS = {"mcp": "mcp_folders", "skills": "skill_folders"}
MAX_FOLDERS = 50


def saved(kind: str) -> list[str]:
    """The folders as stored (~-paths), in the order they were added."""
    import ai_core
    value = ai_core.setting(KEYS[kind])
    return [p for p in value if isinstance(p, str) and p.strip()][:MAX_FOLDERS] if isinstance(value, list) else []


def folders(kind: str) -> list[Path]:
    """The added folders that are there now."""
    found = []
    for text in saved(kind):
        path = Path(os.path.expanduser(text))
        if path.is_absolute() and path.is_dir() and path not in found:
            found.append(path)
    return found


def with_change(kind: str, add=(), drop=()) -> list[str]:
    """The stored list with `add` added and `drop` taken out (paths compared after ~)."""
    import inventory
    gone = {os.path.realpath(os.path.expanduser(str(p))) for p in drop}
    kept = [p for p in saved(kind) if os.path.realpath(os.path.expanduser(p)) not in gone]
    for path in add:
        text = inventory.tilde(str(path))
        if os.path.realpath(os.path.expanduser(text)) not in {os.path.realpath(os.path.expanduser(k)) for k in kept}:
            kept.append(text)
    return kept[:MAX_FOLDERS]
