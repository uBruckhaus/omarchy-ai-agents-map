"""Hermes: identity, session numbers, security settings, sign-in, local LLMs, skills and MCP servers.

Sessions come from ~/.hermes/state.db (read-only). Only these columns are read: of sessions the id,
parent id, folder, times, model, tokens and cost; of messages the session id and time. Never titles,
prompts or message content (titles: the Insights add-on).
"""

import re
import sqlite3
from pathlib import Path

import harnesses
from harnesses import Harness, LocalLlm, Mcp, Session, Sessions, Skills, Way, ref
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


def _access():
    from agent_status import Access
    from signin import Login
    return Access(_("A provider key (hermes setup), or a local LLM from the map"),
                  "hermes setup", (".hermes/auth.json", ".hermes/.env"), (), local_ok=True,
                  logins=(Login(_("Setup wizard (account or key)"), ("hermes", "setup")),))


# --- pointed at a local LLM (connect.plan) --------------------------------------------------------

def llm_text(text: str, provider: str, label: str, url: str, models: list, context: dict,
                model: str, use_now: bool, remove: bool = False) -> str:
    import connect
    entry = [f"  {provider}:", f"    name: {label}", f"    api: {url}", "    api_key: local",
             f"    default_model: {model}", "    discover_models: false", "    models:"]
    for name in models:
        if context.get(name):
            entry += [f"      {name}:", f"        context_length: {context[name]}"]
        else:
            entry.append(f"      {name}: {{}}")
    entry_text = "\n".join(entry) + "\n"

    block = connect._top_block(text, "providers")
    if block is None:
        if not remove:
            text = text + ("" if text.endswith("\n") or not text else "\n") + "providers:\n" + entry_text
    else:
        start, end = block
        body = text[start:end]
        header, _nl, rest = body.partition("\n")
        # Our entry: from "  <provider>:" to the next two-space key.
        rest = re.sub(rf"^  {re.escape(provider)}:.*\n(?:(?:    |\t\t).*\n?|\s*\n)*", "", rest, flags=re.MULTILINE)
        if not remove:
            rest = rest.rstrip("\n") + ("\n" if rest.strip() else "") + entry_text
        header = "providers:" if header.strip() in ("providers: {}", "providers:{}") else header
        new_body = header + "\n" + rest if rest.strip() else ("" if remove else header + "\n")
        text = text[:start] + new_body + text[end:]

    if use_now and not remove:
        block = connect._top_block(text, "model")
        lines = [f"  default: {model}", f"  provider: {provider}", "  base_url: ''"]
        if block is None:
            text = "model:\n" + "\n".join(lines) + "\n" + text
        else:
            start, end = block
            body = text[start:end]
            for line in lines:
                key = line.split(":", 1)[0].strip()
                if re.search(rf"^  {key}:", body, re.MULTILINE):
                    body = re.sub(rf"^  {key}:.*$", line, body, count=1, flags=re.MULTILINE)
                else:
                    body = body.rstrip("\n") + "\n" + line + "\n"
            text = text[:start] + body + text[end:]
    return text


def _llm_connected(texts, provider):
    import connect
    block = connect._top_block(texts[0], "providers")
    return bool(block) and re.search(rf"^  {re.escape(provider)}:", texts[0][block[0]:block[1]],
                                     re.MULTILINE) is not None


def _llm_active(texts, provider):
    import connect
    block = connect._top_block(texts[0], "model")
    return bool(block) and re.search(rf"^  provider:\s*{re.escape(provider)}\s*$", texts[0][block[0]:block[1]],
                                     re.MULTILINE) is not None


def _llm_transform(texts, provider, label, url, models, context, model, use_now, remove):
    return [llm_text(texts[0], provider, label, url, models, context, model, use_now, remove)]


# --- skills (skills_ctl) --------------------------------------------------------------------------

def _skill_dirs_in(text: str) -> list[str]:
    import connect
    block = connect._top_block(text, "skills")
    if block is None:
        return []
    body = text[block[0]:block[1]]
    found = re.search(r"^  external_dirs:[ \t]*\n((?:    - .*\n?)*)", body, re.MULTILINE)
    if found is None:
        inline = re.search(r"^  external_dirs:[ \t]*\[(.*)\]", body, re.MULTILINE)
        return [d.strip().strip("'\"") for d in inline.group(1).split(",") if d.strip()] if inline else []
    return [line[6:].strip().strip("'\"") for line in found.group(1).splitlines() if line.strip()]


def _skills_listed():
    import connect
    import skills_ctl
    return _skill_dirs_in(connect._read(skills_ctl.HOME / ".hermes/config.yaml"))


