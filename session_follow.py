#!/usr/bin/env python3
"""Follow what a running agent does, in a terminal: `session_follow.py <form> <pid> [<name>]`.

Started by the map's console for an instance that runs in the background (agent_terminals): no
window shows it, so this shows what it writes, as it writes it:

- its session file, by the form its adapter's [sessions] reader names (<form>; "-": none):
  pi-files ~/.pi/agent/sessions/<folder>/*.jsonl, claude-files ~/.claude/projects/<folder>/*.jsonl
  (the folder named after its working folder), the one written since it started: what was asked,
  its answers, the tools it calls and the first line of what they gave back;
- else its output, when that goes to a file (`pi -p … > answer.md`).

Read-only, this account's own regular files only, never through a link, each read bounded. What the
agent wrote is shown as text: control characters (escape sequences that could drive the terminal)
are taken out. It stops when the agent has ended.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import time
from pathlib import Path

import agent_terminals

HOME = Path.home()
PI_SESSIONS = HOME / ".pi/agent/sessions"
CLAUDE_PROJECTS = HOME / ".claude/projects"
CHUNK = 1024 * 1024              # bytes read per look
MAX_LINE = 4 * 1024 * 1024       # a longer session line is skipped
SHOWN_BEFORE = 30                # entries of what was written before this started
TEXT_MOST = 1500                 # characters of one answer or question
TOOL_MOST = 160                  # characters of a tool call's arguments or result
CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def clean(text, most: int) -> str:
    text = CONTROL.sub("", str(text or "").replace("\r\n", "\n"))
    return text if len(text) <= most else text[:most - 1].rstrip() + "…"


def one_line(text, most: int) -> str:
    return clean(" ".join(str(text or "").split()), most)


def own_file(path: Path) -> os.stat_result | None:
    try:
        info = os.lstat(path)
    except OSError:
        return None
    return info if stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() else None


def alive(pid: int, since: float = 0.0) -> bool:
    """Still this account's process, not ended (a zombie counts as ended) and, with since, still the
    one that was followed, not a later one with the same number."""
    return agent_terminals.alive(pid, since)


def output_file(pid: int) -> Path | None:
    """Its standard output, when that is one of this account's regular files."""
    try:
        target = Path(os.readlink(f"/proc/{pid}/fd/1"))
    except OSError:
        return None
    return target if target.is_absolute() and own_file(target) is not None else None


FOLLOWED = ("pi-files", "claude-files")      # the session forms this can follow


def session_folder(form: str, cwd: str) -> Path | None:
    if not cwd.startswith("/"):
        return None
    if form == "pi-files":
        return PI_SESSIONS / ("--" + cwd[1:].replace("/", "-") + "--")
    if form == "claude-files":
        return CLAUDE_PROJECTS / re.sub(r"[^A-Za-z0-9]", "-", cwd)
    return None


def session_file(form: str, pid: int, since: float, siblings: list | None = None) -> Path | None:
    """The session file it writes now: open in it, else the newest one that is its own: begun after it
    started and before another instance of it in the same folder started (that one's are the
    other's; Claude's /clear begins a new file in the same instance). siblings: [start time] of
    those other instances (None: looked up in /proc)."""
    folder = session_folder(form, _cwd(pid))
    if folder is None:
        return None
    try:
        for fd in os.listdir(f"/proc/{pid}/fd"):
            target = Path(os.readlink(f"/proc/{pid}/fd/{fd}"))
            if target.parent == folder and target.suffix == ".jsonl" and own_file(target) is not None:
                return target
    except OSError:
        pass
    siblings = _siblings(pid) if siblings is None else siblings
    others = sorted(t for t in siblings if t > since)
    until = others[0] - 1 if others else float("inf")      # from then on a later instance may write there
    best, newest = None, since - 5
    try:
        entries = list(os.scandir(folder))[:5000]
    except OSError:
        return None
    for entry in entries:
        if entry.name.endswith(".jsonl"):
            info = own_file(Path(entry.path))
            if info is None or info.st_mtime < newest:
                continue
            begun = first_time(Path(entry.path)) or info.st_mtime
            if since - 5 <= begun <= until or (begun < since - 5 and not siblings):   # its own, or one it resumed
                best, newest = Path(entry.path), info.st_mtime
    return best


