"""What is it doing? (Maprisma task 074: agent_activity_report.dart, live_session_activity.dart,
jsonl_session_activity.dart, trajectory_activity.dart, agent_progress.dart).

For one running instance of an agent (its id is "<pid>:<kernel start ticks>", so a reused process
number is never taken for it): its current request, answers, tool calls and edited files from its
own session file, the progress it reported itself, the files changed in its project folders since
it started (a timeline), the helper processes it started, what started it (its parents and the
terminal window), its phase and whether it may be stuck.

Read-only and bounded: /proc of this account's own processes only; the session file must be one the
process has open (else the single one begun after it started in its folder), opened without
following a link, only its last 512 KiB (an Antigravity database: an in-memory snapshot through
readonly_sqlite, 64 rows); the project walk stops after 4,000 entries or 250 ms and skips hidden
folders, .git, build, node_modules, __pycache__ and credential files. Only text is shown, with
control characters taken out.

Progress files: ${XDG_RUNTIME_DIR}/ai-agents-map/progress/<pid>-<start>.json, written by
report_progress() for the agent instance that owns the calling process (an MCP tool in Maprisma).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import stat
import threading
import time
from pathlib import Path

import activity_log
from activity_log import iso, meta_text, parse_time

INSTANCE = re.compile(r"^([1-9][0-9]{0,9}):([0-9]{1,20})$")
UUID_DB = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.db$")
ROLLOUT = re.compile(r"^\d{4}/\d{2}/\d{2}/rollout-[^/]+\.jsonl$")
TERMINALS = re.compile(r"^(alacritty|kitty|foot|wezterm|konsole|ghostty|kgx|gnome-terminal|xterm|ptyxis)")
TAIL = 512 * 1024
MAX_LINE = 128 * 1024
EXCLUDED = {".git", "build", ".dart_tool", "__pycache__", "node_modules", "auth.json", "credentials.json",
            ".credentials.json"}
WALK_ENTRIES, WALK_SECONDS, WALK_DEPTH = 4000, 0.25, 8
STUCK_MS = 600_000
CONTROL = re.compile("[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")

# The session forms the app can read for What is it doing? and Collect history. Which one an agent
# writes, and where, is its adapter's [sessions] activity_reader (else its reader) and activity_dir.
FORMS = ("claude-files", "codex-jsonl", "pi-files", "trajectory-sqlite")


def session_facts(command: str) -> dict:
    """{reader, dir, extra_dir_option} of an agent's live session files ({} when none is known)."""
    import harnesses
    harness = harnesses.get(command)
    if harness is None:
        return {}
    reader = getattr(harness, "activity_reader", "") or harness.session_files
    directory = getattr(harness, "activity_dir", "")
    if reader not in FORMS or not isinstance(directory, str) or not directory.startswith("~/"):
        return {}
    return {"reader": reader, "dir": directory, "extra_dir_option": getattr(harness, "extra_dir_option", "") or ""}


# --- processes -------------------------------------------------------------------------------------

def _stat(pid: int, proc: str = "/proc") -> list | None:
    try:
        if os.stat(f"{proc}/{pid}").st_uid != os.getuid():
            return None
        with open(f"{proc}/{pid}/stat", "rb") as handle:
            data = handle.read(4096)
        return data[data.rindex(b")") + 2:].split()
    except (OSError, ValueError):
        return None


def start_ticks(pid: int, proc: str = "/proc") -> str | None:
    """Its kernel start time in clock ticks (field 22 of stat): with the pid, its identity."""
    fields = _stat(pid, proc)
    try:
        return fields[19].decode() if fields else None
    except IndexError:
        return None


def instance_id(pid: int, proc: str = "/proc") -> str | None:
    start = start_ticks(pid, proc)
    return f"{pid}:{start}" if start else None


def alive(pid: int, proc: str = "/proc") -> bool:
    fields = _stat(pid, proc)
    return bool(fields) and fields[0] not in (b"Z", b"X")


def _proc_read(pid: int, name: str, limit: int, proc: str = "/proc") -> bytes:
    """At most `limit` bytes of /proc/<pid>/<name> of this account's own process (b"" otherwise)."""
    try:
        if os.stat(f"{proc}/{pid}").st_uid != os.getuid():
            return b""
        with open(f"{proc}/{pid}/{name}", "rb", buffering=0) as handle:
            return handle.read(limit)
    except OSError:
        return b""


