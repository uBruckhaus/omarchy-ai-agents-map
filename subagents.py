"""The subagents a running agent works with (Claude Code's Task/Agent tool, OpenCode's task tool):
their type, their task and whether they still work. The map shows a mark in the agent's box while one
works; a click on it opens the subagents page.

Which agent keeps them where and in which form comes from its adapter ([subagents] format and path).

- claude-projects (Claude Code): each running instance's project folder ~/.claude/projects/<its working folder, every
  character but letters and digits as "-">; a session folder there with a subagents/ folder holds per
  subagent agent-<id>.meta.json (its type and the task's description) and agent-<id>.jsonl (its
  transcript, written while it works: its time, and in its last lines only the names of the tools it
  called; one that handed its answer back with HANDBACK has finished).
- opencode-db (OpenCode): the sessions in its database (read-only) that have a parent session: title (the task),
  agent (its type), time_idle (finished).

Only this account's own processes and regular files, never through a link, every read bounded; of what
the subagents say nothing is read but the task's one-line description.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import time
from dataclasses import dataclass
from pathlib import Path

HOME = Path.home()
CLAUDE_PROJECTS = HOME / ".claude/projects"            # the defaults; the adapters name the real places
OPENCODE_DB = HOME / ".local/share/opencode/opencode.db"
RUNNING = 60.0              # seconds since a subagent's transcript was written: it still works
RECENT = 1800.0             # seconds a finished one stays on the page (dimmed): the default
KEPT = 10                   # finished ones kept at most: the default (Settings › Subagents sets both)
MAX_SESSIONS = 2000         # entries looked at in a project folder
MAX_SUBAGENTS = 40          # subagents listed per agent
MAX_META = 16 * 1024        # bytes of a .meta.json
MAX_TASK = 200              # characters of a task's description
MAX_KIND = 60
HANDBACK = "SubagentHandback"   # the tool a Claude Code subagent ends with: it has finished
SESSION_RE = re.compile(r"^[A-Za-z0-9-]{1,80}$")
SUBAGENT_RE = re.compile(r"^(agent-[A-Za-z0-9_-]{1,80})\.meta\.json$")


@dataclass(frozen=True)
class SubAgent:
    agent: str              # the agent's command ("claude")
    key: str                # unique: "<agent>:<session>:<subagent>"
    kind: str               # its type ("general-purpose", "Explore", "build")
    task: str               # the task's description ("" when it has none)
    folder: str             # the session's working folder ("~/Projects/asterism"; "" when not known)
    running: bool
    age: float              # seconds since it last worked


def _text(value, limit: int) -> str:
    """One line of plain text: control characters and runs of space as one space, cut to limit."""
    if not isinstance(value, str):
        return ""
    return " ".join("".join(c if c.isprintable() else " " for c in value).split())[:limit]


def _home(path: str) -> str:
    home = str(HOME)
    return "~" + path[len(home):] if path == home or path.startswith(home + "/") else path


def project_name(folder: str) -> str:
    """Claude Code's name for a working folder's project: every character but letters and digits as "-"."""
    return re.sub(r"[^A-Za-z0-9]", "-", folder)


def _own_cwd(pid: int, proc: str) -> str | None:
    """The working folder of this account's own process, or None."""
    try:
        if os.stat(f"{proc}/{pid}").st_uid != os.getuid():
            return None
        return os.readlink(f"{proc}/{pid}/cwd")
    except OSError:
        return None


