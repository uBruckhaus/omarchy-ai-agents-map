"""Claude Code's work per project, as numbers only: tokens, working time, sessions, lines
changed and cost, from its session files (~/.claude/projects/<project>/<session>.jsonl).

Only two kinds of line are parsed, and only their numbers are kept:
- a model reply ("type":"assistant"): model name, token counts, time, the project folder (cwd);
- a session's cost summary ("type":"cost-state"): lines added/removed, cost, API and tool time.
No message text, title or prompt is read into the results or shown.

Subagent runs (<project>/<session>/subagents/[...]/agent-<id>.jsonl) are read the same way and
counted for their session, per agent type: the type is the only thing read from the run's
agent-<id>.meta.json (its task description is not).

Reading is bounded and incremental: own regular files only (no symlinks), at most MAX_LINE bytes
per line (longer lines are skipped), MAX_FILES files, MAX_BYTES per run; each file is read on from
where the last run stopped (same inode, grown). The results are cached owner-only in CACHE.
"""

import json
import os
import re
import stat
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path

ROOT = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"
CACHE = Path.home() / ".cache/ai-agents-map/claude-sessions.json"
MAX_LINE = 8 * 1024 * 1024
MAX_FILES = 5000
MAX_DIRS = 500
MAX_BYTES = 2 * 1024 ** 3          # per run; the rest is read next time
MAX_CACHE = 16 * 1024 * 1024
GAP = 5 * 60                       # replies further apart than this are separate stretches of work
MAX_META = 64 * 1024
MAX_DEPTH = 3                      # folders below <session>/subagents/ looked into
CACHE_VERSION = 2
_MODEL = re.compile(r"[A-Za-z0-9._:/-]{1,100}")
_SID = re.compile(r"[A-Za-z0-9-]{1,64}")
_AGENT_TYPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._:-]{0,63}")
_ASSISTANT = b'"assistant"'      # quick pre-filters (any JSON spacing); json decides
_COST = b'"cost-state"'


@dataclass
class Session:
    cwd: str = ""
    first: float = 0.0             # first and last reply (epoch seconds)
    last: float = 0.0
    active: float = 0.0            # seconds: gaps between replies up to GAP
    replies: int = 0
    tokens: dict = field(default_factory=dict)      # model -> [input, output, cache read, cache write]
    hours: dict = field(default_factory=dict)       # "weekday*24+hour" -> replies
    lines_added: int = 0
    lines_removed: int = 0
    cost: float = 0.0
    api_ms: int = 0
    # Filled in by collect_by_id from the session's subagent runs (not cached):
    # agent type -> [runs, tokens (input + output + cache write), working seconds]
    subagents: dict = field(default_factory=dict)


def _epoch(stamp) -> float | None:
    if not isinstance(stamp, str) or len(stamp) > 40:
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _number(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 10 ** 13 else 0


def add_reply(session: Session, entry: dict):
    """One model reply: its model, tokens, time and project folder (nothing else)."""
    message = entry.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("usage"), dict):
        return
    usage = message["usage"]
    model = message.get("model")
    model = model if isinstance(model, str) and _MODEL.fullmatch(model) else "unknown"
    counts = session.tokens.setdefault(model, [0, 0, 0, 0])
    for i, key in enumerate(("input_tokens", "output_tokens", "cache_read_input_tokens",
                             "cache_creation_input_tokens")):
        counts[i] += _number(usage.get(key))
    cwd = entry.get("cwd")
    if not session.cwd and isinstance(cwd, str) and cwd.startswith("/") and len(cwd) <= 1024 \
            and not any(ord(c) < 32 for c in cwd):
        session.cwd = cwd
    when = _epoch(entry.get("timestamp"))
    session.replies += 1
    if when is None:
        return
    if session.last and 0 < when - session.last <= GAP:
        session.active += when - session.last
    session.first = min(session.first, when) if session.first else when
    session.last = max(session.last, when)
    local = time.localtime(when)
    slot = str(local.tm_wday * 24 + local.tm_hour)
    session.hours[slot] = session.hours.get(slot, 0) + 1


def add_cost(session: Session, entry: dict):
    """A session's cost summary: lines changed, cost and API time (the latest one counts)."""
    session.lines_added = _number(entry.get("totalLinesAdded"))
    session.lines_removed = _number(entry.get("totalLinesRemoved"))
    cost = entry.get("totalCostUSD")
    session.cost = float(cost) if isinstance(cost, (int, float)) and 0 <= cost < 10 ** 7 else 0.0
    session.api_ms = _number(entry.get("totalAPIDuration"))


def _session_from(data) -> Session | None:
    if not isinstance(data, dict):
        return None
    try:
        s = Session(**{k: data[k] for k in Session.__dataclass_fields__ if k in data and k != "subagents"})
    except TypeError:
        return None
    if not isinstance(s.tokens, dict) or not isinstance(s.hours, dict) or not isinstance(s.cwd, str):
        return None
    return s