def comm(pid: int, proc: str = "/proc") -> str:
    return _proc_read(pid, "comm", 64, proc).decode("utf-8", "replace").strip()


def argv_of(pid: int, proc: str = "/proc") -> list[str]:
    raw = _proc_read(pid, "cmdline", 64 * 1024, proc)
    return [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a][:256]


def own_processes(proc: str = "/proc") -> list[int]:
    found = []
    try:
        names = os.listdir(proc)[:100000]
    except OSError:
        return found
    uid = os.getuid()
    for name in names:
        if name.isdigit():
            try:
                if os.stat(f"{proc}/{name}").st_uid == uid:
                    found.append(int(name))
            except OSError:
                pass
    return found


def descendants(pid: int, proc: str = "/proc") -> list[int]:
    """This account's processes started by it, directly or further down (one /proc pass)."""
    children: dict[int, list[int]] = {}
    for other in own_processes(proc):
        fields = _stat(other, proc)
        try:
            children.setdefault(int(fields[1]), []).append(other)
        except (TypeError, ValueError, IndexError):
            continue
    found, queue = [], list(children.get(pid, ()))
    while queue and len(found) < 256:
        child = queue.pop(0)
        if child in found or child == pid:
            continue
        found.append(child)
        queue.extend(children.get(child, ()))
    return found


def _started(pid: int, proc: str = "/proc") -> float:
    import agent_terminals
    return agent_terminals.started(pid, proc)


def _ancestors(pid: int, proc: str = "/proc") -> list[int]:
    import agent_terminals
    return agent_terminals.ancestors(pid, proc)


def _cwd(pid: int, proc: str = "/proc") -> str:
    import agent_terminals
    return agent_terminals.cwd(pid, proc)


def cpu(pid: int, proc: str = "/proc") -> int | None:
    import agent_activity
    return agent_activity.cpu_ticks(pid, proc)


# --- session files --------------------------------------------------------------------------------

def own_file(path: str) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()


