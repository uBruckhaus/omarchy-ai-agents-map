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

import dataclasses
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
    packages: tuple[tuple[str, str], ...] = ()   # package: ((manager, name), …), e.g. (("pacman", "hermes-desktop"),)
    url: str = ""                     # manual: where to get it
    note: str = ""                    # manual: one line of instruction (English)
    update: tuple = ()                # command: the argv that updates what it installed (plain words)


@dataclass(frozen=True)
class Download:
    """A program the app installs itself from a GitHub release, described as data: an add-on can
    bring a new agent or local LLM this way without app code (install_ctl.Declared runs it: version
    folders, `current`, the command in ~/.local/bin, the release's SHA-256 checked, a user service).
    Placeholders in `service` and `env` values: {binary} {host} {port} {models} {folder}."""
    repo: str                         # "owner/name" on github.com
    assets: tuple                     # ((build, regex for the asset's file name), …), first = default build
    binary: str                       # the program inside the unpacked release (a relative path)
    command: str                      # its name in ~/.local/bin
    tag: str = r"v?\d+(\.\d+)+"       # which release tags count (regex; pre-releases never)
    single_file: bool = False         # the asset is the program itself, not an archive
    builds: tuple = ()                # ((build, label), …) to choose from on the install page
    gpu_builds: tuple = ()            # ((GPU vendor, build), …): the default build per GPU
    service: tuple = ()               # ExecStart argv of its user service; () none (an agent)
    address: str = ""                 # host:port its service listens on
    env: tuple = ()                   # ((NAME, value), …) for the service's environment file
    models_env: str = ""              # the variable in `env` holding the model folder ("" none)
    login_default: bool = False       # the install page's "Start at login", preset

@dataclass(frozen=True)
class Configure:
    """One of an agent's or local LLM's own ways to set it up, offered under Configure… on the map:
    its own command in a terminal tab of the window (a wizard, a menu, a check), or its app window."""
    label: str                        # "Setup wizard"
    argv: tuple                       # its own program and plain words, never a shell
    note: str = ""                    # what to do in it, shown above it in the terminal
    window: bool = False              # an app window of its own (LM Studio), not a terminal


# Programs a Configure command may never start with: it is the agent's or LLM's own program.
NOT_OWN = {"sh", "bash", "zsh", "fish", "dash", "env", "sudo", "pkexec", "doas", "su", "python", "python3",
           "perl", "ruby", "node", "bun", "deno", "npx", "uvx", "curl", "wget", "xdg-open"}


