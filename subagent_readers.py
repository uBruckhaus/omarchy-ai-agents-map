"""Bounded readers of delegated-session metadata, selected by adapters.

No credentials, message bodies or model answers are exposed. File formats without a
persisted end marker use recent activity, like the original project reader. A fork
or a parent id alone is deliberately not enough for formats that also use it for
ordinary conversations.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import stat
from pathlib import Path

import subagents as sa

MAX_FILES = 2000
MAX_ENTRIES = 8000
MAX_HEAD = 512 * 1024
MAX_TAIL = 128 * 1024
MAX_EVENTS = 4 * 1024 * 1024
MAX_TOTAL = 32 * 1024 * 1024
_CACHE: dict = {}


def epoch(value) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if math.isfinite(value) and value >= 0 else 0.0
    if isinstance(value, str) and len(value) <= 40:
        from datetime import datetime
        try:
            return epoch(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        except (ValueError, OverflowError, OSError):
            pass
    return 0.0


def entries(data: bytes):
    for line in data.splitlines():
        try:
            item = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(item, dict):
            yield item


def files(root: Path, suffix: str, depth: int, now: float, recent: float) -> list:
    """Own directories only; bounded traversal, newest files first, no symlinks."""
    home = Path.home()
    if not root.is_absolute():
        return []
    if root.is_relative_to(home):
        root = home.resolve() / root.relative_to(home)
    if any(parent.is_symlink() for parent in (root, *root.parents) if parent != home.resolve() and parent.is_relative_to(home.resolve())):
        return []
    pending, found, inspected = [(root, 0)], [], 0
    while pending and inspected < MAX_ENTRIES:
        folder, level = pending.pop()
        if not sa._own_dir(folder):
            continue
        try:
            with os.scandir(folder) as scan:
                children = list(e for _, e in zip(range(MAX_ENTRIES - inspected), scan))
            inspected += len(children)
            # Dated stores: inspect the newest date folders first, even at the traversal limit.
            for child in sorted(children, key=lambda e: e.name):
                if child.is_dir(follow_symlinks=False) and level < depth:
                    pending.append((Path(child.path), level + 1))
                elif child.name.endswith(suffix):
                    info = child.stat(follow_symlinks=False)
                    if (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                            and now - info.st_mtime <= recent):
                        found.append((Path(child.path), info))
        except OSError:
            continue
    return sorted(found, key=lambda item: -item[1].st_mtime)[:MAX_FILES]


def read(path: Path, info, whole: bool = False) -> tuple[bytes, bytes]:
    """Check the opened file again. Read just metadata header and lifecycle tail by default."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        with os.fdopen(fd, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if (not stat.S_ISREG(opened.st_mode) or opened.st_uid != os.getuid()
                    or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)):
                return b"", b""
            if whole:
                if opened.st_size <= MAX_EVENTS:
                    return handle.read(MAX_EVENTS + 1), b""
                head = handle.read(MAX_HEAD)
                handle.seek(max(len(head), opened.st_size - MAX_EVENTS))
                return head, handle.read(MAX_EVENTS)
            head = handle.readline(MAX_HEAD + 1)
            if len(head) > MAX_HEAD:
                return b"", b""
            handle.seek(max(len(head), opened.st_size - MAX_TAIL))
            tail = handle.read(MAX_TAIL)
            return head, tail
    except OSError:
        return b"", b""


def _row(agent: str, sid, kind, task, folder, written, finished, now: float, recent: float):
    when = epoch(written)
    if not when or now - when > recent or not sa._text(sid, 160):
        return None
    age = max(0.0, now - when)
    return sa.SubAgent(agent, f"{agent}:{sa._text(sid, 160)}", sa._text(kind, sa.MAX_KIND) or "subagent",
                       sa._text(task, sa.MAX_TASK), sa._home(sa._text(folder, 4096)),
                       not finished and age <= sa.RUNNING, age)