TIMESTAMP = re.compile(rb'"timestamp"\s*:\s*"([0-9T:.+-]{19,40}Z?)"')


def first_time(path: Path) -> float | None:
    """When a session file was begun: the first timestamp in its first lines (Pi's and Claude
    Code's entries carry one), None when it has none."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)   # as Follower: no link, no FIFO wait
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            return None
        found = TIMESTAMP.search(os.read(fd, 64 * 1024))
    except OSError:
        return None
    finally:
        os.close(fd)
    if not found:
        return None
    try:
        import datetime as dt
        return dt.datetime.fromisoformat(found.group(1).decode().replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _siblings(pid: int) -> list:
    """Start times of this account's other processes of the same program in the same folder."""
    try:
        comm = Path(f"/proc/{pid}/comm").read_text()[:64]
    except OSError:
        return []
    where, found = _cwd(pid), []
    for name in os.listdir("/proc")[:100000]:
        if not name.isdigit() or int(name) == pid:
            continue
        try:
            if os.stat(f"/proc/{name}").st_uid != os.getuid() or Path(f"/proc/{name}/comm").read_text()[:64] != comm:
                continue
        except OSError:
            continue
        if _cwd(int(name)) == where:
            found.append(_started(int(name)))
    return found


def _cwd(pid: int) -> str:
    return agent_terminals.cwd(pid)


def _identity(pid: int) -> float:
    """Its kernel start time, 0 when it cannot be read (then it is not followed)."""
    return agent_terminals.started(pid)


def _started(pid: int) -> float:
    return agent_terminals.started(pid) or time.time()


# --- what a session line says ---------------------------------------------------------------

def _arguments(value) -> str:
    if isinstance(value, dict):
        for key in ("command", "file_path", "path", "pattern", "query", "url", "description", "prompt"):
            if isinstance(value.get(key), str):
                return value[key]
        return json.dumps(value, ensure_ascii=False)
    return str(value or "")


def _result(content) -> str:
    if isinstance(content, list):
        content = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
    lines = [line for line in str(content or "").splitlines() if line.strip()]
    first = lines[0] if lines else ""
    return first + (f"  (+{len(lines) - 1} lines)" if len(lines) > 1 else "")


def render(entry: dict) -> list[str]:
    """Readable lines for one session entry (Pi's or Claude Code's form); [] for the rest."""
    if not isinstance(entry, dict):
        return []
    kind = entry.get("type")
    if kind not in ("message", "user", "assistant"):
        return []
    message = entry.get("message")
    if not isinstance(message, dict):
        return []
    role = message.get("role")
    content = message.get("content")
    if role == "toolResult":                        # Pi: a tool's answer is a message of its own
        return [f"   ↳ {one_line(_result(content), TOOL_MOST)}"]
    blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
    if not isinstance(blocks, list):
        return []
    lines = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        what = block.get("type")
        if what == "text" and str(block.get("text", "")).strip():
            text = clean(block["text"], TEXT_MOST).strip()
            if role == "user":
                lines.append("› " + text.replace("\n", "\n  "))
            elif role == "assistant":
                lines.append("● " + text.replace("\n", "\n  "))
        elif what in ("toolCall", "tool_use"):
            args = block.get("arguments") if what == "toolCall" else block.get("input")
            lines.append(f"⚙ {one_line(block.get('name'), 40)}  {one_line(_arguments(args), TOOL_MOST)}")
        elif what == "tool_result":                  # Claude Code: inside a user message
            lines.append(f"   ↳ {one_line(_result(block.get('content')), TOOL_MOST)}")
    return lines


# --- following ------------------------------------------------------------------------------