def configure_ok(argv, own: tuple) -> bool:
    """A Configure command: plain words, starting with one of the program names in `own`."""
    return bool(argv) and all(isinstance(a, str) and a and "\n" not in a and "\0" not in a for a in argv) \
        and argv[0] in own and argv[0] not in NOT_OWN and "/" not in argv[0]


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
    download: Download | None = None   # or: what the app installs, as data (install_ctl.Declared)
    detect: Callable | None = None    # detect() -> (installed, version, source) when none of the above fits
    short_version: bool = False       # show the version up to its first "-" (LM Studio Bionic)
    offered: bool = True              # in Omarchy's agent menu (False: only this app offers it)
    # capabilities (the feature modules build their tables from these)
    security: Callable | None = None  # security(report): agent_security fills in its settings
    usage: Callable | None = None     # usage() -> agent_status.Usage from its own session store (else the
                                      # app derives it from `sessions`)
    limits: Callable | None = None    # limits() -> (plan, [agent_details.Limit]) from its own store
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
    ask: tuple = ()                   # argv for one prompt and one answer without tools ("{prompt}" in it)
    games: tuple = ()                 # games it can play through `ask` ("chess"): AI Playground offers them
    effort: tuple = ()                # ((level, options), …): how hard it thinks per Thinking time level
    configure: tuple = ()             # (Configure, …): its own setup commands (Configure… on the map)
    # where it keeps things, and what it is (the feature modules ask these instead of naming agents)
    home: str = ""                    # "~/…" folder of its settings (shown in its card)
    activity: tuple = ()              # "~/…" files and folders whose last change is its last use
    details: Callable | None = None   # details(agent_details.Details): its model and providers
    program_dir: str = ""             # "~/…" folder of its program when it is not where mise or PATH say
    web_search: bool = False          # searches the web itself (no MCP server needed for it)
    chat_app: bool = False            # a chat app, not a coding agent in a terminal (Bionic)
    herdr: str = ""                   # herdr's name for it when it is not the command
    logout: tuple = ()                # argv of its own sign-out (run in a terminal)
    start: tuple = ()                 # arguments ▶ starts it with (DeepSeek Harness: its profile)
    research: tuple = ()              # argv: one prompt looked up on the web, no other tools
                                      # ("{prompt}", "{schema}": a JSON schema file for the answer)
    research_output: str = "text"     # how research answers: "text", or with its token use ("claude-json",
                                      # "codex-jsonl"; ai_helper.parse_output)
    task: tuple = ()                  # argv: started in a terminal tab with "{prompt}" as its first prompt
    rewrite: tuple = ()               # argv: the prompt on stdin, no tools at all; "{answer}": the file
                                      # it writes its answer to (else its output is the answer)
    models: tuple | None = None       # (choices(current, home), plan(model, home)): its online models
    tools_setting: tuple = ()         # ("~/…" JSON file, key): the tools it is given
    sandbox_setting: str = ""         # the security setting that is its sandbox
    replies: tuple = ()               # ("~/…" session folder, depth of its files, format, ((model name
                                      # prefix, context size), …)): the map's usage statistics box (usage_box)
    subagents: tuple = ()             # (format, "~/…" where it keeps them): its subagents (subagents.py)
    prices: tuple = ()                # (as of, ((model name beginning, (input, output, cache read, cache
                                      # write) USD per million tokens), …)): an API user's cost (agent_status)
    asks_setting: str = ""            # the security setting that says when it asks before acting


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
    """The app's own adapters (the AI Agents Map plugin always uses these): adapters/<command>.toml
    where an agent is described as data (adapter_data), else its harness_<name>.py module."""
    if not _BUILT_IN:                      # read once, as an imported module would be
        import importlib

        import adapter_data
        for name in _MODULES:
            path = adapter_data.ADAPTERS / f"{name.replace('_', '-')}.toml"
            _BUILT_IN.append(adapter_data.load(path) if path.is_file()
                             else importlib.import_module(f"harness_{name}").HARNESS)
    return tuple(_BUILT_IN)


_BUILT_IN: list = []


def hide_built_in(commands) -> None:
    """Asterism: these agents come as add-ons (installed, signed), so only the add-on, when it is
    switched on, shows them; the AI Agents Map plugin never hides any."""
    _HIDDEN.clear()
    _HIDDEN.update(commands)
    _forget()


def _order(harness) -> int:
    """Add-on agents in the built-in order (Omarchy's menu), new ones after."""
    name = harness.command.replace("-", "_")
    return _MODULES.index(name) if name in _MODULES else len(_MODULES)


_ADDED: list = []          # [(addon id, Harness)] registered by loaded add-ons (addon_api.add_harness)
_EXTENSIONS: list = []     # [(addon id, command, {field: value})] from extension add-ons (extend)
_LISTENERS: list = []      # rebuild functions of modules that keep tables made from this registry
_CACHE: list = []          # the merged tuple, made again after any change

# What an extension add-on may add to an agent another add-on (or the app) brings: facts and readers
# for the details page and the map. Never what installs, updates or names it.
EXTENDABLE = ("sessions", "usage", "limits", "security", "access", "login_status", "skills", "mcp",
              "local_llm", "data", "homepage")


def _forget() -> None:
    _CACHE.clear()


