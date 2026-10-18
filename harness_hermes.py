"""Hermes: its sessions from ~/.hermes/state.db (read-only).

Only these columns are read: of sessions the id, parent id, folder, times, model, tokens and cost;
of messages the session id and time. Never titles, prompts or message content (titles are
Asterism-only, in session_topics.py).
"""

import sqlite3
from pathlib import Path

import harnesses
from harnesses import Harness, Session, Sessions
from i18n import _

DB = Path.home() / ".hermes/state.db"


def collect(db_path: Path = DB) -> dict:
    """{session id: Session} for Hermes's top-level sessions, their child sessions counted in as
    subagent runs (Hermes does not name a type for them)."""
    db = harnesses.connect(db_path)
    if db is None:
        return {}
    try:
        rows = db.execute(
            "SELECT id, parent_session_id, cwd, started_at, COALESCE(last_activity_at, ended_at, started_at),"
            " model, input_tokens, COALESCE(output_tokens, 0) + COALESCE(reasoning_tokens, 0),"
            " cache_read_tokens, cache_write_tokens, COALESCE(actual_cost_usd, estimated_cost_usd)"
            " FROM sessions ORDER BY started_at DESC LIMIT ?", (harnesses.MAX_SESSIONS,)).fetchall()
        times: dict = {}
        for sid, stamp in db.execute(
                "SELECT session_id, timestamp FROM messages ORDER BY timestamp DESC LIMIT ?",
                (harnesses.MAX_MESSAGES,)):
            try:
                times.setdefault(str(sid), []).append(float(stamp))
            except (TypeError, ValueError):
                continue
    except sqlite3.Error:
        return {}
    finally:
        db.close()
    sessions, runs = {}, []
    for sid, parent, cwd, started, last, model, inp, out, cache_read, cache_write, cost in rows:
        s = Session(cwd=harnesses.folder(cwd))
        s.tokens[harnesses.model_name(model)] = [harnesses.number(inp), harnesses.number(out),
                                                 harnesses.number(cache_read), harnesses.number(cache_write)]
        fallback = [t for t in (started, last) if isinstance(t, (int, float))]
        harnesses.add_times(s, times.get(str(sid)) or fallback)
        s.cost = harnesses.money(cost)
        if parent:
            runs.append((str(parent), s))
        else:
            sessions[str(sid)] = s
    for parent, run in runs:
        if parent in sessions:
            harnesses.add_run(sessions[parent], "?", run)
    return sessions


def _note() -> str:
    return _("Numbers from {0}'s session database (no text is read). The API value is what the tokens "
             "would cost at API prices, not what a plan costs.").format("Hermes")


HARNESS = Harness("hermes", "Hermes", sessions=Sessions(collect, _note))