def _own_dir(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()


def _read_meta(path: Path) -> dict:
    """A subagent's .meta.json: this account's own regular file, not through a link, at most MAX_META."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return {}
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > MAX_META:
            return {}
        data = handle.read(MAX_META + 1)
    try:
        found = json.loads(data) if len(data) <= MAX_META else {}
    except ValueError:
        return {}
    return found if isinstance(found, dict) else {}


def _written(path: Path) -> float | None:
    """When this account's own regular file was last written, or None."""
    try:
        info = os.lstat(path)
    except OSError:
        return None
    return info.st_mtime if stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() else None


def handed_back(transcript: Path) -> bool:
    """A Claude Code subagent's transcript ends with its hand-back: it has finished (only tool names
    are looked at, in the last lines of this account's own regular file)."""
    import usage_box
    for line in reversed(usage_box.tail_lines(transcript)[-4:]):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        content = (entry.get("message") or {}).get("content") if isinstance(entry, dict) else None
        if isinstance(content, list) and any(isinstance(c, dict) and c.get("type") == "tool_use"
                                             and c.get("name") == HANDBACK for c in content):
            return True
    return False


def claude_projects(agent: str, pids, now: float | None = None, root: Path = CLAUDE_PROJECTS,
                    proc: str = "/proc", recent: float = RECENT) -> list[SubAgent]:
    """Format "claude-projects" (Claude Code): the subagents in the project folders of the agent's running
    instances (pids), the ones that worked in the last `recent` seconds, newest first."""
    now = time.time() if now is None else now
    found, folders = [], {}
    for pid in list(pids)[:64]:
        cwd = _own_cwd(pid, proc)
        if cwd:
            folders.setdefault(project_name(cwd), cwd)
    for name, cwd in folders.items():
        project = root / name
        if not _own_dir(project):
            continue
        try:
            entries = os.scandir(project)
        except OSError:
            continue
        with entries:
            sessions = [e.name for _i, e in zip(range(MAX_SESSIONS), entries)
                        if SESSION_RE.match(e.name) and e.is_dir(follow_symlinks=False)]
        for session in sessions:
            folder = project / session / "subagents"
            if not _own_dir(folder):
                continue
            try:
                names = sorted(os.listdir(folder))[:MAX_SESSIONS]
            except OSError:
                continue
            for meta_name in names:
                match = SUBAGENT_RE.match(meta_name)
                if not match:
                    continue
                transcript = folder / f"{match.group(1)}.jsonl"
                written = _written(transcript) or _written(folder / meta_name)
                if written is None or now - written > recent:
                    continue
                meta = _read_meta(folder / meta_name)
                running = now - written <= RUNNING and not handed_back(transcript)
                found.append(SubAgent(agent, f"{agent}:{session}:{match.group(1)}",
                                      _text(meta.get("agentType"), MAX_KIND) or "general-purpose",
                                      _text(meta.get("description"), MAX_TASK), _home(cwd),
                                      running, max(0.0, now - written)))
    found.sort(key=lambda s: s.age)
    return found[:MAX_SUBAGENTS]


def opencode_db(agent: str, now: float | None = None, db: Path = OPENCODE_DB,
                recent: float = RECENT) -> list[SubAgent]:
    """Format "opencode-db" (OpenCode): the sessions with a parent session in its database of the last
    `recent` seconds, newest first."""
    now = time.time() if now is None else now
    if not (db.is_file() and not db.is_symlink() and db.stat().st_uid == os.getuid()):
        return []
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1.0)
    except sqlite3.Error:
        return []
    try:
        rows = con.execute(
            "SELECT id, title, agent, directory, time_updated, time_idle FROM session_v2"
            " WHERE parent_id IS NOT NULL AND time_updated >= ? ORDER BY time_updated DESC LIMIT ?",
            (int((now - recent) * 1000), MAX_SUBAGENTS)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()
    found = []
    for sid, title, kind, directory, updated, idle in rows:
        if not isinstance(updated, (int, float)):
            continue
        age = max(0.0, now - updated / 1000)
        finished = isinstance(idle, (int, float)) and idle >= updated
        found.append(SubAgent(agent, f"{agent}:{_text(sid, 80)}", _text(kind, MAX_KIND) or "subagent",
                              _text(title, MAX_TASK), _home(_text(directory, 4096)),
                              not finished and age <= RUNNING, age))
    return found


def where(command: str) -> tuple:
    """(format, path) from the agent's adapter ([subagents]), or () when it names none the app reads."""
    import adapter_data
    import harnesses
    harness = harnesses.get(command)
    found = harness.subagents if harness is not None else ()
    return found if len(found) == 2 and found[0] in adapter_data.SUBAGENT_FORMATS else ()


def of(command: str, pids=(), now: float | None = None, recent: float = RECENT, kept: int = KEPT) -> list[SubAgent]:
    """The subagents of a running agent, by the form its adapter names (empty when it names none): every
    working one, and of the finished ones the `kept` newest that worked in the last `recent` seconds."""
    found = where(command)
    if not found:
        return []
    path = Path(os.path.expanduser(found[1]))
    if found[0] == "claude-projects":
        subs = claude_projects(command, pids, now, path, recent=recent)
    else:
        subs = opencode_db(command, now, path, recent=recent)
    finished = [s for s in subs if not s.running][:max(0, kept)]
    return sorted([s for s in subs if s.running] + finished, key=lambda s: s.age)


def working(subs) -> int:
    return sum(1 for s in subs if s.running)


MAX_DEPTH = 12              # parents followed up from an agent's process


def agents_using_agents(pids_by_agent: dict, parent=None, depth: int = MAX_DEPTH) -> set:
    """{(user, used)}: agent commands where a running instance of `used` was started by one of `user`
    (directly or through a shell, a script, a tool it runs), from the process tree. An agent inside
    another instance of itself does not count."""
    if parent is None:
        import inventory
        parent = inventory.parent_pid
    owner = {pid: agent for agent, pids in pids_by_agent.items() for pid in list(pids)[:64]}
    found = set()
    for agent, pids in pids_by_agent.items():
        for pid in list(pids)[:64]:
            current = pid
            for _step in range(depth):
                current = parent(current)
                if not current or current <= 1:
                    break
                user = owner.get(current)
                if user is not None:
                    if user != agent:
                        found.add((user, agent))
                    break
    return found
