"""Session readers for agents whose session files have token counts: the same numbers as
claude_sessions (claude_sessions.Session: first and last reply, replies, tokens per model as input,
output, cache read and cache write), for the details page's usage, projects and API cost cards and the
map's cost label. Which agent uses which reader comes from its adapter ([sessions] reader).

- codex-files: ~/.codex/sessions/<y>/<m>/<d>/rollout-*.jsonl; the session's working folder from
  session_meta, its model from turn_context, its tokens from the last token_count's total_token_usage
  (input there includes the cached tokens: they are taken out as cache read).
- pi-files: ~/.pi/agent/sessions/<folder>/*.jsonl; each assistant message's usage (input, output,
  cacheRead, cacheWrite) under its model, the session header's cwd.
- dsh-files: ~/.dsh/sessions/<folder>/session-*/session.v4.jsonl.zstd (zstd, Python 3.14's
  compression.zstd); each assistant/message's usage (inputTokens, outputTokens, cacheReadTokens,
  cacheWriteTokens) under its model.
- omp-db: Oh My Pi's ~/.omp/agent/agent.db, table client_usage (read-only): per row its model,
  requests and input, output, cache read and cache write tokens (input without the cached ones, as
  in Pi), each row one entry here.
- goose-db: Goose's ~/.local/share/goose/sessions/sessions.db, table sessions (read-only): each
  session's accumulated input, output, cache read and cache write tokens (Goose's input includes both
  cache kinds: taken out) and its model (model_config_json's model_name).
- gemini-files: Gemini CLI's ~/.gemini/tmp/<project>/chats/ (session-*.jsonl, a subagent's
  <parent>/<id>.jsonl, older session-*.json): each "gemini" message's tokens (input includes the
  cached ones: taken out; tool prompts count as input, thoughts as output), the same message written
  again later (its tokens came after its text) counted once, by its id.
- openclaw-db: OpenClaw's ~/.openclaw/agents/<agent>/agent/openclaw-agent.sqlite, table
  transcript_events (event_json, or zstd event_zstd), and older ~/.openclaw/agents/<agent>/sessions/
  *.jsonl: each assistant message's usage (input, output, cacheRead, cacheWrite) under its model.
- crush-db: Crush keeps no usable token totals (its columns hold the last step only), but each
  session's cost in US dollars: the projects in ~/.local/share/crush/projects.json, each one's
  crush.db (read-only), table sessions' cost and times. These sessions have that cost and no tokens.

Only numbers, times, model names and working folders are taken, never what was said. This account's
own regular files only, never through a link; files written in the last DAYS days, at most MAX_FILES
per agent and MAX_BYTES each (a zstd file also at most MAX_BYTES once unpacked); lines over MAX_LINE
skipped. What a file gave is kept by (path, size, time) so an unchanged file is not read again.
"""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path

from claude_sessions import Session

HOME = Path.home()
DAYS = 30
MAX_FILES = 2000
MAX_BYTES = 64 * 1024 * 1024
MAX_LINE = 8 * 1024 * 1024
_KEPT: dict = {}              # (reader, path) -> ((size, mtime_ns), Session or None)


