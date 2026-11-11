"""The MCP servers AI Agents Map knows, used by an agent or not.

A server stays listed (on the map and in the cards) after its last agent
lost it: drawing a line to it again sets it up from here. Removing it for
good (the card's Remove) takes it from every agent and from this list.

mcp-servers.json in desktop.config_dir() (~/.config/omarchy/ai-agents-map), readable only by you:
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
    """key (name, or name~2 for another server of that name) -> entry (mcp_ctl.normal shape)."""
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
            if mcp_ctl.LIBRARY_KEY_RE.fullmatch(str(name)):
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
    """Add the servers agents use now (the map's id: name or name~2 -> entry)
    that the list lacks or has differently. Written straight away: it only grows
    from what is set up. Each server keeps the key that holds the same server
    (mcp_ctl.identity); a new one takes its name when it is the first of that
    name and no saved server holds the name (used or not), else the next free
    name~N. Only an update (mcp_update) rewrites a saved server."""
    import mcp_ctl
    servers = load()
    merged = dict(servers)
    claimed, pending = set(), []
    for ident, entry in used.items():
        if entry is None:
            continue
        name, same = mcp_ctl.base_name(ident), mcp_ctl.identity(entry)
        keys = [k for k in sorted(merged) if mcp_ctl.base_name(k) == name and k not in claimed
                and mcp_ctl.identity(merged[k]) == same]
        key = ident if ident in keys else (keys[0] if keys else None)
        if key is None:
            pending.append((ident, name, entry))
        else:
            claimed.add(key)
            merged[key] = entry
    for ident, name, entry in pending:
        key, n = name, 2
        if ident != name or name in merged:     # a saved server of another identity keeps its key
            key = f"{name}~{n}"
            while key in merged or key in claimed:
                n += 1
                key = f"{name}~{n}"
        claimed.add(key)
        merged[key] = entry
    if merged == servers:
        return
    path = library_file()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    connect._write(path, text_with(dict(list(merged.items())[:MAX_SERVERS])))
