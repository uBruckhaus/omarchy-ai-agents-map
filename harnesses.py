"""Harness adapters: what the app knows about each agent harness, one module per harness
(harness_<name>.py), registered in a fixed list here. No outside code is ever loaded.

Each module defines HARNESS = Harness(...). Its capabilities are optional (None: not supported):

- sessions: the harness's sessions as claude_sessions.Session objects (numbers only: folder, times,
  working time, tokens per model, lines changed, cost, subagent runs), so the details page shows the
  same "Projects, last 30 days" for every harness. Session titles and summaries are not here: they
  are read by session_topics.py, which only ships in Asterism (see edition.py).

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
class Harness:
    command: str                      # as in agents.py
    name: str
    sessions: Sessions | None = None


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

def _all() -> tuple:
    import harness_claude
    import harness_hermes
    import harness_opencode
    return (harness_claude.HARNESS, harness_hermes.HARNESS, harness_opencode.HARNESS)


def all_harnesses() -> tuple:
    return _all()


def get(command: str) -> Harness | None:
    return next((h for h in _all() if h.command == command), None)


def with_sessions() -> list:
    return [h for h in _all() if h.sessions is not None]