def _int(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 10 ** 13 else 0


def _text(value, limit: int = 200) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _files(root: Path, pattern: str, now: float) -> list:
    """This account's own regular files below root matching pattern, written in the last DAYS days,
    newest first, at most MAX_FILES (links are not followed)."""
    found = []
    try:
        candidates = root.glob(pattern)
        for _number, path in zip(range(MAX_FILES * 4), candidates):
            try:
                info = os.lstat(path)
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and now - info.st_mtime <= DAYS * 86400:
                found.append((info.st_mtime, path, info))
    except OSError:
        return []
    found.sort(key=lambda item: -item[0])
    return found[:MAX_FILES]


def _read(path: Path, info) -> bytes:
    """The whole file, opened without following links, if it is still this account's own regular file
    of at most MAX_BYTES."""
    if info.st_size > MAX_BYTES:
        return b""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return b""
    with os.fdopen(fd, "rb") as handle:
        now = os.fstat(handle.fileno())
        if not stat.S_ISREG(now.st_mode) or now.st_uid != os.getuid():
            return b""
        return handle.read(MAX_BYTES + 1)[:MAX_BYTES]


def _unzstd(data: bytes, limit: int = 0) -> bytes:
    """zstd unpacked, at most `limit` bytes (MAX_BYTES by default; more: nothing, not a cut half)."""
    limit = limit or MAX_BYTES
    try:
        from compression import zstd
    except ImportError:         # before Python 3.14: no zstd reader
        return b""
    out, rest = [], data
    total = 0
    try:
        while rest:                 # one frame after another: dsh appends a frame per write
            unpacker = zstd.ZstdDecompressor()
            part = unpacker.decompress(rest, max_length=limit + 1 - total)
            total += len(part)
            if total > limit or not unpacker.eof:
                return b""          # too large once unpacked, or a frame cut short
            out.append(part)
            rest = unpacker.unused_data
    except Exception:
        return b""
    return b"".join(out)


def _entries(data: bytes):
    for line in data.split(b"\n"):
        if not line or len(line) > MAX_LINE:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            yield entry


def _epoch(value) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value / 1000 if value > 1e11 else float(value)           # milliseconds or seconds
    if isinstance(value, str) and len(value) <= 40:
        from datetime import datetime
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _seen(session: Session, when: float | None):
    if when is None:
        return
    session.first = when if not session.first else min(session.first, when)
    session.last = max(session.last, when)


def _add(session: Session, model: str, counts):
    numbers = session.tokens.setdefault(model or "unknown", [0, 0, 0, 0])
    for i, n in enumerate(counts):
        numbers[i] += n


# --- the three forms ------------------------------------------------------------------------------

def _codex(data: bytes) -> Session | None:
    session, model, totals = Session(), "", None
    for entry in _entries(data):
        payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
        kind = entry.get("type")
        _seen(session, _epoch(entry.get("timestamp")))
        if kind == "session_meta":
            session.cwd = _text(payload.get("cwd"), 4096)
        elif kind == "turn_context":
            model = _text(payload.get("model")) or model
        elif kind == "event_msg" and payload.get("type") == "token_count" and isinstance(payload.get("info"), dict):
            usage = payload["info"].get("total_token_usage")
            if isinstance(usage, dict):
                totals = usage
                session.replies += 1
    if totals is None:
        return None
    cached = _int(totals.get("cached_input_tokens"))
    _add(session, model, [max(0, _int(totals.get("input_tokens")) - cached), _int(totals.get("output_tokens")),
                          cached, _int(totals.get("cache_write_input_tokens"))])
    return session


def _pi(data: bytes) -> Session | None:
    session = Session()
    for entry in _entries(data):
        when = _epoch(entry.get("timestamp"))
        if entry.get("type") == "session":
            session.cwd = _text(entry.get("cwd"), 4096)
        message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
        usage = message.get("usage")
        if entry.get("type") != "message" or message.get("role") != "assistant" or not isinstance(usage, dict):
            continue
        _seen(session, when)
        session.replies += 1
        _add(session, _text(message.get("model")), [_int(usage.get("input")), _int(usage.get("output")),
                                                    _int(usage.get("cacheRead")), _int(usage.get("cacheWrite"))])
    return session if session.replies else None


def _dsh(data: bytes) -> Session | None:
    session = Session()
    for entry in _entries(_unzstd(data)):
        body = entry.get("data") if isinstance(entry.get("data"), dict) else {}
        if entry.get("type") == "session":
            session.cwd = _text(body.get("cwd") or body.get("workspace"), 4096)
        usage = body.get("usage")
        if entry.get("type") != "assistant/message" or not isinstance(usage, dict):
            continue
        _seen(session, _epoch(entry.get("time")))
        session.replies += 1
        source = body.get("message", {}).get("source", {}) if isinstance(body.get("message"), dict) else {}
        model = _text(body.get("model")) or (_text(source.get("model")) if isinstance(source, dict) else "")
        _add(session, model, [_int(usage.get("inputTokens")), _int(usage.get("outputTokens")),
                              _int(usage.get("cacheReadTokens")), _int(usage.get("cacheWriteTokens"))])
    return session if session.replies else None




def collect(form: str, root: Path | None = None, now: float | None = None) -> dict:
    """{session file path: Session} of one form (a READERS key) for the last DAYS days."""
    default_root, pattern, parse = READERS[form]
    now = time.time() if now is None else now
    found = {}
    listed = _files(root or default_root, pattern, now)
    current = {(form, str(path)) for _mtime, path, _info in listed}
    for key in [k for k in _KEPT if k[0] == form and k not in current]:
        del _KEPT[key]              # a file gone (or too old now): forgotten, so the cache cannot grow
    for _mtime, path, info in listed:
        key, mark = (form, str(path)), (info.st_size, info.st_mtime_ns)
        kept = _KEPT.get(key)
        if kept is not None and kept[0] == mark:
            session = kept[1]
        else:
            try:
                session = parse(_read(path, info))
            except Exception:       # a file in a form this does not know: left out
                session = None
            _KEPT[key] = (mark, session)
        if session is not None:
            found[str(path)] = session
    return found


MAX_ROWS = 100_000


def omp_db(db: Path | None = None, now: float | None = None) -> dict:
    """{"omp:<row id>": Session} from Oh My Pi's client_usage table, the last DAYS days."""
    import sqlite3
    db = db or HOME / ".omp/agent/agent.db"
    now = time.time() if now is None else now
    try:
        info = os.lstat(db)
    except OSError:
        return {}
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        return {}
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1.0)
    except sqlite3.Error:
        return {}
    try:
        rows = con.execute("SELECT id, recorded_at, model, requests, input_tokens, output_tokens, cache_read_tokens,"
                           " cache_write_tokens FROM client_usage ORDER BY id DESC LIMIT ?", (MAX_ROWS,)).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        con.close()
    found = {}
    for row_id, recorded, model, requests, *counts in rows:
        when = _epoch(recorded)
        if when is None or now - when > DAYS * 86400:
            continue
        session = Session(first=when, last=when, replies=_int(requests))
        _add(session, _text(model), [_int(n) for n in counts])
        found[f"omp:{_int(row_id)}"] = session
    return found


