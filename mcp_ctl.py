"""Connect an agent to an MCP server another agent already has (a line drawn
on the map), or remove it from the agent.

The server's definition is read from an agent that has it (command, args,
env or url, headers), turned into the target agent's own format and written
with connect.plan's machinery: diff preview, backup, Undo. Values of env and
headers never show in the preview (connect.Plan.secrets).

Claude Code keeps its MCP servers in ~/.claude.json, which running sessions
rewrite all the time: it is changed through `claude mcp add-json/remove
--scope user` instead (Undo runs the opposite command).
"""

import json
import os
import re
import shutil
from pathlib import Path

import agent_profiles
import connect

HOME = Path.home()
SUPPORTED = ("claude", "codex", "copilot", "crush", "cursor-agent", "gemini", "grok", "hermes", "muse", "omp",
             "openclaw", "opencode", "pi", "dsh")
# Agents whose MCP servers are set up only in their own app (no settings file to write).
IN_APP = {"bionic": "Settings › Connected apps"}
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def supported(agent: str) -> bool:
    """Built in, or a profile looked up on the web (agent_profiles)."""
    return agent in SUPPORTED or agent_profiles.mcp_profile(agent) is not None


def _expand(path: str) -> Path:
    return HOME / path[2:] if path.startswith("~/") else Path(path)


def paths_for(agent: str) -> list[Path]:
    if agent not in SUPPORTED and agent != "lmstudio":
        profile = agent_profiles.mcp_profile(agent)
        return [_expand(profile["file"])] if profile else []
    return {"codex": [HOME / ".codex/config.toml"],
            "copilot": [HOME / ".copilot/mcp-config.json"],
            "cursor-agent": [HOME / ".cursor/mcp.json"],
            "gemini": [HOME / ".gemini/settings.json"],
            "hermes": [HOME / ".hermes/config.yaml"],
            "opencode": [HOME / ".config/opencode/opencode.json"],
            "pi": [HOME / ".pi/agent/mcp.json"],
            "omp": [HOME / ".omp/agent/mcp.json"],
            "crush": [HOME / ".config/crush/crush.json"],
            "grok": [HOME / ".grok/config.toml"],
            "muse": [HOME / ".config/muse/settings.json"],
            "openclaw": [HOME / ".openclaw/openclaw.json"],
            "lmstudio": [HOME / ".lmstudio/mcp.json"],
            "dsh": [HOME / ".dsh/profiles/web/cordis.patch.yml"]}.get(agent, [])


# --- reading: every agent's entries as one shape -------------------------------------------

def _strs(value) -> list:
    return [str(v) for v in value] if isinstance(value, list) else []


def _map(value) -> dict:
    return {str(k): str(v) for k, v in value.items()} if isinstance(value, dict) else {}


def normal(entry: dict) -> dict | None:
    """{"transport": stdio|http|sse, "command", "args", "env", "url", "headers"} or None."""
    if not isinstance(entry, dict):
        return None
    kind = str(entry.get("type") or entry.get("transport") or "").lower()
    url = entry.get("httpUrl") or entry.get("url") or entry.get("serverUrl")
    if isinstance(url, str) and url:
        transport = "sse" if kind == "sse" or (not entry.get("httpUrl") and kind == "" and url.rstrip("/").endswith("/sse")) \
            else "http"
        return {"transport": transport, "command": "", "args": [], "env": {}, "url": url,
                "headers": _map(entry.get("headers") or entry.get("http_headers"))}
    command = entry.get("command")
    args = _strs(entry.get("args"))
    if isinstance(command, list):           # opencode: one list
        command, args = (str(command[0]) if command else ""), _strs(command[1:])
    if not isinstance(command, str) or not command:
        return None
    return {"transport": "stdio", "command": command, "args": args,
            "env": _map(entry.get("env") or entry.get("environment")), "url": "", "headers": {}}


