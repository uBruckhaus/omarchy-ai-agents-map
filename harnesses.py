"""Harness adapters: what the app knows about each agent harness, one module per harness
(harness_<name>.py), registered in a fixed list here. No outside code is ever loaded.

Each module defines HARNESS = Harness(...). Its capabilities are optional (None: not supported):

- sessions: the harness's sessions as claude_sessions.Session objects (numbers only: folder, times,
  working time, tokens per model, lines changed, cost, subagent runs), so the details page shows the
  same "Projects, last 30 days" for every harness. Session titles and summaries are not here: they
  come from add-ons (Insights), which only Asterism loads (see edition.py).

The database readers open the harness's own store read-only and take only the numeric and folder
columns they name, never message text.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import claude_sessions
from claude_sessions import Session

DB_TIMEOUT = 2.0            # seconds to wait for a database the harness is writing
MAX_SESSIONS = 20000        # sessions read from a database
MAX_MESSAGES = 400000       # message times read from a database
MAX_CWD = 1024


@dataclass(frozen=True)
class Sessions:
    collect: Callable[[], dict]       # {session id: Session} (blocking; numbers only)
    note: Callable[[], str]           # where the numbers come from, translated, for the details page


@dataclass(frozen=True)
class Skills:
    dirs: tuple                       # folders the agent reads skills from by itself ("~/…")
    own: str                          # the folder its links go into
    settings_list: bool = False       # it keeps a folder list in its settings, which the app writes (Pi, Hermes)
    listed: Callable | None = None    # listed() -> extra folders its settings list (raises on unreadable files)
    settings_file: str = ""           # with settings_list: the file that list is in
    write: Callable | None = None     # write(text, folder, remove) -> that file's new text


@dataclass(frozen=True)
class Mcp:
    """Where an agent keeps its MCP servers and how an entry looks there (as agent_profiles does
    for agents looked up on the web)."""
    path: str = ""                    # "~/.codex/config.toml"
    format: str = "json"              # json | toml | yaml | claude (~/.claude.json); others: read/write below
    key: str = "mcpServers"           # the table the servers are in ("mcp_servers", "mcp", "mcp.servers")
    shape: str = "plain"              # how mcp_ctl.native writes one entry for it
    in_app: str = ""                  # set up only inside the agent's own app (where), nothing to write
    read: Callable | None = None      # read(text) -> {name: entry} for a format mcp_ctl does not know
    write: Callable | None = None     # write(text, name, item or None) -> the file's new text
    native: Callable | None = None    # native(entry) -> how one entry looks there (instead of `shape`)


@dataclass(frozen=True)
class Way:
    """One way to install (and remove) an agent or a local LLM; install_recipes picks the first that
    works on this PC. The app runs a way itself only in a terminal window the user watches (sudo asks
    for the password there), and always shows the exact command too."""
    kind: str                         # installer | mise | tool | command | package | manual
    spec: str = ""                    # mise: its tool spec; tool: the package name
    tool: str = ""                    # tool: npm | pipx | uv
    argv: tuple = ()                  # command: the desktop's own installer (e.g. Omarchy's), plain words
    sudo: bool = False                # command: it asks for the password (in the terminal)
    packages: tuple = ()              # package: ((manager, name), …), e.g. (("pacman", "hermes-desktop"),)
    url: str = ""                     # manual: where to get it
    note: str = ""                    # manual: one line of instruction (English)


@dataclass(frozen=True)
class LocalLlm:
    """How an agent is pointed at a local LLM (connect.plan): its settings files and three functions
    over their texts (in the order of `files`)."""
    files: tuple                      # "~/…" settings files the connection is written to
    connected: Callable               # connected(texts, provider) -> bool: the provider entry is there
    active: Callable                  # active(texts, provider) -> bool: its current model is from it
    transform: Callable               # transform(texts, provider, label, url, models, context, model,
                                      #           use_now, remove) -> new texts


def ref(module: str, name: str) -> Callable:
    """A function of another module, imported when first called (adapters stay light, no cycles)."""
    def call(*args, **kwargs):
        import importlib
        return getattr(importlib.import_module(module), name)(*args, **kwargs)
    call.__qualname__ = call.__name__ = f"{module}.{name}"
    return call


@dataclass(frozen=True)
class Harness:
    command: str                      # the agent's command, as in Omarchy's menu
    name: str                         # display name in the app's own texts
    sessions: Sessions | None = None
    # identity and install (agents.py builds its list from these)
    label: str = ""                   # short label when the desktop's menu cannot be read
    full_name: str = ""               # used instead of the menu's short label ("Claude Code" for "Claude")
    icon: str = ""                    # Nerd Font glyph, or Omarchy's own font with icon_font="omarchy"
    icon_font: str = ""
    spec: str = ""                    # mise tool spec when it is not the command
    package: str = ""                 # pacman package that also counts as installed
    package_only: bool = False        # installed only as that package (OpenClaw)
    own_install_counts: bool = True   # a file of the user's own at ~/.local/bin/<command> counts (not Hermes)
    installer: Callable | None = None  # installer() -> install_ctl.Installer: the app installs it itself
    detect: Callable | None = None    # detect() -> (installed, version, source) when none of the above fits
    short_version: bool = False       # show the version up to its first "-" (LM Studio Bionic)
    offered: bool = True              # in Omarchy's agent menu (False: only this app offers it)
    # capabilities (the feature modules build their tables from these)
    security: Callable | None = None  # security(report): agent_security fills in its settings
    usage: Callable | None = None     # usage() -> agent_status.Usage from its own session store
    access: Callable | None = None    # access() -> agent_status.Access: what it needs to work
    login_status: tuple | None = None  # (argv, text meaning "no", text meaning "yes")
    local_llm: LocalLlm | None = None  # can be pointed at a local LLM from the map (connect)
    fixed_provider: str = ""          # tied to one local LLM ("lmstudio")
    self_served: bool = False         # runs that LLM's models itself: never a reason to start it (Bionic)
    skills: Skills | None = None
    mcp: Mcp | None = None
    short_model_flag: bool = False    # takes -m for its model (provider_watch)
    self_update: tuple = ()           # argv of its own updater for its own install (("claude", "update"))
    install: tuple = ()               # ways to install / remove it (Way), best first
    data: tuple = ()                  # "~/…" folders with its settings and sessions: never removed by us
    latest: Callable | None = None    # latest(channel) -> newest version for self_update
    homepage: str = ""                # its web page (the Add-ons page's Homepage button)


# --- helpers for the adapters ----------------------------------------------------------------------

def connect(path: Path):
    """Read-only, never creating anything; None when it cannot be opened."""
    if not path.is_file():
        return None
    try:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=DB_TIMEOUT)
    except sqlite3.Error:
        return None


def number(value) -> int:
    try:
        n = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return n if 0 <= n < 10 ** 13 else 0


def money(value) -> float:
    try:
        n = float(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return n if 0 <= n < 10 ** 7 else 0.0


def folder(value) -> str:
    """A project folder as the harness stored it, or "" when it does not look like one."""
    text = value if isinstance(value, str) else ""
    if text.startswith("/") and len(text) <= MAX_CWD and not any(ord(c) < 32 for c in text):
        return text
    return ""


def model_name(value) -> str:
    """OpenCode keeps {"id": …, "providerID": …}; the others a plain name."""
    text = str(value or "").strip()
    if text.startswith("{"):
        try:
            import json
            data = json.loads(text[:2000])
            text = str(data.get("id", "")) if isinstance(data, dict) else ""
        except ValueError:
            return "unknown"
    return text[:100] or "unknown"


def add_times(session: Session, times) -> None:
    """Working time and busiest hours from message times (epoch seconds), counted as for Claude
    Code: gaps up to claude_sessions.GAP are working time."""
    previous = None
    for when in sorted(t for t in times if t):
        if previous is not None and 0 < when - previous <= claude_sessions.GAP:
            session.active += when - previous
        previous = when
        session.first = min(session.first, when) if session.first else when
        session.last = max(session.last, when)
        session.replies += 1
        local = time.localtime(when)
        slot = str(local.tm_wday * 24 + local.tm_hour)
        session.hours[slot] = session.hours.get(slot, 0) + 1


def add_run(parent: Session, kind: str, run: Session) -> None:
    """A subagent run counted into its parent session, as collect_by_id does for Claude Code."""
    stats = parent.subagents.setdefault(kind or "?", [0, 0, 0.0])
    stats[0] += 1
    stats[1] += sum(c[0] + c[1] + c[3] for c in run.tokens.values())
    stats[2] += run.active


# --- the registry ----------------------------------------------------------------------------------

_MODULES = ("claude", "codex", "copilot", "crush", "cursor_agent", "gemini", "grok", "hermes", "muse", "omp",
            "openclaw", "opencode", "pi", "dsh", "bionic", "agy")     # Omarchy's menu order, then the app's own


_HIDDEN: set = set()       # built-in agents an installed add-on brings instead (hide_built_in)


def _built_in() -> tuple:
    return tuple(h for h in built_in_adapters() if h.command not in _HIDDEN)


def built_in_adapters() -> tuple:
    """The adapters in the app's own modules (the AI Agents Map plugin always uses these)."""
    import importlib
    return tuple(importlib.import_module(f"harness_{name}").HARNESS for name in _MODULES)