def read_file(path: Path, state: dict, budget: int) -> int:
    """Read path on from state["offset"] into state["sessions"]; returns the bytes read."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return 0
    with os.fdopen(fd, "rb") as handle:
        st = os.fstat(handle.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
            return 0
        if state.get("inode") != st.st_ino or st.st_size < state.get("offset", 0):
            state.clear()                       # a new or rewritten file: from the start
        state["inode"] = st.st_ino
        sessions = state.setdefault("sessions", {})
        offset = state.get("offset", 0)
        handle.seek(offset)
        read = 0
        while read < budget:
            line = handle.readline(MAX_LINE + 1)
            if not line:
                break
            if not line.endswith(b"\n"):
                if len(line) <= MAX_LINE:
                    break                       # a line still being written: read it next time
                # Longer than MAX_LINE: skip to its end and keep nothing of it.
                skipped, done = len(line), False
                while True:
                    more = handle.readline(MAX_LINE + 1)
                    if not more:
                        break                   # still being written: the next run starts at it again
                    skipped += len(more)
                    if more.endswith(b"\n"):
                        done = True
                        break
                if not done:
                    break
                read += skipped
                offset += skipped
                continue
            read += len(line)
            offset += len(line)
            if _ASSISTANT not in line and _COST not in line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict) or entry.get("type") not in ("assistant", "cost-state"):
                continue
            sid = entry.get("sessionId")
            sid = sid if isinstance(sid, str) and re.fullmatch(r"[A-Za-z0-9-]{1,64}", sid) else "?"
            data = sessions.get(sid)
            session = data if isinstance(data, Session) else (_session_from(data) or Session())
            sessions[sid] = session
            if entry["type"] == "assistant":
                add_reply(session, entry)
            else:
                add_cost(session, entry)
        state["offset"] = offset
        return read


def _load_cache(cache: Path) -> dict:
    try:
        fd = os.open(cache, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(fd, "rb") as handle:
            if os.fstat(handle.fileno()).st_uid != os.getuid():
                return {}
            data = json.loads(handle.read(MAX_CACHE + 1)[:MAX_CACHE] or b"{}")
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("version") != CACHE_VERSION or not isinstance(data.get("files"), dict):
        return {}
    return data["files"]


def _save_cache(files: dict, cache: Path):
    import ai_core
    plain = {path: {"inode": s.get("inode"), "offset": s.get("offset", 0),
                    **({"type": s["type"]} if isinstance(s.get("type"), str) else {}),
                    "sessions": {sid: asdict(x) if isinstance(x, Session) else x
                                 for sid, x in s.get("sessions", {}).items()}}
             for path, s in files.items()}
    try:
        ai_core.atomic_write(cache, json.dumps({"version": CACHE_VERSION, "files": plain}))
    except OSError:
        pass


def collect(root: Path = ROOT, cache: Path = CACHE) -> list[Session]:
    """Every session's numbers (incremental, bounded; blocking: run it on a thread)."""
    return list(collect_by_id(root, cache).values())


def _subagent_files(session_dir: Path, limit: int) -> list[Path]:
    """agent-*.jsonl below <session>/subagents/ (own folders only, no symlinks, bounded)."""
    found, stack = [], [(session_dir / "subagents", 0)]
    while stack and len(found) < limit:
        folder, depth = stack.pop()
        try:
            with os.scandir(folder) as entries:
                items = sorted(entries, key=lambda e: e.name)[:MAX_DIRS]
        except OSError:
            continue
        for item in items:
            if item.is_symlink():
                continue
            if item.is_dir(follow_symlinks=False):
                if depth < MAX_DEPTH:
                    stack.append((Path(item.path), depth + 1))
            elif item.name.startswith("agent-") and item.name.endswith(".jsonl"):
                found.append(Path(item.path))
    return found[:limit]