def read_part(path: str, limit: int, tail: bool = True) -> bytes | None:
    """Its last (or first) `limit` bytes, opened without following a link; this account's regular file only."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            return None
        if tail and info.st_size > limit:
            os.lseek(fd, info.st_size - limit, os.SEEK_SET)
        chunks, left = [], limit
        while left > 0:
            chunk = os.read(fd, min(left, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            left -= len(chunk)
        return b"".join(chunks)
    except OSError:
        return None
    finally:
        os.close(fd)


def unavailable() -> dict:
    return {"status": "unavailable", "session_id": None, "request": None, "answers": [], "tool_calls": [],
            "edited_files": []}


_PATCH_HEADER = re.compile(r"^\*\*\* (?:(?:Add|Update|Delete) File|Move to): (.+)$", re.MULTILINE)
_PATCH_OK = re.compile(r"^(?:Wall time: [^\n]*\n(?:Output:\n)?)?Success\. Updated the following files:")
_CODEX_PREAMBLE = re.compile(r"^\s*(<environment_context>|<user_instructions>|<permissions instructions>|# AGENTS\.md instructions)")


def _time(value) -> str | None:
    moment = parse_time(value) if isinstance(value, str) else None
    return iso(moment) if moment else None


def jsonl_session_activity(tail: str, session_id: str, rollout: bool = False) -> dict:
    """The current turn of a JSONL session (Claude Code, Pi, Codex rollouts)."""
    request, answers, tools, edits = None, [], [], {}
    calls, patches = {}, {}
    for line in tail.splitlines():
        if len(line) > MAX_LINE:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        if rollout:
            message = row.get("payload") if row.get("type") == "response_item" else None
        else:
            message = row.get("message") if row.get("type") in ("message", "user", "assistant") else None
        if not isinstance(message, dict):
            continue
        at = _time(row.get("timestamp"))
        if rollout and message.get("type") != "message":
            kind, call = message.get("type"), message.get("call_id")
            if kind in ("function_call", "custom_tool_call"):
                name = meta_text(message.get("name"), 128)
                if not name or len(tools) >= 48:
                    continue
                tool = {"at": at, "name": name, "status": None}
                tools.append(tool)
                if isinstance(call, str):
                    calls[call] = tool
                    if name == "apply_patch" and isinstance(message.get("input"), str):
                        patches[call] = [p for p in (meta_text(m.group(1), 500) for m in
                                                     list(_PATCH_HEADER.finditer(message["input"]))[:48]) if p]
            elif kind in ("function_call_output", "custom_tool_call_output"):
                tool = calls.pop(call, None)
                if tool is None:
                    continue
                output, patch = message.get("output"), patches.pop(call, None)
                if patch is not None and isinstance(output, str) and _PATCH_OK.match(output):
                    tool["status"] = "completed"
                    for path in patch:
                        if len(edits) >= 48:
                            break
                        edits[path] = {"at": at, "path": path}
            continue
        content = message.get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else \
            [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
        wanted = ("input_text", "output_text") if rollout else ("text",)
        texts = []
        for block in blocks:
            if block.get("type") not in wanted:
                continue
            if rollout and message.get("role") == "user" and isinstance(block.get("text"), str) \
                    and _CODEX_PREAMBLE.match(block["text"]):
                continue
            value = meta_text(block.get("text"), 2000)
            if value:
                texts.append(value)
        text = "\n".join(texts[:8])
        if message.get("role") == "user" and text:
            request = {"at": at, "text": text}
            answers, tools, calls, patches, edits = [], [], {}, {}, {}
        elif message.get("role") == "assistant":
            if text and len(answers) < 32:
                answers.append({"at": at, "text": text})
            for block in blocks:
                if block.get("type") in ("toolCall", "tool_use"):
                    name = meta_text(block.get("name"), 128)
                    if name and len(tools) < 48:
                        tools.append({"at": at, "name": name, "status": None})
    available = request is not None or bool(answers) or bool(tools)
    return {"status": "unavailable" if not available else "partial" if request is None else "available",
            "session_id": session_id if available else None, "request": request, "answers": answers,
            "tool_calls": tools, "edited_files": list(edits.values())}


def proto_field(data: bytes, wanted: int) -> bytes | None:
    """The first length-delimited field `wanted` of a protobuf message; ValueError when it is malformed."""
    cursor = 0

    def varint() -> int:
        nonlocal cursor
        value = 0
        for i in range(10):
            if cursor >= len(data):
                break
            octet = data[cursor]
            cursor += 1
            if i == 9 and octet > 1:
                break
            value |= (octet & 127) << (7 * i)
            if octet < 128:
                return value
        raise ValueError("Invalid protobuf varint")
    while cursor < len(data):
        key = varint()
        tag = key >> 3
        if tag <= 0 or tag > 0x1fffffff:
            break
        wire = key & 7
        if wire == 0:
            varint()
        elif wire == 1:
            cursor += 8
        elif wire == 2:
            size = varint()
            if size > len(data) - cursor:
                raise ValueError("Invalid protobuf field length")
            if tag == wanted:
                return data[cursor:cursor + size]
            cursor += size
        elif wire == 5:
            cursor += 4
        else:
            raise ValueError("Unsupported protobuf wire type")
    if cursor != len(data):
        raise ValueError("Truncated protobuf field")
    return None


def trajectory_text(data: bytes, path) -> str:
    for tag in path:
        field = proto_field(data, tag)
        if field is None:
            return ""
        data = field
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("Invalid text") from None
    return re.sub("[\x00-\x08\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]", "", text).strip()[:2000]


def trajectory_activity(path: str, session_id: str, connect=None) -> dict:
    """The current turn of an Antigravity conversation database (format-0 steps only)."""
    import readonly_sqlite
    con = (connect or readonly_sqlite.connect)(Path(path))
    if con is None:
        return unavailable()
    try:
        rows = con.execute(
            "SELECT step_type, status, step_format, CASE WHEN length(step_payload) <= 65536 THEN step_payload END "
            "FROM steps WHERE idx >= (SELECT COALESCE(MAX(idx), 0) FROM steps WHERE step_type = 14) "
            "ORDER BY idx DESC LIMIT 64").fetchall()
    except Exception:
        return unavailable()
    finally:
        con.close()
    request, answers, tools, edits, partial = None, [], [], [], False
    for kind, status, form, payload in reversed(rows):
        if form != 0 or not isinstance(payload, bytes):
            partial = True
            continue
        try:
            if kind == 14:
                text = trajectory_text(payload, [19, 2])
                if text:
                    request = {"at": None, "text": text}
            elif kind == 15:
                text = trajectory_text(payload, [20, 1])
                if text:
                    answers.append({"at": None, "text": text})
            elif kind == 132:
                name = trajectory_text(payload, [5, 4, 2])
                if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,128}", name):
                    partial = True
                    continue
                done = status == 3
                tools.append({"at": None, "name": name, "status": "completed" if done else None})
                if done and name == "write_file":
                    target = trajectory_text(payload, [140, 1, 2])
                    if target.startswith("/"):
                        edits.append({"at": None, "path": target})
            else:
                partial = True
        except ValueError:
            partial = True
    available = request is not None or bool(answers) or bool(tools)
    return {"status": "unavailable" if not available else "partial" if partial else "available",
            "session_id": session_id if available else None, "request": request, "answers": answers[:32],
            "tool_calls": tools[:48], "edited_files": edits[:48]}


def _session_folder(reader: str, root: str, cwd: str) -> str:
    if reader == "pi-files":
        return os.path.join(root, "--" + cwd[1:].replace("/", "-") + "--")
    if reader == "claude-files":
        return os.path.join(root, re.sub(r"[^A-Za-z0-9]", "-", cwd))
    return root


def live_session_file(command: str, instance: str, facts: dict, agent_pids, proc: str = "/proc",
                      now: dt.datetime | None = None) -> str | None:
    """The session file one live instance writes (None when it cannot be told which)."""
    match = INSTANCE.match(instance or "")
    reader, directory = facts.get("reader"), facts.get("dir", "")
    if match is None or reader not in ("trajectory-sqlite", "pi-files", "claude-files", "codex-jsonl") \
            or not directory.startswith("~/"):
        return None
    pid, start = int(match.group(1)), match.group(2)
    home = activity_log.identity_home()
    root = os.path.normpath(str(home / directory[2:]))
    if not root.startswith(str(home) + "/") or not alive(pid, proc) or start_ticks(pid, proc) != start \
            or pid not in agent_pids:
        return None
    cwd = _cwd(pid, proc)
    if reader != "trajectory-sqlite" and not cwd.startswith("/"):
        return None
    folder = _session_folder(reader, root, cwd)
    rollout = reader == "codex-jsonl"

    def matches(path: str) -> bool:
        if rollout:
            inside = path.startswith(root + "/") and bool(ROLLOUT.match(os.path.relpath(path, root)))
        else:
            inside = os.path.dirname(path) == folder
        name = os.path.basename(path)
        return inside and (bool(UUID_DB.match(name)) if reader == "trajectory-sqlite"
                           else name.endswith(".jsonl") and not name.startswith("."))
    candidates = set()
    try:
        fds = os.listdir(f"{proc}/{pid}/fd")[:512]
    except OSError:
        fds = []
    for fd in fds:
        try:
            target = os.readlink(f"{proc}/{pid}/fd/{fd}")
        except OSError:
            continue
        if matches(target):
            candidates.add(target)
    if not candidates and reader != "trajectory-sqlite" \
            and not any(other != pid and _cwd(other, proc) == cwd for other in agent_pids):
        since = _started(pid, proc)
        if since > 0:
            today = (now or dt.datetime.now()).astimezone()
            folders = [os.path.join(root, f"{d:%Y}", f"{d:%m}", f"{d:%d}")
                       for d in (today, today - dt.timedelta(days=1))] if rollout else [folder]
            for where in folders:
                try:
                    names = sorted(os.listdir(where))[:128]
                except OSError:
                    continue
                for name in names:
                    path = os.path.join(where, name)
                    if not matches(path) or not own_file(path):
                        continue
                    head = (read_part(path, 8192, tail=False) or b"").decode("utf-8", "replace")
                    stamp = None
                    if rollout:
                        try:
                            record = json.loads(head.splitlines()[0]) if head else None
                        except ValueError:
                            continue
                        meta = record.get("payload") if isinstance(record, dict) else None
                        if not isinstance(record, dict) or record.get("type") != "session_meta" \
                                or not isinstance(meta, dict) or meta.get("cwd") != cwd or isinstance(meta.get("source"), dict):
                            continue
                        stamp = record.get("timestamp")
                    else:
                        found = re.search(r'"timestamp"\s*:\s*"([^"]+)"', head)
                        stamp = found.group(1) if found else None
                    began = parse_time(stamp) if isinstance(stamp, str) else None
                    if began is not None and began.timestamp() >= since - 2:
                        candidates.add(path)
    if len(candidates) != 1 or start_ticks(pid, proc) != start:
        return None
    return candidates.pop()


def live_session_activity(command: str, instance: str, facts: dict, agent_pids, proc: str = "/proc",
                          now: dt.datetime | None = None) -> dict:
    """The session file of one live instance, read (unavailable() when it cannot be told which)."""
    match = INSTANCE.match(instance or "")
    path = live_session_file(command, instance, facts, agent_pids, proc, now)
    if path is None or match is None:
        return unavailable()
    start, reader = match.group(2), facts["reader"]
    pid, rollout = int(match.group(1)), facts["reader"] == "codex-jsonl"
    session = os.path.splitext(os.path.basename(path))[0]
    if reader == "trajectory-sqlite":
        activity = trajectory_activity(path, session)
    else:
        tail = read_part(path, TAIL)
        activity = unavailable() if tail is None else jsonl_session_activity(tail.decode("utf-8", "replace"),
                                                                            session, rollout=rollout)
    return activity if start_ticks(pid, proc) == start else unavailable()


# --- token usage of a live instance (the activity log's live rows) ---------------------------------

USAGE_MAX = 8 << 20          # a larger session file gives no usage (unknown), never a partial sum


def session_usage(command: str, pids, proc: str = "/proc", cache: dict | None = None) -> dict | None:
    """{session, model, input, output}: the token totals of the one running instance's own session file
    (instance-checked as for What is it doing?), or None. Read whole up to 8 MiB, numbers and model names
    only; a file unchanged since the last look (cache) is not read again."""
    pids = [p for p in list(pids)[:2] if alive(p, proc)]
    if len(pids) != 1:
        return None
    ident = instance_id(pids[0], proc)
    facts = session_facts(command)
    if not ident or facts.get("reader") not in ("claude-files", "codex-jsonl", "pi-files"):
        return None
    path = live_session_file(command, ident, facts, pids, proc)
    if path is None:
        return None
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > USAGE_MAX:
        return None
    key = (path, info.st_size, info.st_mtime_ns)
    if cache is not None and cache.get("key") == key:
        totals = cache["totals"]
    else:
        data = read_part(path, USAGE_MAX + 1, tail=False)
        if data is None or len(data) > USAGE_MAX:
            return None
        totals = usage_totals(data.decode("utf-8", "replace"), facts["reader"] == "codex-jsonl")
        if cache is not None:
            cache.update(key=key, totals=totals)
    if totals is None or instance_id(pids[0], proc) != ident:
        return None
    return {"session": f"{ident}:{path}", **totals}


def usage_totals(text: str, rollout: bool) -> dict | None:
    """Input and output tokens of a session (Codex: its last running total; else each message once)."""
    models, seen, last = set(), {}, None
    for line in text.splitlines():
        if len(line) > MAX_LINE or '"usage"' not in line and "token_count" not in line and '"model"' not in line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        message = row.get("message") if isinstance(row.get("message"), dict) else {}
        if rollout:
            if row.get("type") == "turn_context" and isinstance(payload.get("model"), str):
                models.add(payload["model"])
            info = payload.get("info") if payload.get("type") == "token_count" else None
            total = info.get("total_token_usage") if isinstance(info, dict) else None
            if isinstance(total, dict) and type(total.get("input_tokens")) is int and type(total.get("output_tokens")) is int:
                last = (total["input_tokens"], total["output_tokens"])
            continue
        usage = message.get("usage")
        if not isinstance(usage, dict):
            continue
        tin, tout = usage.get("input_tokens", usage.get("input")), usage.get("output_tokens", usage.get("output"))
        if type(tin) is not int or type(tout) is not int or tin < 0 or tout < 0:
            continue
        if isinstance(message.get("model"), str):
            models.add(message["model"])
        key = str(message.get("id") or row.get("uuid") or row.get("id") or len(seen))
        before = seen.get(key, (0, 0))
        seen[key] = (max(tin, before[0]), max(tout, before[1]))
    if rollout:
        if last is None:
            return None
        tin, tout = last
    else:
        if not seen:
            return None
        tin, tout = sum(v[0] for v in seen.values()), sum(v[1] for v in seen.values())
    return {"model": next(iter(models)) if len(models) == 1 else None, "input": tin, "output": tout}


# --- progress the agent reports itself ---------------------------------------------------------

def progress_dir() -> Path | None:
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    return Path(runtime) / "ai-agents-map" / "progress" if runtime.startswith("/") else None


def _clean(text: str) -> str:
    return CONTROL.sub(" ", text).strip()


def read_progress(pid: int, start: str) -> dict | None:
    folder = progress_dir()
    if folder is None:
        return None
    data = read_part(str(folder / f"{pid}-{start}.json"), 8192, tail=False)
    try:
        report = json.loads(data) if data and len(data) < 8192 else None
    except ValueError:
        return None
    if not isinstance(report, dict):
        return None
    step, done, total, note, at = (report.get(k) for k in ("step", "done", "total", "note", "at"))
    if not isinstance(step, str) or type(done) is not int or type(total) is not int or done < 0 or total < 1 \
            or done > total or (note is not None and not isinstance(note, str)) or parse_time(at) is None:
        return None
    return {"step": _clean(step)[:240], "done": done, "total": total,
            "note": _clean(note)[:1000] if isinstance(note, str) else None, "at": at}


def progress(command: str, instance: str, agent_pids, proc: str = "/proc") -> dict | None:
    match = INSTANCE.match(instance or "")
    if match is None:
        return None
    pid, start = int(match.group(1)), match.group(2)
    if not alive(pid, proc) or start_ticks(pid, proc) != start or pid not in agent_pids:
        return None
    report = read_progress(pid, start)
    return report if start_ticks(pid, proc) == start else None


def progress_snapshot(pids_by_agent: dict, proc: str = "/proc") -> dict:
    """{agent: [{id, pid, progress}]} of every running instance: the progress each reported itself
    (for the map's boxes and a terminal's progress line)."""
    found = {}
    for command, pids in pids_by_agent.items():
        for pid in list(pids)[:32]:
            start = start_ticks(pid, proc)
            if start is None or not alive(pid, proc):
                continue
            report = read_progress(pid, start)
            if start_ticks(pid, proc) == start:
                found.setdefault(command, []).append({"id": f"{pid}:{start}", "pid": pid, "progress": report})
    return found


def report_progress(arguments: dict, agent_pids, caller: int | None = None, proc: str = "/proc") -> bool:
    """report_progress(step, done, total, note?) from a process of a running agent instance: written for
    the instance among its parents, nobody else's. ValueError for invalid arguments."""
    step, done, total, note = (arguments.get(k) for k in ("step", "done", "total", "note"))
    if any(k not in ("step", "done", "total", "note") for k in arguments) or not isinstance(step, str) \
            or not step.strip() or len(step) > 240 or type(done) is not int or type(total) is not int \
            or done < 0 or total < 1 or total > 1_000_000 or done > total \
            or (note is not None and (not isinstance(note, str) or len(note) > 1000)):
        raise ValueError("Invalid progress arguments")
    caller = os.getpid() if caller is None else caller
    owner = next((p for p in [caller] + _ancestors(caller, proc) if p in set(agent_pids)), None)
    folder = progress_dir()
    if owner is None or folder is None:
        return False
    start = start_ticks(owner, proc)
    label = _clean(step)
    if start is None or not alive(owner, proc) or not label:
        return False
    import ai_core
    try:
        ai_core.atomic_write(folder / f"{owner}-{start}.json", json.dumps(
            {"step": label, "done": done, "total": total, "note": _clean(note) if isinstance(note, str) else None,
             "at": iso(dt.datetime.now(dt.timezone.utc))}))
    except OSError:
        return False
    return start_ticks(owner, proc) == start