class Follower:
    """Reads what is appended to a file, a bounded piece at a time. Each look opens it anew without
    following a link or blocking (a FIFO put in its place cannot hang it) and checks the opened file
    itself: this account's, a regular file. A file truncated or replaced (log rotation) is read
    again from its start, without a half line left over from the old one."""

    def __init__(self, path: Path):
        self.path, self.offset, self.rest, self.identity = path, 0, b"", None

    def new_bytes(self) -> bytes:
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError:
            return b""
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                return b""
            identity = (info.st_dev, info.st_ino)
            if self.identity is not None and (identity != self.identity or info.st_size < self.offset):
                self.offset, self.rest = 0, b""          # another file now, or cut short: from its start
            self.identity = identity
            if info.st_size <= self.offset:
                return b""
            os.lseek(fd, self.offset, os.SEEK_SET)
            data = os.read(fd, CHUNK)
        except OSError:
            return b""
        finally:
            os.close(fd)
        self.offset += len(data)
        return data

    def new_lines(self) -> list[bytes]:
        fresh = self.new_bytes()                 # first: a cut or replaced file drops the half line kept
        data = self.rest + fresh
        *lines, self.rest = data.split(b"\n")
        if len(self.rest) > MAX_LINE:
            self.rest = b""
        return [line for line in lines if line.strip() and len(line) <= MAX_LINE]


def entries(lines) -> list[str]:
    shown = []
    for line in lines:
        try:
            shown.extend(render(json.loads(line)))
        except ValueError:
            continue
    return shown


def follow(form: str, pid: int, name: str, out=sys.stdout, sleep=time.sleep, grace: float = 1.5) -> int:
    """Shows what it wrote and goes on until it has ended (and its file stayed quiet for `grace`)."""
    name = one_line(name, 80)                            # a folder or file name can hold escape sequences too
    identity = _identity(pid)
    if not identity:                                      # gone already, or not this account's: who it is cannot be told
        print(f"{name} has ended.", file=out)
        return 1
    since = _started(pid)
    where = _cwd(pid)
    print(f"{name} · process {pid} · {one_line(where.replace(str(HOME), '~', 1), 300)}", file=out)
    session = session_file(form, pid, since)             # what it does step by step, where it keeps that
    output = None if session else output_file(pid)
    source = session or output
    if source is None:
        print(f"{name} runs in the background without a file the app can follow (no session file, "
              f"its output goes to the program that started it).", file=out)
        return 1
    print(("Its output: " if output else "Its session: ") + one_line(str(source).replace(str(HOME), "~", 1), 300),
          file=out)
    print("─" * 60, file=out)
    reader = Follower(source)
    if output is not None:
        info = own_file(source)
        reader.offset = max(0, (info.st_size if info else 0) - 8192)      # the last few lines first
    first = True
    quiet_since = None
    while True:
        lines = reader.new_lines()
        if output is not None:
            for line in lines:
                print(clean(line.decode("utf-8", "replace"), TEXT_MOST), file=out)
        else:
            shown = entries(lines)
            if first and len(shown) > SHOWN_BEFORE:
                print(f"… ({len(shown) - SHOWN_BEFORE} earlier lines)", file=out)
                shown = shown[-SHOWN_BEFORE:]
            for line in shown:
                print(line, file=out)
        out.flush()
        first = False
        if not alive(pid, identity):
            quiet_since = quiet_since or time.monotonic()
            if lines or time.monotonic() - quiet_since < grace:
                sleep(0.5)
                continue
            print("─" * 60, file=out)
            print(f"{name} has ended.", file=out)
            return 0
        sleep(0.5)


def main(argv) -> int:
    if len(argv) < 2 or not re.fullmatch(r"[a-z0-9-]{1,64}", argv[0]) or not argv[1].isdigit():
        print("usage: session_follow.py <form> <pid> [<name>]")
        return 2
    try:
        return follow(argv[0], int(argv[1]), one_line(argv[2] if len(argv) > 2 else "The agent", 60))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
