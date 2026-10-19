"""OpenCode: identity, session numbers, security settings, sign-in, local LLMs, skills and MCP servers.

Sessions come from ~/.local/share/opencode/opencode.db (read-only). Only these columns are read: of
session_v2 the id, parent id, agent type, folder, times, tokens, cost and lines changed; of
session_message the session id and time. Never titles or message data (titles are Asterism-only, in
the Insights add-on).
"""

import re
import sqlite3
from pathlib import Path

import harnesses
from harnesses import Harness, LocalLlm, Mcp, Session, Sessions, Skills, Way, ref
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


def _access():
    from agent_status import Access
    from signin import Login
    return Access(_("An account or API key for any provider, or a local LLM from the map"),
                  "opencode auth login", (".local/share/opencode/auth.json",), (), local_ok=True,
                  logins=(Login(_("A provider (account or key)"), ("opencode", "auth", "login")),))


# --- pointed at a local LLM (connect.plan) --------------------------------------------------------

def llm_text(text: str, provider: str, label: str, url: str, models: list, model: str,
                  use_now: bool, remove: bool = False) -> str:
    import connect
    data = connect._json(text)
    providers = data.setdefault("provider", {})
    if remove:
        providers.pop(provider, None)
        if not providers:
            data.pop("provider")
        # Its model would point at a provider that is gone: OpenCode falls back to its own default.
        if str(data.get("model", "")).startswith(provider + "/"):
            data.pop("model")
    else:
        providers[provider] = {"npm": "@ai-sdk/openai-compatible", "name": label, "options": {"baseURL": url},
                               "models": {name: {"name": name} for name in models}}
        if use_now:
            data["model"] = f"{provider}/{model}"
    return connect._dump(data)


def _llm_connected(texts, provider):
    import connect
    return provider in connect._json(texts[0]).get("provider", {})


def _llm_active(texts, provider):
    import connect
    return str(connect._json(texts[0]).get("model", "")).startswith(provider + "/")


def _llm_transform(texts, provider, label, url, models, context, model, use_now, remove):
    return [llm_text(texts[0], provider, label, url, models, model, use_now, remove)]


# --- security settings (agent_security) ----------------------------------------------------------

def _security(report):
    """Its security settings (agent_security.report_for)."""
    from agent_security import Choice, HOME, Setting, _json_file
    path = HOME / ".config/opencode/opencode.json"
    data = _json_file(path)
    report.files.append(path)
    permission = data.get("permission")

    def current(key: str):
        if isinstance(permission, str):
            return permission
        value = permission.get(key) if isinstance(permission, dict) else None
        if isinstance(value, dict):
            return value.get("*", "ask") if set(value) == {"*"} else value   # patterns: custom
        if value is None and isinstance(permission, dict) and isinstance(permission.get("*"), str):
            return permission["*"]
        return value

    def three(allow_risk: int):
        return [Choice("ask", _("Ask"), _("Asks you each time."), 0),
                Choice("allow", _("Allow"), _("Does it without asking."), allow_risk),
                Choice("deny", _("Never"), _("The agent cannot do it."), 0)]

    report.settings += [
        Setting("permission.edit", _("Edit files"), _("Writing and changing files."), three(2),
                current("edit"), "allow", path, terms=("approval",), recommended="ask"),
        Setting("permission.bash", _("Run commands"), _("Shell commands, with all your rights."), three(3),
                current("bash"), "allow", path, terms=("approval", "rules"), recommended="ask"),
        Setting("permission.webfetch", _("Fetch web pages"), _("Reading web pages."), three(1),
                current("webfetch"), "allow", path, terms=("injection",), recommended="allow"),
        Setting("permission.external_directory", _("Outside the project"), _(
            "Reading or writing files outside the folder it was started in."), three(2),
                current("external_directory"), "ask", path, terms=("approval",), recommended="ask"),
    ]


HARNESS = Harness("opencode", "OpenCode", sessions=Sessions(collect, _note), label="OpenCode", icon="\ue902",
                  icon_font="omarchy", security=_security, usage=ref("agent_status", "opencode"),
                  access=_access,
                  local_llm=LocalLlm(("~/.config/opencode/opencode.json",), _llm_connected, _llm_active,
                                     _llm_transform),
                  skills=Skills(("~/.config/opencode/skills", "~/.claude/skills", "~/.agents/skills"),
                                "~/.config/opencode/skills"),
                  mcp=Mcp("~/.config/opencode/opencode.json", key="mcp", shape="opencode"),
                  short_model_flag=True,
                  install=(Way("mise", spec="opencode"),),
                  data=("~/.local/share/opencode", "~/.config/opencode"),
                  homepage="https://opencode.ai")