# --- the project timeline ---------------------------------------------------------------------------

def activity_timeline(roots, since: float, home: Path | None = None) -> list[dict]:
    """Files changed in these folders since `since` (epoch seconds), grouped: a new group after 5
    minutes or when the kind changes (test files: testing, else editing); the newest 120 files."""
    if not since:
        return []
    home = str(home or activity_log.identity_home())
    deadline = time.monotonic() + WALK_SECONDS
    files: dict[str, float] = {}
    visited = 0

    def budget() -> bool:
        return visited < WALK_ENTRIES and time.monotonic() < deadline

    def walk(path: str, depth: int):
        nonlocal visited
        if depth > WALK_DEPTH or not budget():
            return
        try:
            entries = os.scandir(path)
        except OSError:
            return
        with entries:
            for entry in entries:
                if not budget():
                    break
                visited += 1
                if entry.name.startswith(".") or entry.name in EXCLUDED:
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISDIR(info.st_mode):
                    walk(entry.path, depth + 1)
                elif stat.S_ISREG(info.st_mode) and info.st_mtime >= since:
                    files[entry.path] = info.st_mtime
    for root in list(roots)[:16]:
        if not isinstance(root, str) or not os.path.isabs(root) or root in ("/", home) or os.path.realpath(root) != root:
            continue
        walk(root, 0)
    groups: list[dict] = []
    last = None
    for path, mtime in sorted(files.items(), key=lambda item: item[1])[-120:]:
        kind = "testing" if re.search(r"(^test_|_test\.|\.test\.|\.spec\.)", os.path.basename(path)) else "editing"
        at = iso(dt.datetime.fromtimestamp(mtime, dt.timezone.utc))
        if last is None or mtime - last >= 300 or groups[-1]["kind"] != kind:
            groups.append({"from": at, "to": at, "kind": kind, "files": []})
        groups[-1]["to"] = at
        groups[-1]["files"].append({"path": path, "at": at, "attribution": "observed"})
        last = mtime
    return groups


