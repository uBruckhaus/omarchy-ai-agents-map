"""OpenCode: its sessions from ~/.local/share/opencode/opencode.db (read-only).

Only these columns are read: of session_v2 the id, parent id, agent type, folder, times, tokens,
cost and lines changed; of session_message the session id and time. Never titles or message data
(titles are Asterism-only, in session_topics.py).
"""

import re
import sqlite3
from pathlib import Path

import harnesses
from harnesses import Harness, Session, Sessions
from i18n import _

DB = Path.home() / ".local/share/opencode/opencode.db"
_AGENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._:-]{0,63}")


def collect(db_path: Path = DB) -> dict:
    """{session id: Session} for OpenCode's top-level sessions, their subagent runs counted in."""
    db = harnesses.connect(db_path)
    if db is None:
        return {}
    try:
        rows = db.execute(
            "SELECT id, parent_id, agent, directory, time_created, time_updated, model, tokens_input,"
            " COALESCE(tokens_output, 0) + COALESCE(tokens_reasoning, 0), tokens_cache_read,"
            " tokens_cache_write, cost, summary_additions, summary_deletions"
            " FROM session_v2 ORDER BY time_created DESC LIMIT ?", (harnesses.MAX_SESSIONS,)).fetchall()
        times: dict = {}
        for sid, created in db.execute(
                "SELECT session_id, time_created FROM session_message ORDER BY time_created DESC LIMIT ?",
                (harnesses.MAX_MESSAGES,)):
            times.setdefault(str(sid), []).append(harnesses.number(created) / 1000.0)
    except sqlite3.Error:
        return {}
    finally:
        db.close()
    sessions, runs = {}, []
    for sid, parent, agent, directory, created, updated, model, inp, out, cache_read, cache_write, cost, \
            added, removed in rows:
        s = Session(cwd=harnesses.folder(directory))
        s.tokens[harnesses.model_name(model)] = [harnesses.number(inp), harnesses.number(out),
                                                 harnesses.number(cache_read), harnesses.number(cache_write)]
        harnesses.add_times(s, times.get(str(sid)) or
                            [harnesses.number(created) / 1000.0, harnesses.number(updated) / 1000.0])
        s.cost = harnesses.money(cost)
        s.lines_added, s.lines_removed = harnesses.number(added), harnesses.number(removed)
        if parent:
            kind = agent if isinstance(agent, str) and _AGENT.fullmatch(agent) else "?"
            runs.append((str(parent), kind, s))
        else:
            sessions[str(sid)] = s
    for parent, kind, run in runs:
        if parent in sessions:
            harnesses.add_run(sessions[parent], kind, run)
    return sessions


def _note() -> str:
    return _("Numbers from {0}'s session database (no text is read). The API value is what the tokens "
             "would cost at API prices, not what a plan costs.").format("OpenCode")


HARNESS = Harness("opencode", "OpenCode", sessions=Sessions(collect, _note))