def _sqlite_rows(db: Path, query: str, params=()) -> list:
    """Rows as dicts from this account's own regular database file, opened read-only (not through a
    link); [] when it is not there or the query does not fit its schema."""
    import sqlite3
    try:
        info = os.lstat(db)
    except OSError:
        return []
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        return []
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1.0)
    except sqlite3.Error:
        return []
    try:
        cursor = con.execute(query, params)
        names = [c[0] for c in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchmany(MAX_ROWS)]
    except sqlite3.Error:
        return []
    finally:
        con.close()


GOOSE_COLUMNS = ("id", "working_dir", "created_at", "updated_at", "model_config_json", "accumulated_input_tokens",
                 "accumulated_output_tokens", "accumulated_cache_read_tokens", "accumulated_cache_write_tokens")


def goose_db(db: Path | None = None, now: float | None = None) -> dict:
    """{"goose:<id>": Session} of Goose's sessions updated in the last DAYS days."""
    now = time.time() if now is None else now
    found = {}
    db = db or HOME / ".local/share/goose/sessions/sessions.db"
    have = {row.get("name") for row in _sqlite_rows(db, "SELECT name FROM pragma_table_info('sessions')")}
    columns = [c for c in GOOSE_COLUMNS if c in have]        # older schemas lack the cache columns
    if "updated_at" not in columns:
        return {}
    for row in _sqlite_rows(db, f"SELECT {', '.join(columns)} FROM sessions ORDER BY updated_at DESC LIMIT ?",
                            (MAX_ROWS,)):
        when = _epoch(row.get("updated_at"))
        if when is None or now - when > DAYS * 86400:
            continue
        try:
            config = json.loads(row.get("model_config_json") or "{}")
        except ValueError:
            config = {}
        model = _text(config.get("model_name")) if isinstance(config, dict) else ""
        read, write = _int(row.get("accumulated_cache_read_tokens")), _int(row.get("accumulated_cache_write_tokens"))
        session = Session(cwd=_text(row.get("working_dir"), 4096), first=_epoch(row.get("created_at")) or when, last=when)
        _add(session, model, [max(0, _int(row.get("accumulated_input_tokens")) - read - write),
                              _int(row.get("accumulated_output_tokens")), read, write])
        found[f"goose:{_text(row.get('id'), 80)}"] = session
    return found


def _gemini(data: bytes) -> Session | None:
    session, kept = Session(), {}
    try:
        whole = json.loads(data) if data.lstrip().startswith(b"{") else None      # an older session-*.json
    except ValueError:
        whole = None                                                              # JSONL: one record a line
    if isinstance(whole, dict) and isinstance(whole.get("messages"), list):
        entries = [m for m in whole["messages"][:MAX_ROWS] if isinstance(m, dict)]
    else:
        entries = list(_entries(data))
    for entry in entries:
        if not isinstance(entry.get("tokens"), dict) or entry.get("type") == "user":
            continue                      # only the model's replies carry token counts
        kept[str(entry.get("id") or len(kept))[:200]] = entry          # written again: the last one counts
    for entry in kept.values():
        tokens = entry["tokens"]
        cached = _int(tokens.get("cached"))
        _seen(session, _epoch(entry.get("timestamp")))
        session.replies += 1
        _add(session, _text(entry.get("model")),
             [max(0, _int(tokens.get("input")) - cached) + _int(tokens.get("tool")),
              _int(tokens.get("output")) + _int(tokens.get("thoughts")), cached, 0])
    return session if session.replies else None


