"""The MCP servers AI Agents Map knows, used by an agent or not.

A server stays listed (on the map and in the cards) after its last agent
lost it: drawing a line to it again sets it up from here. Removing it for
good (the card's Remove) takes it from every agent and from this list.

mcp-servers.json in desktop.config_dir() (~/.config/omarchy/ai-agents-map on Omarchy), readable only by you:
the entries are copies of the agents' own (env and headers may hold keys).
Changes go through connect.Change, so Undo covers them.
"""

import json
from pathlib import Path

import connect

HOME = Path.home()
MAX_SERVERS = 200


def library_file() -> Path:
    import desktop
    return desktop.config_dir() / "mcp-servers.json"


def load() -> dict:
    """name -> entry (mcp_ctl.normal shape)."""
    try:
        data = json.loads(connect._read(library_file()) or "{}")
    except (OSError, ValueError):
        return {}
    servers = data.get("servers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        return {}
    import mcp_ctl
    found = {}
    for name, entry in list(servers.items())[:MAX_SERVERS]:
        try:
            if mcp_ctl.NAME_RE.match(str(name)):
                found[str(name)] = mcp_ctl.check_entry(entry)
        except (ValueError, AttributeError, TypeError):
            continue
    return found


def text_with(servers: dict) -> str:
    return json.dumps({"servers": dict(sorted(servers.items()))}, indent=2, ensure_ascii=False) + "\n"


def change(name: str, entry: dict | None) -> connect.Change | None:
    """The list with `name` set to `entry` (None: taken out), as a planned change."""
    servers = load()
    if entry is None:
        if name not in servers:
            return None
        servers.pop(name)
    else:
        if servers.get(name) == entry:
            return None
        servers[name] = entry
    path = library_file()
    old = connect._read(path)
    new = text_with(servers)
    return connect.Change(path, old, new) if new != old else None


def remember(used: dict):
    """Add the servers agents use now (name -> entry) that the list lacks or
    has differently. Written straight away: it only grows from what is set up."""
    servers = load()
    merged = {**servers, **{n: e for n, e in used.items() if e is not None}}
    if merged == servers:
        return
    path = library_file()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    connect._write(path, text_with(dict(list(merged.items())[:MAX_SERVERS])))