def _read_json(path: Path, limit: int = connect.MAX_BYTES) -> dict:
    try:
        if path.stat().st_size > limit:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _dig(data, key: str):
    for part in key.split("."):
        data = data.get(part) if isinstance(data, dict) else None
    return data


def _profile_entries(profile: dict):
    path = _expand(profile["file"])
    if profile["format"] == "json":
        return _dig(_read_json(path), profile["key"])
    if profile["format"] == "toml":
        import tomllib
        try:
            return tomllib.loads(connect._read(path)).get(profile["key"])
        except (OSError, ValueError):
            return None
    try:
        import yaml
        return (yaml.safe_load(connect._read(path)) or {}).get(profile["key"])
    except Exception:
        return None


def raw_entries(agent: str) -> dict:
    """name -> the agent's own entry (as stored), enabled or not."""
    if agent not in SUPPORTED and agent != "lmstudio":
        profile = agent_profiles.mcp_profile(agent)
        found = _profile_entries(profile) if profile else None
    elif agent == "claude":
        found = _read_json(HOME / ".claude.json", 8 * 1024 * 1024).get("mcpServers")
    elif agent in ("codex", "grok"):
        import tomllib
        try:
            found = tomllib.loads(connect._read(paths_for(agent)[0])).get("mcp_servers")
        except (OSError, ValueError):
            found = None
    elif agent == "dsh":
        try:
            import dsh_ctl
            found = dsh_ctl.mcp_servers(connect._read(paths_for(agent)[0]))
        except Exception:       # no PyYAML, or a patch it cannot read
            found = None
    elif agent == "hermes":
        try:
            import yaml
            found = (yaml.safe_load(connect._read(paths_for(agent)[0])) or {}).get("mcp_servers")
        except Exception:       # no PyYAML, or a file it cannot read
            found = None
    elif agent in ("opencode", "crush"):
        found = _read_json(paths_for(agent)[0]).get("mcp")
    elif agent == "muse":
        found = _read_json(paths_for(agent)[0]).get("mcp_servers")
    elif agent == "openclaw":
        mcp = _read_json(paths_for(agent)[0]).get("mcp")
        found = mcp.get("servers") if isinstance(mcp, dict) else None
    elif paths_for(agent):
        found = _read_json(paths_for(agent)[0]).get("mcpServers")
    else:
        found = None
    return {str(k): v for k, v in found.items() if isinstance(v, dict)} if isinstance(found, dict) else {}


def entry_from(sources, name: str):
    """(agent, normal entry) of the first source agent that has the server."""
    for agent in sources:
        entry = normal(raw_entries(agent).get(name))
        if entry is not None:
            return agent, entry
    return None, None


def is_connected(agent: str, name: str) -> bool:
    return name in raw_entries(agent)


# --- writing: one entry in the target agent's format -----------------------------------------

def from_profile(profile: dict, e: dict) -> dict:
    """An entry in the shape a looked-up profile describes."""
    item = {}
    if e["transport"] == "stdio":
        if profile["type_field"] and profile["stdio_type"]:
            item[profile["type_field"]] = profile["stdio_type"]
        if profile["command_list"]:
            item["command"] = [e["command"], *e["args"]]
        else:
            item["command"], item["args"] = e["command"], e["args"]
        if e["env"]:
            item[profile["env_field"]] = e["env"]
        return item
    if profile["type_field"] and profile["http_type"]:
        item[profile["type_field"]] = "sse" if e["transport"] == "sse" and profile["http_type"] != "remote" \
            else profile["http_type"]
    item[profile["url_field"]] = e["url"]
    if e["headers"]:
        item[profile["headers_field"]] = e["headers"]
    return item