def _openclaw_entry(session: Session, entry: dict):
    message = entry.get("message") if isinstance(entry.get("message"), dict) else entry
    usage = message.get("usage") if isinstance(message.get("usage"), dict) else entry.get("usage")
    report = entry.get("usageReport") if isinstance(entry.get("usageReport"), dict) else {}
    if message.get("role") != "assistant" or not isinstance(usage, dict) or report.get("state") == "unavailable":
        return
    _seen(session, _epoch(message.get("timestamp")) or _epoch(entry.get("timestamp")))
    session.replies += 1
    _add(session, _text(message.get("model")), [_int(usage.get("input")), _int(usage.get("output")),
                                                _int(usage.get("cacheRead")), _int(usage.get("cacheWrite"))])


def _openclaw_jsonl(data: bytes) -> Session | None:
    session = Session()
    for entry in _entries(data):
        _openclaw_entry(session, entry)
    return session if session.replies else None


MAX_EVENT = 1024 * 1024     # bytes of one OpenClaw transcript event, packed or unpacked
MAX_EVENTS = 20_000         # events read per database


def openclaw_db(root: Path | None = None, now: float | None = None) -> dict:
    """{"openclaw:<session>": Session}: each agent's openclaw-agent.sqlite (transcript_events) and its
    older sessions/*.jsonl, the last DAYS days."""
    root = root or HOME / ".openclaw/agents"
    now = time.time() if now is None else now
    found = {}
    for _mtime, db, _info in _files(root, "*/agent/openclaw-agent.sqlite", now):
        since = (now - DAYS * 86400)
        rows = _sqlite_rows(db, "SELECT session_id, event_json, event_zstd FROM transcript_events"
                                " WHERE length(event_json) <= ? OR length(event_zstd) <= ?"
                                " ORDER BY rowid DESC LIMIT ?", (MAX_EVENT, MAX_EVENT, MAX_EVENTS))
        for row in rows:
            raw = row.get("event_json")
            if not raw and isinstance(row.get("event_zstd"), bytes):
                raw = _unzstd(row["event_zstd"], MAX_EVENT)          # one event: at most MAX_EVENT unpacked
            try:
                entry = json.loads(raw) if raw else None
            except (ValueError, TypeError):
                entry = None
            if not isinstance(entry, dict):
                continue
            session = found.setdefault(f"openclaw:{_text(str(row.get('session_id')), 80)}", Session())
            _openclaw_entry(session, entry)
            if session.last and session.last < since:
                session.replies = 0
    found = {k: s for k, s in found.items() if s.replies}
    found.update({f"openclaw-file:{k}": s for k, s in collect("openclaw-files", root, now).items()})
    return found


def crush_db(projects: Path | None = None, now: float | None = None) -> dict:
    """{"crush:<project>:<session>": Session} with the cost Crush reports per session (no tokens: it
    keeps none that add up), from the projects it lists, the last DAYS days."""
    projects = projects or HOME / ".local/share/crush/projects.json"
    now = time.time() if now is None else now
    try:
        info = os.lstat(projects)
        listed = json.loads(_read(projects, info)) if stat.S_ISREG(info.st_mode) else {}
    except (OSError, ValueError):
        return {}
    found = {}
    for number, project in enumerate((listed.get("projects") if isinstance(listed, dict) else None) or []):
        if number >= 200 or not isinstance(project, dict):
            break
        folder = _text(project.get("data_dir"), 4096) or (_text(project.get("path"), 4096) + "/.crush")
        if not folder.startswith("/"):
            continue
        for row in _sqlite_rows(Path(folder) / "crush.db",
                                "SELECT id, cost, created_at, updated_at FROM sessions ORDER BY updated_at DESC LIMIT ?",
                                (MAX_ROWS,)):
            when = _epoch(row.get("updated_at"))
            cost = row.get("cost")
            if when is None or now - when > DAYS * 86400 or not isinstance(cost, (int, float)) or cost <= 0:
                continue
            session = Session(cwd=_text(project.get("path"), 4096), first=_epoch(row.get("created_at")) or when,
                              last=when, cost=float(min(cost, 10 ** 6)))
            found[f"crush:{number}:{_text(str(row.get('id')), 80)}"] = session
    return found


def codex_files() -> dict:
    return collect("codex-files")


def pi_files() -> dict:
    return collect("pi-files")


def dsh_files() -> dict:
    return collect("dsh-files")


def gemini_files() -> dict:
    return collect("gemini-files")


READERS = {                  # the forms adapters name ([sessions] reader): where and how
    "codex-files": (HOME / ".codex/sessions", "*/*/*/rollout-*.jsonl", _codex),
    "pi-files": (HOME / ".pi/agent/sessions", "*/*.jsonl", _pi),
    "dsh-files": (HOME / ".dsh/sessions", "*/session-*/session.v4.jsonl.zstd", _dsh),
    "gemini-files": (HOME / ".gemini/tmp", "*/chats/**/*.json*", _gemini),
    "openclaw-files": (HOME / ".openclaw/agents", "*/sessions/*.jsonl", _openclaw_jsonl),
}