def _skills_text(text: str, folder: str, remove: bool) -> str:
    """skills.external_dirs in config.yaml, as text: one `- path` line."""
    import connect
    import skills_ctl
    item = f"    - {folder}\n"
    block = connect._top_block(text, "skills")
    if block is None:
        if remove:
            return text
        return text + ("" if text.endswith("\n") or not text else "\n") + "skills:\n  external_dirs:\n" + item
    start, end = block
    body = text[start:end]
    listed = re.search(r"^  external_dirs:[ \t]*\n((?:    - .*\n?)*)", body, re.MULTILINE)
    if listed is None:
        if remove:
            return text
        if re.search(r"^  external_dirs:", body, re.MULTILINE):
            raise ValueError("skills.external_dirs is written in a form this cannot edit")
        body = body.rstrip("\n") + "\n  external_dirs:\n" + item
        return text[:start] + body + ("\n" if text[end:] and not body.endswith("\n") else "") + text[end:]
    lines = [line for line in listed.group(1).splitlines(keepends=True) if line.strip()]
    keep = [line for line in lines if line[6:].strip().strip("'\"") not in (folder, skills_ctl.tilde(folder))]
    if not remove:
        keep.append(item)
    if keep:
        new_list = "  external_dirs:\n" + "".join(l if l.endswith("\n") else l + "\n" for l in keep)
    else:
        new_list = ""
    body = body[:listed.start()] + new_list + body[listed.end():]
    return text[:start] + body + text[end:]


# --- security settings (agent_security) ----------------------------------------------------------

def _security(report):
    """Its security settings (agent_security.report_for)."""
    from agent_security import Choice, HOME, Setting, _read, _yaml_value
    path = HOME / ".hermes/config.yaml"
    text = _read(path) or ""
    report.files.append(path)
    report.settings += [
        Setting("approvals.mode", _("Dangerous commands"), _(
            "What Hermes does before a command it considers dangerous (deleting, overwriting, system changes)."),
            [Choice("manual", _("Always ask"), _("Asks you every time."), 0),
             Choice("smart", _("Automatic review"), _(
                 "A guardian model approves what it judges safe and asks about the rest."), 1),
             Choice("off", _("Never ask"), _("Runs dangerous commands without asking (like --yolo)."), 3)],
            _yaml_value(text, "approvals.mode"), "smart", path, "yaml", ("approval", "classifier", "bypass"),
            recommended="manual"),
        Setting("security.redact_secrets", _("Hide secrets"), _(
            "Masks passwords, tokens and keys in tool output and logs."),
            [Choice(True, _("On"), _("Secrets are masked."), 0),
             Choice(False, _("Off"), _("Secrets can reach the model and the logs."), 2)],
            _yaml_value(text, "security.redact_secrets"), True, path, "yaml", ("redaction",), recommended=True),
        Setting("terminal.backend", _("Where commands run"), _(
            "local runs commands on this PC as you; docker, ssh or a cloud backend runs them elsewhere. Change it "
            "with “hermes config” (it needs that backend set up)."),
            [Choice("local", _("This PC"), _("Commands run here with your rights."), 1),
             Choice("docker", _("Docker container"), _("Commands run in a container."), 0)],
            _yaml_value(text, "terminal.backend"), "local", path, "yaml", ("sandbox",), editable=False),
    ]


HARNESS = Harness("hermes", "Hermes", sessions=Sessions(collect, _note), label="Hermes", icon="\ue90a",
                  icon_font="omarchy", spec="pipx:hermes-agent[extras=all]", package="hermes-desktop",
                  own_install_counts=False, security=_security, usage=ref("agent_status", "hermes"),
                  access=_access,
                  local_llm=LocalLlm(("~/.hermes/config.yaml",), _llm_connected, _llm_active, _llm_transform),
                  skills=Skills(("~/.hermes/skills",), "~/.hermes/skills", settings_list=True,
                                listed=_skills_listed, settings_file="~/.hermes/config.yaml",
                                write=_skills_text),
                  mcp=Mcp("~/.hermes/config.yaml", format="yaml", key="mcp_servers"),
                  short_model_flag=True,
                  install=(Way("command", argv=("omarchy-install-hermes-cli", "--now"), sudo=True),
                           Way("package", packages=(("pacman", "hermes-desktop"),)),
                           Way("manual", url="https://github.com/NousResearch/hermes-agent")),
                  data=("~/.hermes",),
                  homepage="https://github.com/NousResearch/hermes-agent")