def native(agent: str, e: dict) -> dict:
    stdio = e["transport"] == "stdio"
    if agent not in SUPPORTED:
        return from_profile(agent_profiles.mcp_profile(agent), e)
    if agent == "dsh":
        import dsh_ctl
        return dsh_ctl.mcp_config(e)
    if agent in ("muse", "openclaw", "crush"):
        if stdio:
            item = {"command": e["command"], "args": e["args"]}
            if e["env"]:
                item["env"] = e["env"]
            kind = {"muse": "transport", "openclaw": "", "crush": "type"}[agent]
            return {kind: "stdio", **item} if kind else item
        remote = {"muse": "streamable_http", "openclaw": "streamable-http", "crush": "http"}[agent]
        if e["transport"] == "sse":
            remote = "sse"
        item = {"transport" if agent != "crush" else "type": remote, "url": e["url"]}
        if e["headers"]:
            item["headers"] = e["headers"]
        return item
    if agent == "opencode":
        if stdio:
            item = {"type": "local", "command": [e["command"], *e["args"]], "enabled": True}
            if e["env"]:
                item["environment"] = e["env"]
            return item
        item = {"type": "remote", "url": e["url"], "enabled": True}
        if e["headers"]:
            item["headers"] = e["headers"]
        return item
    if stdio:
        item = {"command": e["command"], "args": e["args"]}
        if e["env"]:
            item["env"] = e["env"]
        if agent == "claude":
            item = {"type": "stdio", **item, "env": e["env"]}
        elif agent == "copilot":
            item = {"type": "local", **item, "tools": ["*"]}
        return item
    if agent == "gemini":
        item = {"httpUrl" if e["transport"] == "http" else "url": e["url"]}
    elif agent in ("claude", "copilot", "omp"):
        item = {"type": e["transport"], "url": e["url"]}
    else:
        item = {"url": e["url"]}
    if e["headers"]:
        item["http_headers" if agent == "codex" else "headers"] = e["headers"]
    if agent == "copilot":
        item["tools"] = ["*"]
    return item


def _json_text(text: str, key: str, name: str, item: dict | None) -> str:
    data = connect._json(text)
    servers = data.setdefault(key, {})
    if not isinstance(servers, dict):
        raise ValueError(f"{key} is not an object")
    if item is None:
        servers.pop(name, None)
        if not servers:
            data.pop(key)
    else:
        servers[name] = item
    return connect._dump(data)


def _json_dotted(text: str, key: str, name: str, item: dict | None) -> str:
    """Like _json_text, for a dotted key ("mcp.servers")."""
    data = connect._json(text)
    *parents, last = key.split(".")
    node = data
    for part in parents:
        node = node.setdefault(part, {})
        if not isinstance(node, dict):
            raise ValueError(f"{part} is not an object")
    inner = _json_text(json.dumps({last: node.get(last, {})}), last, name, item)
    value = json.loads(inner).get(last)
    if value:
        node[last] = value
    else:
        node.pop(last, None)
    return connect._dump(data)


def _toml_key(key: str) -> str:
    return key if re.fullmatch(r"[A-Za-z0-9_-]+", key) else json.dumps(key)


def _toml_value(value) -> str:
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{_toml_key(k)} = {json.dumps(v)}" for k, v in value.items()) + " }"
    return json.dumps(value, ensure_ascii=False)


def codex_text(text: str, name: str, item: dict | None, table: str = "mcp_servers") -> str:
    """[<table>.<name>] and its sub-tables go; the new table is appended."""
    header = rf"\[{re.escape(table)}\.(?:{re.escape(name)}|{re.escape(json.dumps(name))})(?:\.[^\]]*)?\]"
    out, skipping = [], False
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith("["):
            skipping = re.match(rf"\s*{header}\s*(#.*)?$", line.rstrip("\n")) is not None
        if not skipping:
            out.append(line)
    text = "".join(out)
    if item is not None:
        block = f"[{table}.{_toml_key(name)}]\n" + "".join(f"{k} = {_toml_value(v)}\n" for k, v in item.items())
        text = text.rstrip("\n") + ("\n\n" if text.strip() else "") + block
    return text