def rollout(head: bytes, tail: bytes, path: Path, written: float) -> list:
    """A child rollout declares its thread parent in session_meta (including older source nesting)."""
    meta = next(entries(head), {})
    if meta.get("type") != "session_meta" or not isinstance(meta.get("payload"), dict):
        return []
    meta = meta["payload"]
    source = meta.get("source")
    sub = source.get("subagent") if isinstance(source, dict) else None
    spawn = sub.get("thread_spawn") if isinstance(sub, dict) else None
    spawn = spawn if isinstance(spawn, dict) else {}
    parent = meta.get("parent_thread_id") or spawn.get("parent_thread_id")
    # Ordinary conversation forks also have parents. Require delegated origin.
    if not isinstance(sub, (str, dict)) or not sub or not isinstance(parent, str) or not sa.SESSION_RE.fullmatch(parent):
        return []
    name = meta.get("agent_path") or spawn.get("agent_path") or meta.get("agent_nickname") or spawn.get("agent_nickname")
    role = meta.get("agent_role") or spawn.get("agent_role") or name
    if isinstance(name, str) and isinstance(role, str) and name != role:
        role = f"{role} · {name}"
    finished = False
    for event in entries(tail):
        payload = event.get("payload")
        if event.get("type") == "event_msg" and isinstance(payload, dict):
            kind = payload.get("type")
            if kind in ("task_complete", "turn_aborted"):
                finished = True
            elif kind in ("task_started", "turn_started"):
                finished = False
    return [(meta.get("id"), role, name, meta.get("cwd"), written, finished)]


def copilot_events(head: bytes, tail: bytes, path: Path, written: float, previous=(), cwd="") -> list:
    """Persisted SDK lifecycle events, joined by toolCallId, never tool arguments or answers."""
    runs: dict = {row[0].split(':', 1)[1]: list(row) for row in previous}
    cwd = previous[0][3] if previous else cwd
    import itertools
    for event in itertools.chain(entries(head), entries(tail)):
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        kind = event.get("type")
        if kind == "session.start":
            context = data.get("context")
            if isinstance(context, dict):
                cwd = context.get("cwd", "")
        key = sa._text(data.get("toolCallId"), 100)
        if not key:
            continue
        if kind == "subagent.started":
            runs[key] = [f"{path.parent.name}:{key}", data.get("agentName"),
                         data.get("agentDisplayName") or "", cwd,
                         epoch(event.get("timestamp")) or written, False]
        elif kind in ("subagent.completed", "subagent.failed"):
            if key not in runs:
                runs[key] = [f"{path.parent.name}:{key}", data.get("agentName"), "", cwd, written, True]
            runs[key][4] = epoch(event.get("timestamp")) or written
            runs[key][5] = True
    return list(runs.values())


FILE_FORMATS = {
    "codex-rollouts": (".jsonl", 3, rollout, False),
    "copilot-events": ("events.jsonl", 1, copilot_events, True),
}