def agent_type(transcript: Path) -> str:
    """The run's agent type from its agent-<id>.meta.json ("" when unknown); nothing else of it."""
    meta = transcript.with_name(transcript.name[:-len(".jsonl")] + ".meta.json")
    try:
        fd = os.open(meta, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(fd, "rb") as handle:
            st = os.fstat(handle.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_size > MAX_META:
                return ""
            data = json.loads(handle.read(MAX_META))
    except (OSError, ValueError):
        return ""
    kind = data.get("agentType") if isinstance(data, dict) else None
    return kind if isinstance(kind, str) and _AGENT_TYPE.fullmatch(kind) else ""


def collect_by_id(root: Path = ROOT, cache: Path = CACHE) -> dict:
    """{session id: Session} for every session with replies, its subagent runs counted in."""
    files = _load_cache(cache)
    seen, budget = {}, MAX_BYTES
    try:
        dirs = sorted(p for p in root.iterdir() if p.is_dir() and not p.is_symlink())[:MAX_DIRS]
    except OSError:
        dirs = []
    paths, runs = [], []           # runs: (path, parent session id; None: from its lines)
    for folder in dirs:
        try:
            for path in sorted(folder.glob("*.jsonl")):
                # Older Claude Code versions kept subagent runs next to the sessions.
                (runs.append((path, None)) if path.name.startswith("agent-") else paths.append(path))
            session_dirs = sorted(p for p in folder.iterdir()
                                  if p.is_dir() and not p.is_symlink() and _SID.fullmatch(p.name))[:MAX_FILES]
        except OSError:
            continue
        for session_dir in session_dirs:
            if len(paths) + len(runs) >= MAX_FILES:
                break
            runs += [(path, session_dir.name)
                     for path in _subagent_files(session_dir, MAX_FILES - len(paths) - len(runs))]
        if len(paths) + len(runs) >= MAX_FILES:
            break
    for path in paths[:MAX_FILES] + [path for path, _sid in runs]:
        key = str(path)
        state = s if isinstance(s := files.get(key), dict) else {}
        if budget > 0:
            budget -= read_file(path, state, budget)
        seen[key] = state
    for path, _sid in runs:
        state = seen[str(path)]
        if "type" not in state:
            state["type"] = agent_type(path)
    _save_cache(seen, cache)
    sessions = {}
    run_keys = {str(path): sid for path, sid in runs}
    for key, state in seen.items():
        if key in run_keys:
            continue
        for sid, data in state.get("sessions", {}).items():
            session = data if isinstance(data, Session) else _session_from(data)
            if session is not None and session.replies:
                sessions[sid] = session
    for key, sid in run_keys.items():
        state = seen[key]
        for line_sid, data in state.get("sessions", {}).items():
            parent = sessions.get(sid or line_sid)
            run = data if isinstance(data, Session) else _session_from(data)
            if parent is None or run is None or not run.replies:
                continue
            stats = parent.subagents.setdefault(state.get("type") or "?", [0, 0, 0.0])
            stats[0] += 1
            stats[1] += sum(c[0] + c[1] + c[3] for c in run.tokens.values())
            stats[2] += run.active
    return sessions


# --- what the details page shows ------------------------------------------------------------------

@dataclass
class Project:
    cwd: str
    sessions: int = 0
    active: float = 0.0
    tokens: int = 0                # input + output + cache write (cache reads are counted apart)
    cache_read: int = 0
    lines_added: int = 0
    lines_removed: int = 0
    cost: float = 0.0
    last: float = 0.0
    subagent_runs: int = 0
    subagent_tokens: int = 0       # part of tokens


def projects(sessions: list[Session], since: float = 0.0) -> list[Project]:
    """Per project folder, the sessions last active since `since`, busiest first."""
    found: dict[str, Project] = {}
    for s in sessions:
        if s.last < since:
            continue
        p = found.setdefault(s.cwd or "?", Project(s.cwd or "?"))
        p.sessions += 1
        p.active += s.active
        for counts in s.tokens.values():
            p.tokens += counts[0] + counts[1] + counts[3]
            p.cache_read += counts[2]
        p.lines_added += s.lines_added
        p.lines_removed += s.lines_removed
        p.cost += s.cost
        p.last = max(p.last, s.last)
        for runs, tokens, _active in s.subagents.values():
            p.subagent_runs += runs
            p.subagent_tokens += tokens
            p.tokens += tokens
    return sorted(found.values(), key=lambda p: (-p.active, -p.tokens))


def busiest(sessions: list[Session], since: float = 0.0) -> tuple[int, int] | None:
    """(weekday 0=Monday, hour) with the most replies since `since`, or None."""
    totals: dict[int, int] = {}
    for s in sessions:
        if s.last < since:
            continue
        for slot, n in s.hours.items():
            if str(slot).isdigit() and isinstance(n, int):
                totals[int(slot)] = totals.get(int(slot), 0) + n
    if not totals:
        return None
    slot = max(totals, key=lambda s: totals[s])
    return slot // 24, slot % 24


def subagents(sessions: list[Session], since: float = 0.0) -> list[tuple[str, int, int, float]]:
    """(agent type, runs, tokens, working seconds) of the subagent runs in sessions active since
    `since`, most runs first ("?" for runs whose type is unknown)."""
    totals: dict[str, list] = {}
    for s in sessions:
        if s.last < since:
            continue
        for kind, (runs, tokens, active) in s.subagents.items():
            t = totals.setdefault(kind, [0, 0, 0.0])
            t[0] += runs
            t[1] += tokens
            t[2] += active
    return sorted(((k, *v) for k, v in totals.items()), key=lambda r: (-r[1], -r[2]))