def hermes_text(text: str, name: str, item: dict | None, key: str = "mcp_servers") -> str:
    """The `mcp_servers:` (or `key:`) block, edited as text (flow values are valid YAML)."""
    entry = "".join(f"    {k}: {json.dumps(v, ensure_ascii=False)}\n" for k, v in (item or {}).items())
    entry = f"  {name}:\n{entry}" if item is not None else ""
    block = connect._top_block(text, key)
    if block is None:
        if item is None:
            return text
        return text + ("" if text.endswith("\n") or not text else "\n") + f"{key}:\n" + entry
    start, end = block
    body = text[start:end]
    head, _nl, rest = body.partition("\n")
    rest = re.sub(rf"^  {re.escape(name)}:.*\n(?:(?:    |\t\t).*\n?|\s*\n)*", "", rest, flags=re.MULTILINE)
    rest = rest.rstrip("\n") + ("\n" if rest.strip() else "") + entry
    head = f"{key}:" if head.strip() in (f"{key}: {{}}", f"{key}:{{}}") else head
    new_body = head + "\n" + rest if rest.strip() else ""
    return text[:start] + new_body + text[end:]


def openclaw_text(text: str, name: str, item: dict | None) -> str:
    """mcp.servers in ~/.openclaw/openclaw.json. The file may be JSON5 (comments):
    then it is left alone and `openclaw mcp set` is the way."""
    try:
        data = connect._json(text)
    except ValueError as error:
        raise ValueError("~/.openclaw/openclaw.json is JSON5: use `openclaw mcp set`") from error
    mcp = data.setdefault("mcp", {})
    if not isinstance(mcp, dict):
        raise ValueError("mcp is not an object")
    servers_text = _json_text(json.dumps({"servers": mcp.get("servers", {})}), "servers", name, item)
    servers = json.loads(servers_text).get("servers")
    if servers:
        mcp["servers"] = servers
    else:
        mcp.pop("servers", None)
        if not mcp:
            data.pop("mcp")
    return connect._dump(data)


def check_entry(entry: dict) -> dict:
    """A new server's entry (from the form, the scan or the registry), checked."""
    transport = entry.get("transport")
    if transport == "stdio":
        command = entry.get("command")
        if not isinstance(command, str) or not command.strip() or "\n" in command:
            raise ValueError("no command")
    elif transport in ("http", "sse"):
        from urllib.parse import urlsplit
        url = entry.get("url") or ""
        if urlsplit(url).scheme not in ("http", "https") or not urlsplit(url).netloc:
            raise ValueError("not an http(s) address")
    else:
        raise ValueError("unknown type")
    clean = {"transport": transport, "command": entry.get("command", "") if transport == "stdio" else "",
             "args": [str(a) for a in entry.get("args", [])] if transport == "stdio" else [],
             "env": {}, "url": entry.get("url", "") if transport != "stdio" else "", "headers": {}}
    for field in ("env", "headers"):
        for key, value in (entry.get(field) or {}).items():
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_-]{0,127}$", str(key)) or "\n" in str(value):
                raise ValueError(f"bad name {key!r}")
            clean[field][str(key)] = str(value)
    if transport != "stdio":
        clean["env"] = {}
    else:
        clean["headers"] = {}
    return clean


def _secrets(entry: dict) -> list:
    return [v for v in list(entry["env"].values()) + list(entry["headers"].values()) if len(v) >= 4]


