"""What an agent may do when the map starts it: its adapter's [closed] options, added right after the
program in the argv of its look-ups, rewrites and task tabs.

A background run (a web look-up, a skill rewrite) may use the web and nothing else: text it reads there
(a registry entry, a release page) must not reach a tool that changes something. A task in a terminal
tab ("Do it with…", "Fix it with…") asks before each step that changes anything: its permission mode
says so, not only the prompt. Neither gets the agent's MCP servers.

An agent whose MCP servers can only be turned off one by one (closed.mcp_off, "{name}") gets that for
each server in its [mcp] file; a name the option cannot spell refuses the run, as does an agent the
map does not know how to close in. Only the servers' names are read, never their settings.
"""

from __future__ import annotations

import re
from pathlib import Path

SAFE_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")


class Refused(RuntimeError):
    """The agent cannot be closed in as it should be; it is not started."""


def _closed(command: str) -> tuple:
    import harnesses
    harness = harnesses.get(command)
    closed = harness.closed if harness is not None else ()
    if not closed:
        raise Refused(f"AI Agents Map does not know how to keep {command} from changing things; "
                      f"nothing was started")
    return closed


MAX_CONFIG = 4 * 1024 * 1024


def servers(command: str, home_env: str = "", environ=None) -> list[str]:
    """The names of the MCP servers in the agent's own settings file, read on purpose for this (not
    through the map's lenient readers): no file is no servers; a file that cannot be read or
    understood refuses the run, since its servers would then stay on."""
    import json
    import os
    import tomllib
    import harnesses
    environ = os.environ if environ is None else environ
    harness = harnesses.get(command)
    mcp = harness.mcp if harness is not None else None
    if mcp is None or not mcp.path:
        return []
    path = Path(os.path.expanduser(mcp.path))
    if home_env and environ.get(home_env):
        path = Path(environ[home_env]) / path.name        # its home moved: the file moved with it
    try:
        info = path.stat()
    except FileNotFoundError:
        return []
    except OSError as error:
        raise Refused(f"{path} cannot be read ({error.strerror}); nothing was started") from error
    try:
        if info.st_size > MAX_CONFIG:
            raise ValueError("too large")
        text = path.read_text(encoding="utf-8")
        data = tomllib.loads(text) if mcp.format == "toml" else json.loads(text) if mcp.format == "json" else None
        if data is None:
            raise ValueError(f"format {mcp.format}")
        table = data
        for part in mcp.key.split("."):
            table = table.get(part, {}) if isinstance(table, dict) else None
        if not isinstance(table, dict):
            raise ValueError(f"{mcp.key} is not a table")
    except (OSError, ValueError, UnicodeDecodeError) as error:
        raise Refused(f"the MCP servers in {path} cannot be read ({error}); nothing was started") from error
    return list(table)


def mcp_off(command: str, template: tuple, home_env: str = "", names=None) -> list[str]:
    flags: list[str] = []
    if not template:
        return flags
    for name in servers(command, home_env) if names is None else names:
        if not SAFE_NAME.fullmatch(name):
            raise Refused(f"the MCP server {name!r} cannot be turned off for this run; nothing was started")
        flags += [a.replace("{name}", name) for a in template]
    return flags


def _insert(argv: list[str], options: list[str]) -> list[str]:
    return [argv[0]] + options + argv[1:] if argv else argv


def background(command: str, argv: list[str], names=None) -> list[str]:
    """A look-up's or rewrite's argv, closed in (raises Refused)."""
    own, _task, off, home_env = _closed(command)
    return _insert(argv, list(own) + mcp_off(command, off, home_env, names))


def task(command: str, argv: list[str], names=None) -> list[str]:
    """A task tab's argv: the agent asks before each change, whatever its own settings say (raises
    Refused, also when its adapter gives no task options)."""
    _own, options, off, home_env = _closed(command)
    if not options:
        raise Refused(f"AI Agents Map does not know how to make {command} ask before each change; "
                      f"nothing was started")
    return _insert(argv, list(options) + mcp_off(command, off, home_env, names))