def from_files(form: str, agent: str, now: float, root: Path, recent: float) -> list[sa.SubAgent]:
    suffix, depth, parser, whole = FILE_FORMATS[form]
    found, budget, used = [], MAX_TOTAL, set()
    for path, info in files(root, suffix, depth, now, recent):
        key = (form, str(path))
        signature = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        used.add(key)
        cached = _CACHE.get(key)
        if cached is not None and cached[0] == signature:
            rows = cached[1]
        else:
            amount = min(info.st_size, MAX_EVENTS + MAX_HEAD + 1 if whole else MAX_HEAD + MAX_TAIL + 1)
            if amount > budget:
                continue
            budget -= amount
            incremental = (whole and cached is not None and len(cached) == 4
                           and signature[:2] == cached[0][:2] and info.st_size > cached[0][2])
            if incremental:
                try:
                    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
                    with os.fdopen(fd, 'rb') as handle:
                        opened = os.fstat(handle.fileno())
                        if (opened.st_dev, opened.st_ino) != signature[:2]:
                            continue
                        handle.seek(cached[0][2])
                        data = cached[2] + handle.read(MAX_EVENTS)
                    # A large growth gets a fresh bounded scan on the next call.
                    if info.st_size - cached[0][2] > MAX_EVENTS:
                        head, tail = read(path, info, whole)
                        data, previous = head + tail, ()
                    else:
                        previous = cached[1]
                except OSError:
                    continue
            else:
                head, tail = read(path, info, whole)
                data, previous = head + tail, ()
            if whole:
                end = data.rfind(b'\n') + 1
                remainder = data[end:][-MAX_HEAD:]
                cwd = cached[3] if incremental else ""
                for event in entries(data[:end]):
                    if event.get('type') == 'session.start':
                        context = event.get('data', {}).get('context', {})
                        if isinstance(context, dict):
                            cwd = sa._text(context.get('cwd'), 4096)
                rows = copilot_events(data[:end], b'', path, info.st_mtime, previous, cwd)
                rows = sorted(rows, key=lambda row: epoch(row[4]), reverse=True)[:sa.MAX_SUBAGENTS]
                _CACHE[key] = (signature, rows, remainder, cwd)
            else:
                rows = sorted(parser(head, tail, path, info.st_mtime), key=lambda row: epoch(row[4]), reverse=True)[:sa.MAX_SUBAGENTS]
                _CACHE[key] = (signature, rows)
        for row in rows:
            result = _row(agent, row[0], row[1], row[2], row[3], row[4], row[5], now, recent)
            if result is not None:
                found.append(result)
    for key in list(_CACHE):
        if key[0] == form and key not in used:
            del _CACHE[key]
    unique = {}
    for row in sorted(found, key=lambda s: s.age):
        unique.setdefault(row.key, row)
    return list(unique.values())[:sa.MAX_SUBAGENTS]


def query(db: Path, sql: str, parameters: tuple = ()) -> list:
    """Own database only, read-only connection, bounded query work and rows."""
    if db.name in ('auth.json', '.credentials.json', 'credentials.json') or \
            any(parent.is_symlink() for parent in db.parents) or sa._written(db) is None or not sa._own_dir(db.parent):
        return []
    try:
        with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=0.2) as con:
            ticks = 0
            def stop():
                nonlocal ticks
                ticks += 1
                return int(ticks > 1000)
            con.set_progress_handler(stop, 1000)
            con.execute("PRAGMA query_only=ON")
            return con.execute(sql, parameters).fetchmany(MAX_FILES)
    except (sqlite3.Error, ValueError, OSError):
        return []


def from_db(form: str, agent: str, now: float, root: Path, recent: float) -> list[sa.SubAgent]:
    if form == "goose-db":
        rows = query(root, "SELECT substr(id,1,80), substr(session_type,1,60), '', substr(working_dir,1,4096), "
                     "CASE WHEN typeof(updated_at) IN ('integer','real') THEN updated_at ELSE substr(updated_at,1,40) END, 0 FROM sessions "
                     "WHERE session_type = 'sub_agent' AND parent_session_id IS NOT NULL "
                     "ORDER BY updated_at DESC LIMIT ?", (MAX_FILES,))
    else:
        return []
    found = [_row(agent, row[0], row[1], row[2], row[3], row[4], row[5], now, recent) for row in rows]
    return sorted((s for s in found if s is not None), key=lambda s: s.age)[:sa.MAX_SUBAGENTS]


def collect(form: str, agent: str, now: float, root: Path, recent: float) -> list[sa.SubAgent]:
    home = Path.home()
    if root.is_relative_to(home):
        root = home.resolve() / root.relative_to(home)
    if form in FILE_FORMATS:
        return from_files(form, agent, now, root, recent)
    return from_db(form, agent, now, root, recent)
