"""What herdr (a terminal workspace manager for coding agents) says its agents
are doing right now: working, blocked, idle or done, and in which folder.

Read with `herdr [--session <name>] api snapshot`, for the default session and
the named ones (CONFIG/sessions/<name>/herdr.sock), only while that session's
server runs: its socket must exist, be a socket and belong to this user, so
nothing here starts a server or talks to another account's one. The output is size-capped
and only the agent kind, its state, the folder and the workspace's label are
kept (no terminal titles or screen text).

focus() asks herdr to switch to a pane (`herdr agent focus <pane>`); the
terminal window that shows that session is brought to the front by the
desktop (desktop.focus_process) from the herdr clients' process ids.
"""

import json
import os
import re
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

import ai_core

CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "herdr"
SOCKET = Path(os.environ.get("HERDR_SOCKET_PATH") or CONFIG / "herdr.sock")   # as herdr finds it
MAX_BYTES = 4 * 1024 * 1024
MAX_AGENTS = 200
MAX_SESSIONS = 20
TIMEOUT = 4
# herdr's AgentStatus; "unknown": an agent herdr found whose screen it cannot read (still running).
STATES = ("blocked", "working", "idle", "done", "unknown")
# herdr's agent kinds that differ from the commands here.
KINDS = {"cursor": "cursor-agent"}
_KIND = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
_SESSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_PANE = re.compile(r"[A-Za-z0-9][A-Za-z0-9:_-]{0,63}")


@dataclass
class Pane:
    agent: str                 # the command here (claude, codex, ...)
    state: str                 # one of STATES
    cwd: str = ""
    workspace: str = ""        # the workspace's label, when it has one
    session: str = ""          # a named herdr session ("" for the default one)
    pane_id: str = ""          # herdr's pane id ("w1:p2"), for focus()


def server_running(socket: Path = SOCKET) -> bool:
    """This user's herdr server socket is there (nothing is connected to)."""
    try:
        st = os.lstat(socket)
    except OSError:
        return False
    return stat.S_ISSOCK(st.st_mode) and st.st_uid == os.getuid()


def _text(value, limit: int) -> str:
    if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 for c in value):
        return ""
    return value


def parse(data, session: str = "") -> list[Pane]:
    """The agents of a snapshot (herdr api snapshot's JSON)."""
    if isinstance(data, dict) and isinstance(data.get("result"), dict):
        data = data["result"]
    if isinstance(data, dict) and isinstance(data.get("snapshot"), dict):
        data = data["snapshot"]
    if not isinstance(data, dict) or not isinstance(data.get("agents"), list):
        return []
    labels = {}
    for ws in data.get("workspaces") or []:
        if isinstance(ws, dict) and isinstance(ws.get("workspace_id"), str):
            labels[ws["workspace_id"]] = _text(ws.get("label") or ws.get("name"), 80)
    found = []
    for item in data["agents"][:MAX_AGENTS]:
        if not isinstance(item, dict):
            continue
        kind = item.get("agent")
        state = item.get("agent_status")
        if not isinstance(kind, str) or not _KIND.fullmatch(kind) or state not in STATES:
            continue
        cwd = _text(item.get("foreground_cwd") or item.get("cwd"), 1024)
        pane_id = item.get("pane_id")
        found.append(Pane(KINDS.get(kind, kind), state, cwd if cwd.startswith("/") else "",
                          labels.get(item.get("workspace_id"), ""), session,
                          pane_id if isinstance(pane_id, str) and _PANE.fullmatch(pane_id) else ""))
    return found


def sessions(config: Path = CONFIG) -> list[tuple[str, Path]]:
    """(name, socket) of the herdr sessions that may run: "" is the default one."""
    if os.environ.get("HERDR_SOCKET_PATH"):
        return [("", SOCKET)]                # herdr itself only uses that one then
    found = [("", config / "herdr.sock")]
    try:
        names = sorted(p.name for p in (config / "sessions").iterdir())[:MAX_SESSIONS]
    except OSError:
        names = []
    return found + [(n, config / "sessions" / n / "herdr.sock") for n in names if _SESSION.fullmatch(n)]


def snapshot(config: Path = CONFIG) -> list[Pane] | None:
    """The agents herdr runs now, in every running session; None when no herdr
    server of this user runs (blocking, a few seconds at most: run it on a thread)."""
    binary = shutil.which("herdr")
    if binary is None:
        return None
    found, any_running = [], False
    for name, socket in sessions(config):
        if not server_running(socket):
            continue
        any_running = True
        argv = [binary, *(["--session", name] if name else []), "api", "snapshot"]
        out, ok = ai_core.read_bounded_output(argv, max_bytes=MAX_BYTES, timeout=TIMEOUT)
        if not ok:
            continue
        try:
            data = json.loads(out)
        except ValueError:
            continue
        if isinstance(data, dict) and "error" not in data:
            found += parse(data, name)
    return found if any_running else None


def for_agent(panes: list[Pane] | None, command: str) -> list[Pane]:
    """One agent's panes, the ones that need someone first (blocked, then working)."""
    order = {s: i for i, s in enumerate(STATES)}
    return sorted((p for p in panes or [] if p.agent == command), key=lambda p: order[p.state])


def _session_args(session: str) -> list[str]:
    if session and not _SESSION.fullmatch(session):
        raise ValueError("not a herdr session name")
    return ["--session", session] if session else []


def focus(pane: Pane) -> bool:
    """herdr switches to this pane (in its session). Blocking, a few seconds at most."""
    binary = shutil.which("herdr")
    if binary is None or not _PANE.fullmatch(pane.pane_id or ""):
        return False
    _out, ok = ai_core.read_bounded_output([binary, *_session_args(pane.session), "agent", "focus", pane.pane_id],
                                           max_bytes=64 * 1024, timeout=TIMEOUT)
    return ok


def client_pids(session: str, processes: list[tuple[int, str, list[str]]]) -> list[int]:
    """The herdr clients (the TUI in a terminal, not the server) attached to `session`, from
    (pid, comm, argv) of this user's processes (inventory.own_processes)."""
    found = []
    for pid, comm, argv in processes:
        if comm != "herdr" or not argv or "server" in argv[1:]:
            continue
        args, named = list(argv[1:]), ""
        if "--session" in args[:-1]:
            at = args.index("--session")
            named = args[at + 1]
            del args[at:at + 2]
        if any(not a.startswith("-") for a in args):
            continue                    # a subcommand (api, agent, server, ...): not a client
        if named == session:
            found.append(pid)
    return found