def plan(agent: str, name: str, sources=(), remove: bool = False, entry: dict | None = None) -> connect.Plan:
    """Add server `name` to `agent` — copied from the first of `sources` that
    has it, or `entry` (a normal() entry: a new server) — or remove it.
    Raises ValueError when that cannot be done."""
    if not supported(agent) or not NAME_RE.match(name):
        raise ValueError(agent)
    result = connect.Plan(agent, "", name, connected=is_connected(agent, name))
    if remove:
        entry = None
        own = normal(raw_entries(agent).get(name))
        result.secrets = _secrets(own) if own else []
    elif entry is not None:
        entry = check_entry(entry)
        result.secrets = _secrets(entry)
    else:
        _source, entry = entry_from([s for s in sources if s != agent], name)
        if entry is None:
            import mcp_library
            entry = mcp_library.load().get(name)          # no agent has it any more
            _source = "library" if entry is not None else _source
        if entry is None:
            raise ValueError(f"no agent has {name} set up")
        result.secrets = _secrets(entry)
        result.source = _source
    if agent == "claude":
        claude = shutil.which("claude") or str(HOME / ".local/bin/claude")
        old = normal(raw_entries("claude").get(name))
        if (entry and (entry["env"] or entry["headers"])) or (old and (old["env"] or old["headers"])):
            # Keys in env or headers never go into argv (other accounts can read
            # /proc/<pid>/cmdline): ~/.claude.json is edited directly instead, as
            # one planned change (preview, backup, Undo; refused if Claude wrote
            # the file in the meantime).
            result.changes = _claude_json_change(name, None if remove else native("claude", entry))
            return result
        remove_cmd = [claude, "mcp", "remove", "--scope", "user", name]
        add = lambda e: [claude, "mcp", "add-json", "--scope", "user", name,   # noqa: E731
                         json.dumps(native("claude", e), ensure_ascii=False)]
        if remove:
            if old is None:
                return result
            result.commands, result.undo_commands = [remove_cmd], [add(old)]
        else:
            result.commands = ([remove_cmd] if old else []) + [add(entry)]
            result.undo_commands = [remove_cmd] + ([add(old)] if old else [])
        return result
    path = paths_for(agent)[0]
    old_text = connect._read(path)
    item = None if remove else native(agent, entry)
    profile = agent_profiles.mcp_profile(agent) if agent not in SUPPORTED else None
    if profile is not None:
        if profile["format"] == "json":
            new_text = _json_dotted(old_text, profile["key"], name, item)
        elif profile["format"] == "toml":
            new_text = codex_text(old_text, name, item, profile["key"])
        else:
            new_text = hermes_text(old_text, name, item, profile["key"])
    elif agent in ("codex", "grok"):
        if agent == "grok" and item and "http_headers" in item:
            item["headers"] = item.pop("http_headers")
        new_text = codex_text(old_text, name, item)
    elif agent == "muse":
        new_text = _json_text(old_text, "mcp_servers", name, item)
        data = connect._json(new_text)
        if "schema_version" not in data:      # Muse refuses a settings file without it
            new_text = connect._dump({"schema_version": 1, **data})
    elif agent == "openclaw":
        new_text = openclaw_text(old_text, name, item)
    elif agent == "hermes":
        new_text = hermes_text(old_text, name, item)
    elif agent == "dsh":
        import dsh_ctl
        new_text = dsh_ctl.mcp_text(old_text, name, item)
    else:
        new_text = _json_text(old_text, "mcp" if agent in ("opencode", "crush") else "mcpServers", name, item)
    if new_text != old_text:
        result.changes = [connect.Change(path, old_text, new_text)]
    return result


def _claude_json_change(name: str, item: dict | None) -> list:
    path = HOME / ".claude.json"
    try:
        if path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError("~/.claude.json is too large to edit here")
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    data = json.loads(text) if text.strip() else {}
    if not isinstance(data, dict):
        raise ValueError("~/.claude.json is not a JSON object")
    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError("mcpServers is not an object")
    if item is None:
        if name not in servers:
            return []
        servers.pop(name)
    else:
        servers[name] = item
    new = json.dumps(data, indent=2, ensure_ascii=False) + ("\n" if text.endswith("\n") else "")
    return [connect.Change(path, text, new)] if new != text else []


def describe(entry: dict) -> str:
    """What runs, for the page's text: the command (no args with paths) or the URL's host."""
    if entry is None:
        return ""
    if entry["transport"] != "stdio":
        from urllib.parse import urlparse
        return urlparse(entry["url"]).hostname or entry["url"]
    return os.path.basename(entry["command"])