def hide_built_in(commands) -> None:
    """Asterism: these agents come as add-ons (installed, signed), so only the add-on, when it is
    switched on, shows them; the AI Agents Map plugin never hides any."""
    _HIDDEN.clear()
    _HIDDEN.update(commands)


def _order(harness) -> int:
    """Add-on agents in the built-in order (Omarchy's menu), new ones after."""
    name = harness.command.replace("-", "_")
    return _MODULES.index(name) if name in _MODULES else len(_MODULES)


_ADDED: list = []          # [(addon id, Harness)] registered by loaded add-ons (addon_api.add_harness)
_LISTENERS: list = []      # rebuild functions of modules that keep tables made from this registry


def _all() -> tuple:
    added = sorted((h for _owner, h in _ADDED), key=_order)
    return _built_in() + tuple(added)


def on_change(listener) -> None:
    """listener() runs after add-ons added or removed agents (a module rebuilding its tables)."""
    _LISTENERS.append(listener)


def changed() -> None:
    for listener in list(_LISTENERS):
        listener()


def add(harness: Harness, owner: str, updates: bool = False) -> None:
    """An add-on's agent; refused when the command is taken (a built-in or another add-on's).
    updates: the add-on may bring an updater (its "updater" capability)."""
    if not isinstance(harness, Harness) or not harness.command:
        raise ValueError("not a Harness")
    if any(h.command == harness.command for h in _all()):
        raise ValueError(f"the agent {harness.command} exists already")
    if (harness.self_update or harness.latest is not None) and not updates:
        raise ValueError("an add-on's agent cannot bring an updater of its own")
    _ADDED.append((owner, harness))


def remove_added(owner: str | None = None) -> None:
    """Forget the agents of one add-on (or of all add-ons)."""
    _ADDED[:] = [(o, h) for o, h in _ADDED if owner is not None and o != owner]


def all_harnesses() -> tuple:
    return _all()


def get(command: str) -> Harness | None:
    return next((h for h in _all() if h.command == command), None)


def with_sessions() -> list:
    return [h for h in _all() if h.sessions is not None]
