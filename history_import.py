"""Collect history (Maprisma task 076: history_import.dart): the log also covers work agents did while
the window was closed or before the log was switched on, read from the agents' own session files.

An option, on by default (Settings › Collect activity from agent history), and only while the log is
on. The Log tab's "Collect history" button runs it, and the window once after it opens (throttled to
once an hour through ai-log/.import-state.json). Writes only into ai-log/.

Forms (an agent's adapter: [sessions] activity_reader or reader, and activity_dir):
- claude-files  ~/.claude/projects/*/*.jsonl, top level only (no subagents/, no agent-*.jsonl);
- codex-jsonl   ~/.codex/sessions/**/rollout-*.jsonl; subagent rollouts (session_meta.source a map) skipped;
- pi-files      ~/.pi/agent/sessions/*/*.jsonl (child sessions with parentSession skipped);
- trajectory-sqlite  Antigravity conversations: no timestamps in the database, so one span per
  conversation from the file's birth and modification times, marked partial.
Any other agent with session files: unsupported.

Bounds: files changed inside the window only (days, at most the retention), 2,000 sessions per agent,
20,000 folder entries, own regular files only, never through a link; a file of 64 MiB or less is read
whole, a larger one as a 64 KiB head plus an 8 MiB tail (partial); lines over 4 MiB skipped; 60 s per
agent, 180 s in all. A span ends after 30 minutes without a turn; one whose last turn is under 2
minutes old is still running and skipped this time. Topics: the first request of a span through
activity_log.sanitize_topic when "Use the agent's request as topic" is on, else editing/testing.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import stat
import time
from pathlib import Path

import activity_log
from activity_log import ActivityError, history_session, iso, label, micros, sanitize_topic

FORMS = ("claude-files", "codex-jsonl", "trajectory-sqlite", "pi-files")
MAX_SESSIONS = 2000
MAX_VISITED = 20000
WHOLE = 64 << 20
HEAD = 64 * 1024
TAIL = 8 << 20
MAX_LINE = 4 << 20
GAP = dt.timedelta(minutes=30)
RUNNING = dt.timedelta(minutes=2)
SOURCE_SECONDS, TOTAL_SECONDS = 60.0, 180.0
THROTTLE = dt.timedelta(hours=1)
STATE = ".import-state.json"
SEVERITY = {"ok": 0, "partial": 1, "unavailable": 2, "corrupt": 3}
_PATCH = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", re.MULTILINE)
_TESTS = re.compile(r"(^|[/_.-])tests?([/_.-]|$)", re.IGNORECASE)


def history_sources(installed=(), home: Path | None = None) -> list[dict]:
    """[{source, name, supported, form, root}] of the agents with session files: installed ones and
    those whose history folder exists (import_sources of the report, the help dialog's list)."""
    import agent_activity_report
    import harnesses
    home = home or activity_log.identity_home()
    found = []
    for harness in harnesses.all_harnesses():
        facts = agent_activity_report.session_facts(harness.command)
        has_sessions = harness.sessions is not None or bool(harness.session_files) or bool(facts)
        if not has_sessions:
            continue
        root = Path(os.path.normpath(str(home / facts["dir"][2:]))) if facts else None
        if root is not None and not str(root).startswith(str(home).rstrip("/") + "/"):
            root, facts = None, {}           # a folder outside the home folder is never read
        if harness.command not in set(installed) and not (root is not None and root.exists()):
            continue
        found.append({"source": f"agent:{harness.command}", "name": harness.full_name or harness.name,
                      "supported": facts.get("reader") in FORMS, "form": facts.get("reader", ""),
                      "root": str(root) if root else ""})
    return found


def public_sources(sources) -> list[dict]:
    return [{k: s[k] for k in ("source", "name", "supported")} for s in sources]


def epoch(value) -> float | None:
    """Seconds since 1970 of a session line's timestamp (ISO text, or seconds / milliseconds)."""
    if isinstance(value, str):
        moment = activity_log.parse_time(value)
        return moment.timestamp() if moment else None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value / 1000 if value > 1e11 else float(value)
    return None


class _Span:
    def __init__(self, start: dt.datetime, leading_partial: bool = False):
        self.start = self.end = start
        self.leading_partial = leading_partial
        self.request = self.model = None
        self.input = self.output = None
        self.edits: list[str] = []

    @property
    def heuristic(self):
        if not self.edits:
            return None
        return "testing" if any(_TESTS.search(f) for f in self.edits) else "editing"


class _Parsed:
    def __init__(self, session: str, partial: bool = False):
        self.session, self.cwd, self.partial, self.subagent = session, "", partial, False
        self.spans: list[_Span] = []


def _request(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text = " ".join(str(b.get("text") or "") for b in content
                        if isinstance(b, dict) and b.get("type") in ("text", "input_text"))
        return text or None
    return None


class HistoryImporter:
    """One import into `log` from `sources` (history_sources())."""

    def __init__(self, log: activity_log.ActivityLog, sources: list[dict], now: dt.datetime | None = None,
                 clock=time.monotonic):
        self.log, self.sources = log, sources
        self.now = now or activity_log.now_utc()
        self.clock = clock
        self._overall = self._source = 0.0

    def _expired(self) -> bool:
        now = self.clock()
        return now - self._overall >= TOTAL_SECONDS or now - self._source >= SOURCE_SECONDS

    def validate(self):
        if not self.log.enabled:
            raise ActivityError("log_off")
        if self.log.setting("ai_activity_import") is not True:
            raise ActivityError("import_off")

    def throttled(self, dir_fd: int) -> bool:
        data = activity_log.read_own(dir_fd, STATE, 4096)
        try:
            at = activity_log.parse_time(json.loads(data).get("completed_at")) if data else None
        except (ValueError, AttributeError):
            at = None
        return at is not None and dt.timedelta(0) <= self.now - at < THROTTLE

    @staticmethod
    def _raise(result: dict, status: str):
        if SEVERITY[status] > SEVERITY[result["status"]]:
            result["status"] = status

    def run(self, days: int | None = None, auto: bool = False, progress=None) -> dict:
        """activitylog.import: {sources: [...], added, updated, skipped} or {skipped: "throttled"}.
        Refusals before the work are ActivityError (log_off, import_off, import_running,
        invalid_request); during it they end the run with {error, code}."""
        self.validate()
        keep = self.log.keep
        if days is not None and (type(days) is not int or not 1 <= days <= keep):
            raise ActivityError("invalid_request")
        dir_fd = activity_log.private_dir(self.log.directory)
        lock = None
        try:
            lock = activity_log.open_lock(dir_fd, ".import.lock")
            if not activity_log.try_lock(lock):
                raise ActivityError("import_running")
            self.validate()
            if auto and self.throttled(dir_fd):
                return {"skipped": "throttled"}
            try:
                return self._run(days or keep, progress)
            except ActivityError as error:
                return {"error": str(error), "code": error.code}
        finally:
            if lock is not None:
                os.close(lock)
            os.close(dir_fd)

    def _run(self, days: int, progress) -> dict:
        self._overall = self.clock()
        cutoff_date = activity_log.local_date(self.now, 1 - days)
        cutoff = dt.datetime.combine(cutoff_date, dt.time()).astimezone()
        results = []
        for source in self.sources:
            self._source = self.clock()
            if progress is not None:
                progress(source["name"])
            result = {"source": source["source"], "name": source["name"], "sessions": 0, "added": 0,
                      "updated": 0, "skipped": 0, "status": "ok"}
            results.append(result)
            if not source.get("supported"):
                result["status"] = "unsupported"
                continue
            root, form = source.get("root", ""), source["form"]
            if not root or not os.path.isdir(root):
                self._raise(result, "unavailable")
                continue
            command = source["source"].partition(":")[2]
            paths, partial = self._candidates(root, form, cutoff)
            if partial:
                self._raise(result, "partial")
            for path in paths:
                if self._expired():
                    self._raise(result, "partial")
                    break
                self.validate()
                try:
                    parsed = self._trajectory(path) if form == "trajectory-sqlite" else self._json(path, form)
                except ValueError:
                    self._raise(result, "corrupt")
                    continue
                except OSError:
                    self._raise(result, "unavailable")
                    continue
                if parsed is None:
                    self._raise(result, "unavailable")
                    continue
                if parsed.subagent:
                    result["skipped"] += 1
                    continue
                result["sessions"] += 1
                if parsed.partial:
                    self._raise(result, "partial")
                for span in parsed.spans:
                    if self._expired():
                        self._raise(result, "partial")
                        break
                    if span.start < cutoff or self.now - span.end < RUNNING:
                        result["skipped"] += 1
                        continue
                    self._write(command, parsed, span, cutoff, result)
        self.validate()
        import ai_core
        ai_core.atomic_write(self.log.directory / STATE, json.dumps({"completed_at": iso(self.now)}))
        return {"sources": results, **{k: sum(r[k] for r in results) for k in ("added", "updated", "skipped")}}

    def _write(self, command: str, parsed: _Parsed, span: _Span, cutoff: dt.datetime, result: dict):
        session = history_session(parsed.session)
        topic = (sanitize_topic(span.request) or span.heuristic) if self.log.import_topics else span.heuristic
        files = []
        for path in span.edits:
            name = label(os.path.basename(path))
            if name and name not in files:
                files.append(name)
        project = label(os.path.basename(parsed.cwd.rstrip("/"))) if parsed.cwd else None
        row = {"version": 1, "id": f"history:agent:{command}:{session}:{micros(span.start)}", "origin": "history",
               "session": session, "superseded_by": None, "source_kind": "agent", "source": command,
               "project": project, "model": label(span.model), "topic": topic,
               "started_at": iso(span.start), "ended_at": iso(span.end),
               "duration_ms": (micros(span.end) - micros(span.start)) // 1000, "files": files[:30], "helpers": [],
               "tokens_in": span.input, "tokens_out": span.output, "cost": None, "outcome": "unknown"}
        status, _row = self.log.merge_history(row, leading_partial=span.leading_partial, earliest_start=cutoff,
                                              now=self.now)
        if status in ("added", "updated"):
            result[status] += 1
        elif status == "skipped":
            result["skipped"] += 1
            self._raise(result, "partial")

    # -- which files ------------------------------------------------------------------------------

    def _candidates(self, root: str, form: str, cutoff: dt.datetime) -> tuple[list, bool]:
        """The session files changed since the cutoff, newest first. Folders are read entry by entry
        (scan_budget.Budget, with this import's own deadline), never listed or sorted whole first."""
        import scan_budget
        found: list[tuple[float, str]] = []
        partial = False
        uid, limit = os.getuid(), cutoff.timestamp()
        left = max(0.0, min(TOTAL_SECONDS - (self.clock() - self._overall), SOURCE_SECONDS - (self.clock() - self._source)))
        budget = scan_budget.Budget(visits=MAX_VISITED, seconds=left)

        def walk(path: str, depth: int):
            nonlocal partial
            if self._expired() or not budget.take():
                partial = True
                return
            try:
                info = os.lstat(path)
            except OSError:
                return
            if info.st_uid != uid:
                return
            if stat.S_ISDIR(info.st_mode):
                if depth > (1 if form == "claude-files" else 12):
                    return
                try:
                    listing = os.scandir(path)
                except OSError:
                    return
                with listing:
                    for count, entry in enumerate(listing):
                        if count >= 4000:
                            partial = True
                            break
                        if entry.name != "subagents":
                            walk(entry.path, depth + 1)
                        if partial and budget.stopped:
                            break
            elif stat.S_ISREG(info.st_mode):
                modified = info.st_mtime
                if form == "trajectory-sqlite":
                    try:
                        wal = os.lstat(path + "-wal")
                        modified = max(modified, wal.st_mtime)
                    except OSError:
                        pass
                if modified < limit:
                    return
                name = os.path.basename(path)
                if form == "trajectory-sqlite":
                    eligible = name.endswith(".db")
                else:
                    eligible = name.endswith(".jsonl") and not name.startswith("agent-") \
                        and (form != "codex-jsonl" or name.startswith("rollout-"))
                if eligible:
                    if len(found) < MAX_SESSIONS:
                        found.append((modified, path))
                    else:
                        partial = True
        walk(root, 0)
        found.sort(reverse=True)
        return [p for _m, p in found], partial

    # -- JSONL sessions ---------------------------------------------------------------------------

    def _records(self, data: bytes, parsed: _Parsed, cut: bool = False) -> list[dict]:
        text = data.decode("utf-8", "replace")
        if cut:
            newline = text.find("\n")
            text = "" if newline < 0 else text[newline + 1:]
        rows = []
        for line in text.splitlines():
            if self._expired():
                parsed.partial = True
                break
            if len(line) > MAX_LINE:
                parsed.partial = True
                continue
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                parsed.partial = True
                continue
            if isinstance(row, dict):
                rows.append(row)
        return rows

    def _json(self, path: str, form: str) -> _Parsed:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise OSError("unsafe history file")
            big = info.st_size > WHOLE
            head = _pread_all(fd, HEAD, 0) if big else _read_all(fd, WHOLE)
            tail = _pread_all(fd, TAIL, max(0, info.st_size - TAIL)) if big else None
        finally:
            os.close(fd)
        parsed = _Parsed(os.path.splitext(os.path.basename(path))[0], partial=big)
        head_rows = self._records(head, parsed)
        for row in head_rows:
            if row.get("type") == "session_meta" and isinstance(row.get("payload"), dict):
                meta = row["payload"]
                if isinstance(meta.get("source"), dict):
                    parsed.subagent = True
                parsed.cwd = str(meta.get("cwd") or "")
                parsed.session = str(meta.get("id") or parsed.session)
            if row.get("type") == "session":
                parsed.cwd = str(row.get("cwd") or "")
                parsed.session = str(row.get("id") or parsed.session)
                if row.get("parentSession") is not None:
                    parsed.subagent = True
        if parsed.subagent:
            return parsed
        if big and form == "codex-jsonl" and not any(
                r.get("type") == "session_meta" and isinstance(r.get("payload"), dict) for r in head_rows):
            return parsed          # a cut header cannot tell a subagent: wait for a readable one
        rows = head_rows if tail is None else self._records(tail, parsed, cut=True)
        codex = form == "codex-jsonl"
        current = None
        previous_in = previous_out = None
        first, pending_model = True, None
        seen: dict[str, tuple[int, int]] = {}
        for row in rows:
            if self._expired():
                parsed.partial = True
                break
            payload = row["payload"] if isinstance(row.get("payload"), dict) else {}
            message = row["message"] if isinstance(row.get("message"), dict) else {}
            stamp = row.get("timestamp", message.get("timestamp"))
            at = epoch(stamp)
            if at is None:
                if stamp is not None:
                    parsed.partial = True
                continue
            if not 0 <= at < 253402300800:
                parsed.partial = True
                continue
            moment = dt.datetime.fromtimestamp(at, dt.timezone.utc)
            if row.get("type") == "turn_context" and isinstance(payload.get("model"), str):
                pending_model = payload["model"]
                continue
            if codex:
                user = row.get("type") == "event_msg" and payload.get("type") == "user_message"
                assistant = (row.get("type") == "event_msg" and payload.get("type") == "agent_message") or \
                    (row.get("type") == "response_item" and payload.get("role") == "assistant")
            else:
                user = row.get("type") == "user" or message.get("role") == "user"
                assistant = row.get("type") == "assistant" or message.get("role") == "assistant"
            token = codex and payload.get("type") == "token_count"
            if not (user or assistant or token or (codex and row.get("type") == "response_item")):
                continue
            if current is None or moment - current.end > GAP:
                current = _Span(moment, leading_partial=big and first)
                parsed.spans.append(current)
                first = False
            current.start = min(current.start, moment)
            current.end = max(current.end, moment)
            current.model = pending_model or current.model
            if not parsed.cwd and isinstance(row.get("cwd"), str):
                parsed.cwd = row["cwd"]
            if user and current.request is None:
                current.request = _request(payload.get("message") if codex
                                           else message.get("content", row.get("content")))
            if isinstance(message.get("model"), str):
                current.model = message["model"]
            if token:
                info = payload.get("info")
                total = info.get("total_token_usage") if isinstance(info, dict) else None
                if isinstance(total, dict):
                    tin, tout = total.get("input_tokens"), total.get("output_tokens")
                    if type(tin) is int and type(tout) is int and tin >= 0 and tout >= 0:
                        if not current.leading_partial and (previous_in is not None or not big):
                            d_in, d_out = tin - (previous_in or 0), tout - (previous_out or 0)
                            if d_in >= 0 and d_out >= 0:
                                current.input = (current.input or 0) + d_in
                                current.output = (current.output or 0) + d_out
                        previous_in, previous_out = tin, tout
            elif assistant and isinstance(message.get("usage"), dict):
                key = str(message.get("id") or row.get("uuid") or row.get("id") or micros(moment))
                usage = message["usage"]
                tin, tout = usage.get("input_tokens", usage.get("input")), usage.get("output_tokens", usage.get("output"))
                if type(tin) is int and type(tout) is int and tin >= 0 and tout >= 0:
                    before = seen.get(key, (0, 0))
                    current.input = (current.input or 0) + max(0, tin - before[0])
                    current.output = (current.output or 0) + max(0, tout - before[1])
                    seen[key] = (max(tin, before[0]), max(tout, before[1]))
            content = message.get("content")
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    arguments = block.get("input", block.get("arguments"))
                    if block.get("name") in ("Edit", "Write", "edit", "write", "write_file", "MultiEdit") \
                            and isinstance(arguments, dict):
                        target = arguments.get("file_path", arguments.get("path"))
                        if isinstance(target, str) and len(current.edits) < 200:
                            current.edits.append(target)
            patch = payload.get("input", payload.get("arguments"))
            if isinstance(patch, str) and (payload.get("name") == "apply_patch" or "*** Begin Patch" in patch):
                for match in list(_PATCH.finditer(patch))[:200]:
                    current.edits.append(match.group(1))
            if current.leading_partial:
                current.input = current.output = None
        if not head_rows and not big:
            raise ValueError("No readable session records")
        return parsed

    # -- Antigravity conversations ----------------------------------------------------------------

    def _trajectory(self, path: str) -> _Parsed | None:
        import agent_activity_report
        import readonly_sqlite
        with open(path, "rb", opener=lambda p, f: os.open(p, f | os.O_NOFOLLOW | os.O_NONBLOCK)) as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode) or os.fstat(handle.fileno()).st_uid != os.getuid():
                raise OSError("unsafe history database")
            if handle.read(16) != b"SQLite format 3\0":
                raise ValueError("not a database")
        con = readonly_sqlite.connect(Path(path))
        if con is None:
            return None
        try:
            info = os.lstat(path)
            modified = info.st_mtime
            try:
                modified = max(modified, os.lstat(path + "-wal").st_mtime)
            except OSError:
                pass
            end = dt.datetime.fromtimestamp(modified, dt.timezone.utc)
            born = _birth(path)
            start = dt.datetime.fromtimestamp(born, dt.timezone.utc) if born else end
            parsed = _Parsed(os.path.splitext(os.path.basename(path))[0], partial=True)
            span = _Span(start)
            span.end = max(start, end)
            parsed.spans.append(span)
            try:
                rows = con.execute("SELECT step_payload FROM steps WHERE step_type=14 AND step_format=0 "
                                   "AND length(step_payload)<=65536 ORDER BY idx LIMIT 1").fetchall()
                if rows and isinstance(rows[0][0], bytes):
                    try:
                        span.request = agent_activity_report.trajectory_text(rows[0][0], [19, 2]) or None
                    except ValueError:
                        pass
                for (payload,) in con.execute(
                        "SELECT step_payload FROM steps WHERE step_type=132 AND status=3 AND step_format=0 "
                        "AND length(step_payload)<=65536 ORDER BY idx LIMIT 2000"):
                    if self._expired():
                        break
                    try:
                        if isinstance(payload, bytes) and agent_activity_report.trajectory_text(payload, [5, 4, 2]) == "write_file":
                            target = agent_activity_report.trajectory_text(payload, [140, 1, 2])
                            if target.startswith("/"):
                                span.edits.append(target)
                    except ValueError:
                        pass
            except Exception as error:     # sqlite3.DatabaseError: not the schema it should have
                raise ValueError("unreadable conversation") from error
            return parsed
        finally:
            con.close()


def _read_all(fd: int, limit: int) -> bytes:
    chunks, left = [], limit
    while left > 0:
        chunk = os.read(fd, min(left, 1 << 20))
        if not chunk:
            break
        chunks.append(chunk)
        left -= len(chunk)
    return b"".join(chunks)


def _pread_all(fd: int, size: int, offset: int) -> bytes:
    chunks, done = [], 0
    while done < size:
        chunk = os.pread(fd, min(size - done, 1 << 20), offset + done)
        if not chunk:
            break
        chunks.append(chunk)
        done += len(chunk)
    return b"".join(chunks)


def _birth(path: str) -> float | None:
    statx = getattr(os, "statx", None)
    if statx is not None:
        try:
            info = statx(path, getattr(os, "STATX_BTIME", 0x800), follow_symlinks=False)
            born = getattr(info, "stx_btime", None) or getattr(info, "st_birthtime", None)
            return float(born) if born else None
        except (OSError, TypeError, ValueError):
            return None
    try:
        return getattr(os.lstat(path), "st_birthtime", None)
    except OSError:
        return None