def _empty(harness: Harness, name: str) -> bool:
    """A field the agent left at its default (None, "", (), False)."""
    default = next(f for f in dataclasses.fields(Harness) if f.name == name).default
    return getattr(harness, name) == default


def _extended(harness: Harness) -> Harness:
    """The agent with what extension add-ons add to it: only fields it left empty, the first
    extension for a field wins (later ones are noted in conflicts())."""
    filled = {}
    for _owner, command, fields in _EXTENSIONS:
        if command != harness.command:
            continue
        for name, value in fields.items():
            if name not in filled and _empty(harness, name):
                filled[name] = value
    return dataclasses.replace(harness, **filled) if filled else harness


# Facts newer than add-on API 1: an agent add-on signed before them does not bring them, so the
# app's own adapter for the same agent gives them until the add-on is signed again.
NEWER_FACTS = ("home", "activity", "details", "program_dir", "web_search", "chat_app", "herdr", "logout",
               "start", "research", "rewrite", "task", "models", "tools_setting", "sandbox_setting",
               "asks_setting", "replies", "subagents", "prices")


def _with_newer_facts(harness: Harness) -> Harness:
    own = next((h for h in built_in_adapters() if h.command == harness.command), None)
    if own is None or own is harness:
        return harness
    filled = {n: getattr(own, n) for n in NEWER_FACTS if _empty(harness, n) and not _empty(own, n)}
    if "research" in filled or (harness.research_output == "text" and own.research_output != "text" and own.research):
        # The format belongs to its command; an add-on's look-up that only answers in text (signed
        # before research.output) gives way to the app's own, which also says its token use.
        filled.update(research=own.research, research_output=own.research_output)
    return dataclasses.replace(harness, **filled) if filled else harness


def _all() -> tuple:
    if not _CACHE:
        added = sorted((_with_newer_facts(h) for _owner, h in _ADDED), key=_order)
        _CACHE.append(tuple(_extended(h) for h in _built_in() + tuple(added)))
    return _CACHE[0]


def on_change(listener) -> None:
    """listener() runs after add-ons added or removed agents (a module rebuilding its tables)."""
    _LISTENERS.append(listener)


def changed() -> None:
    _forget()
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
    _forget()


def remove_added(owner: str | None = None) -> None:
    """Forget the agents and extensions of one add-on (or of all add-ons)."""
    _ADDED[:] = [(o, h) for o, h in _ADDED if owner is not None and o != owner]
    _EXTENSIONS[:] = [e for e in _EXTENSIONS if owner is not None and e[0] != owner]
    _forget()


def extend(command: str, fields: dict, owner: str) -> list:
    """An extension add-on adds facts to an agent (EXTENDABLE fields only). Returns the fields
    refused (not extendable). Whether a field is used is decided when the agent is shown: only where
    the agent left it empty, and the first extension wins (see conflicts)."""
    if not isinstance(command, str) or not command:
        raise ValueError("no agent command")
    refused = [name for name in fields if name not in EXTENDABLE]
    allowed = {name: value for name, value in fields.items() if name in EXTENDABLE and value is not None}
    if allowed:
        _EXTENSIONS.append((owner, command, allowed))
        _forget()
    return refused


def conflicts() -> dict:
    """addon id -> ["command.field", …] it added but that is not used (the agent has it, or an
    earlier extension filled it), for Settings › Add-ons."""
    base = {h.command: h for h in _built_in() + tuple(h for _o, h in _ADDED)}
    taken, found = set(), {}
    for owner, command, fields in _EXTENSIONS:
        harness = base.get(command)
        for name in fields:
            key = (command, name)
            if harness is not None and (not _empty(harness, name) or key in taken):
                found.setdefault(owner, []).append(f"{command}.{name}")
            taken.add(key)
    return found


def all_harnesses() -> tuple:
    return _all()


def get(command: str) -> Harness | None:
    return next((h for h in _all() if h.command == command), None)


def with_sessions() -> list:
    return [h for h in _all() if h.sessions is not None]