def activity_phase(request: bool, progress: dict | None, timeline: list, testing: bool) -> str:
    if progress is not None and progress["done"] == progress["total"]:
        return "finished"
    if testing:
        return "testing"
    if timeline:
        return timeline[-1]["kind"]
    return "planning" if request or progress is not None else "unknown"


class ActivityIdle:
    """Possibly stuck: active, yet nothing changed (its evidence and CPU time) for 10 minutes."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.samples: dict[str, tuple] = {}
        self.lock = threading.Lock()          # refreshes of several windows may overlap on workers

    def observe(self, instance: str, evidence: str, ticks: list, active: bool, elapsed_ms: int | None = None):
        with self.lock:
            return self._observe(instance, evidence, ticks, active, elapsed_ms)

    def _observe(self, instance: str, evidence: str, ticks: list, active: bool, elapsed_ms: int | None = None):
        now = int(self.clock() * 1000) if elapsed_ms is None else elapsed_ms
        previous = self.samples.pop(instance, None)
        for key in [k for k, s in self.samples.items() if now - s[0] > STUCK_MS]:
            del self.samples[key]
        if not active or any(t is None for t in ticks):
            return None
        unchanged = previous is not None and now - previous[0] <= 15000 and previous[3] == evidence \
            and previous[2] == list(ticks)
        since = previous[1] if unchanged else now
        self.samples[instance] = (now, since, list(ticks), evidence)
        while len(self.samples) > 128:
            del self.samples[next(iter(self.samples))]
        return now - since if now - since >= STUCK_MS else None


TEST_RUNNERS = {"pytest", "py.test", "jest", "vitest", "cargo", "go", "dart", "flutter", "npm", "pnpm", "yarn", "python",
                "python3"}


def _helper_label(argv: list[str], name: str) -> str:
    program = os.path.basename(argv[0]) if argv else name
    words = argv[1:5]
    if program in ("pytest", "py.test") or (program.startswith("python") and "pytest" in words) \
            or (program.startswith("python") and "unittest" in words) or (program in TEST_RUNNERS and "test" in words):
        return f"{program} test"
    return name or program


def agent_activity_report(command: str, selected: str | None, agent_pids, windows: dict | None = None,
                          monitor: ActivityIdle | None = None, proc: str = "/proc") -> dict:
    """agents.activity_report: {status, observed_at, instances, report}. agent_pids: this agent's
    running instances (provider_watch.agent_instances); windows: {pid: title} of terminal windows."""
    now = dt.datetime.now(dt.timezone.utc)
    pids = [p for p in list(agent_pids)[:32] if alive(p, proc)]
    instances = []
    for pid in pids:
        ident = instance_id(pid, proc)
        if ident:
            instances.append({"id": ident, "pid": pid, "label": f"{command} · {pid}"})
    if selected is None:
        instance = instances[0] if len(instances) == 1 else None
    else:
        instance = next((i for i in instances if i["id"] == selected), None)
    result = {"observed_at": iso(now), "instances": instances, "report": None,
              "status": "available" if instance else "choose_instance" if selected is None and len(instances) > 1
              else "unavailable"}
    if instance is None:
        return result
    pid, ident = instance["pid"], instance["id"]
    since = _started(pid, proc)
    origin = _ancestors(pid, proc)
    names = {p: comm(p, proc) for p in origin}
    terminal = next((p for p in origin if TERMINALS.match(names.get(p, ""))), None)
    title = (windows or {}).get(terminal) if terminal else None
    title = meta_text(title, 200) or None if isinstance(title, str) else None
    helpers = []
    for other in descendants(pid, proc)[:32]:
        started = _started(other, proc)
        if started < since:
            continue
        helpers.append({"pid": other, "label": meta_text(_helper_label(argv_of(other, proc), comm(other, proc)), 80),
                        "elapsed_ms": max(0, int((now.timestamp() - started) * 1000))})
    cwd = _cwd(pid, proc)
    roots = {cwd} if cwd else set()
    facts = session_facts(command)
    option = facts.get("extra_dir_option", "")
    args = argv_of(pid, proc)
    if option:
        i = 1
        while i < len(args):
            value = ""
            if args[i] == option and i + 1 < len(args):
                i += 1
                value = args[i]
            elif args[i].startswith(option + "="):
                value = args[i][len(option) + 1:]
            if value and cwd:
                roots.add(os.path.normpath(os.path.join(cwd, value)))
            i += 1
    timeline = activity_timeline(roots, since)
    details = live_session_activity(command, ident, facts, pids, proc)
    reported = progress(command, ident, pids, proc)
    if instance_id(pid, proc) != ident:
        result["status"] = "unavailable"
        return result
    phase = activity_phase(details["request"] is not None, reported, timeline,
                           any(h["label"].endswith(" test") for h in helpers))
    idle = None
    if monitor is not None:
        idle = monitor.observe(ident, json.dumps([details, reported, timeline, [h["pid"] for h in helpers]],
                                                 sort_keys=True, default=str),
                               [cpu(p, proc) for p in [pid] + [h["pid"] for h in helpers]],
                               phase != "finished" and (reported is not None or (
                                   details["request"] is not None and not details["answers"])))
    result["report"] = {
        "instance": instance, "details": details, "progress": reported, "timeline": timeline, "helpers": helpers,
        "origin": {"ancestors": [{"pid": p, "label": names[p]} for p in origin if names.get(p)],
                   "terminal_title": title, "close_risk": "possible" if title else "unknown"},
        "phase": phase, "stuck": {"idle_ms": idle} if idle is not None else None,
    }
    return result


# --- as text --------------------------------------------------------------------------------------

PHASES = {"planning": "Planning", "editing": "Editing", "testing": "Writing or running tests",
          "finished": "Finished"}


def _clock(value) -> str:
    moment = parse_time(value)
    return moment.astimezone().strftime("%H:%M") if moment else ""


def report_text(report: dict) -> str:
    """The whole report as text (the dialog's content and its Copy): every section."""
    from i18n import _
    details, reported, origin = report["details"], report["progress"], report["origin"]
    request = details["request"]
    lines = [f"{_('What is it doing?')} · {report['instance']['label']}",
             _(PHASES.get(report["phase"], "No details available")), ""]
    if report.get("stuck") is not None:
        lines.append(_("Possibly stuck"))
    lines.append(_("Current work"))
    if request is not None:
        lines.append(request["text"])
    lines += [a["text"] for a in details["answers"]]
    lines += [f"• {t['name']}" for t in details["tool_calls"]]
    lines += [f"• {e['path']}" for e in details["edited_files"]]
    if request is None and not details["answers"] and not details["tool_calls"]:
        lines.append(_("No details available"))
    if reported is not None:
        lines.append(f"{reported['done']}/{reported['total']} · {reported['step']}")
        if reported.get("note"):
            lines.append(reported["note"])
    lines += ["", _("Activity timeline")]
    for group in report["timeline"]:
        kind = _("Writing or running tests") if group["kind"] == "testing" else _("Editing")
        lines.append(f"{_clock(group['from'])}–{_clock(group['to'])} {kind}\n"
                     + "\n".join(f["path"] for f in group["files"]))
    lines += ["", _("Helper processes")]
    lines += [f"{h['label']} · {activity_log.duration_text(h['elapsed_ms'])}" for h in report["helpers"]]
    lines += ["", _("Started by")]
    if origin.get("terminal_title"):
        lines.append(origin["terminal_title"])
    if origin.get("close_risk") == "possible":
        lines.append(_("Closing this terminal may stop the work."))
    lines.append(" ← ".join(f"{p['label']} ({p['pid']})" for p in origin["ancestors"]))
    return "\n".join(lines)
