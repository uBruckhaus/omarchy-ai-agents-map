"""What an agent's or a local LLM's details page sums up: usage over time and
whether it is fully set up, with the next step when it is not.

Reads numbers and times only: Omarchy's usage records (Claude Code, Codex),
the token and time columns of Hermes's and OpenCode's session databases
(opened read-only), and the names and times of Pi's session files. Never the
text of a conversation. Credential files are never opened: whether one exists
(a stat) and whether an API-key variable is set in this process's environment
(its name only) is all that is checked.
"""

from __future__ import annotations

import datetime as dt
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import desktop
import harnesses
import plugin_costs
import runtime_ctl
from i18n import _

HOME = Path.home()
DAYS = 7
MAX_ROWS = 20000            # sessions read from a database
MAX_FILES = 5000            # session files looked at
DB_TIMEOUT = 2.0            # seconds to wait for a database another program is writing


@dataclass
class Usage:
    days: list = field(default_factory=list)       # [(date, tokens)] for the last DAYS days, oldest first
    by_model: dict = field(default_factory=dict)   # model -> [input, output, cache]
    sessions_week: int | None = None
    sessions_total: int | None = None
    sessions_today: int | None = None
    prompts_today: int | None = None
    last: dt.datetime | None = None
    cost: float | None = None                      # USD the agent itself estimated, when it does
    cost_today: float | None = None                # of that, the sessions active today
    source: str = ""                                # where the numbers come from, for the page
    has_tokens: bool = True                         # False: only sessions are known (Pi)
    api_value: bool = False                         # cost is the tokens at API prices (not what a plan costs)

    @property
    def week_tokens(self) -> int:
        return sum(t for _d, t in self.days)

    @property
    def today_tokens(self) -> int:
        return self.days[-1][1] if self.days else 0


@dataclass
class Check:
    state: str              # "ok", "warn" (works, but worth a look), "missing" (needs doing), "info"
    title: str
    detail: str = ""
    step: str = ""          # what to do next, when it is not ok
    command: str = ""       # a command line for that step, shown with a copy button


# --- problems the last Start ran into (kept until a start works) ------------------------------

ISSUES = HOME / ".local/state/ai-agents-map/start-issues.json"
# kind -> (what happened, what to do, the command that fixes it)
ISSUE_KINDS = ("vte4", "terminal")


def issue_text(kind: str) -> tuple[str, str, str]:
    """(what happened, what to do, the command that fixes it: the desktop's package manager)."""
    return {
        "vte4": (_("The terminal inside the map needs the vte4 package, so it started in a separate terminal."),
                 _("Install it (it needs your password); the next Start then runs inside the map:"),
                 desktop.install_command("vte4")),
        "terminal": (_("No terminal was found to start it in."),
                     desktop.text("install_terminal"), desktop.install_command("xdg-terminal-exec")),
    }[kind]


def load_issues(path: Path | None = None) -> dict:
    import json
    try:
        with open(path or ISSUES, "rb") as handle:
            data = json.loads(handle.read(64 * 1024))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k)[:64]: v for k, v in data.items()
            if isinstance(v, dict) and v.get("kind") in ISSUE_KINDS}


def _save_issues(issues: dict, path: Path | None = None):
    import json
    path = path or ISSUES
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.new")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(issues, handle, indent=2)
    os.replace(tmp, path)


def remember_issue(command: str, kind: str, path: Path | None = None):
    issues = load_issues(path)
    issues[command] = {"kind": kind, "when": dt.datetime.now().isoformat(timespec="seconds")}
    _save_issues(issues, path)


def forget_issue(command: str, path: Path | None = None) -> bool:
    issues = load_issues(path)
    if command not in issues:
        return False
    issues.pop(command)
    _save_issues(issues, path)
    return True


# --- usage -------------------------------------------------------------------------------------

def _day_list(today: dt.date) -> list:
    return [today - dt.timedelta(days=DAYS - 1 - i) for i in range(DAYS)]


def _int(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def from_record(record: dict, today: dt.date | None = None) -> Usage | None:
    """The desktop's usage record (on Omarchy: omarchy-agent-usage-<agent>)."""
    if not record or not record.get("hasLocalStats"):
        return None
    today = today or dt.date.today()
    per_day = {}
    for item in record.get("recentDays") or []:
        if isinstance(item, dict):
            try:
                per_day[dt.date.fromisoformat(str(item.get("date")))] = _int(item.get("messageCount"))
            except ValueError:
                continue
    usage = Usage(days=[(d, per_day.get(d, 0)) for d in _day_list(today)], source="Omarchy")
    for model, numbers in (record.get("modelUsage") or {}).items():
        if isinstance(numbers, dict):
            usage.by_model[str(model)[:80]] = [
                _int(numbers.get("inputTokens")), _int(numbers.get("outputTokens")),
                _int(numbers.get("cacheReadInputTokens")) + _int(numbers.get("cacheCreationInputTokens"))]
    usage.sessions_total = _int(record.get("totalSessions")) if "totalSessions" in record else None
    usage.sessions_today = _int(record.get("todaySessions")) if "todaySessions" in record else None
    usage.prompts_today = _int(record.get("todayPrompts")) if "todayPrompts" in record else None
    return usage


def _connect(path: Path):
    """Read-only, never creating anything; None when it cannot be opened."""
    if not path.is_file():
        return None
    try:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=DB_TIMEOUT)
    except sqlite3.Error:
        return None


def _from_rows(rows, today: dt.date, source: str) -> Usage:
    """rows: (started epoch seconds, model, input, output, cache, cost)."""
    usage = Usage(days=[], source=source)
    per_day, week_start = {}, today - dt.timedelta(days=DAYS - 1)
    total = week = 0
    cost = 0.0
    for started, model, inp, out, cache, spent in rows:
        try:
            when = dt.datetime.fromtimestamp(float(started))
        except (TypeError, ValueError, OverflowError, OSError):
            continue
        total += 1
        inp, out, cache = _int(inp), _int(out), _int(cache)
        if when.date() >= week_start:
            week += 1
            per_day[when.date()] = per_day.get(when.date(), 0) + inp + out + cache
        model = _model_name(model)
        if model:
            numbers = usage.by_model.setdefault(model, [0, 0, 0])
            numbers[0] += inp
            numbers[1] += out
            numbers[2] += cache
        try:
            cost += float(spent or 0)
        except (TypeError, ValueError):
            pass
        if usage.last is None or when > usage.last:
            usage.last = when
    usage.days = [(d, per_day.get(d, 0)) for d in _day_list(today)]
    usage.sessions_total, usage.sessions_week = total, week
    usage.sessions_today = sum(1 for started, *_rest in rows if _is_day(started, today))
    usage.cost = cost or None
    today_cost = 0.0
    for started, *_rest, spent in rows:
        try:
            today_cost += float(spent or 0) if _is_day(started, today) else 0.0
        except (TypeError, ValueError):
            pass
    usage.cost_today = today_cost if usage.cost is not None else None
    return usage


def _model_name(value) -> str:
    """OpenCode keeps {"id": …, "providerID": …}; the others a plain name."""
    text = str(value or "").strip()
    if text.startswith("{"):
        try:
            import json
            data = json.loads(text[:2000])
            text = str(data.get("id", "")) if isinstance(data, dict) else ""
        except ValueError:
            return ""
    return text[:80]


def _is_day(started, day: dt.date) -> bool:
    try:
        return dt.datetime.fromtimestamp(float(started)).date() == day
    except (TypeError, ValueError, OverflowError, OSError):
        return False


def hermes(today: dt.date | None = None, root: Path = HOME) -> Usage | None:
    db = _connect(root / ".hermes/state.db")
    if db is None:
        return None
    try:
        rows = db.execute(
            "SELECT started_at, model, input_tokens, output_tokens,"
            " COALESCE(cache_read_tokens, 0) + COALESCE(cache_write_tokens, 0), estimated_cost_usd"
            " FROM sessions ORDER BY started_at DESC LIMIT ?", (MAX_ROWS,)).fetchall()
    except sqlite3.Error:
        return None
    finally:
        db.close()
    return _from_rows(rows, today or dt.date.today(), "Hermes")


def from_sessions(sessions, source: str, today: dt.date | None = None) -> Usage | None:
    """Usage derived from an agent's sessions (harnesses.Sessions: numbers only), for every agent
    that has them and no reader of its own, when the desktop keeps no usage record. A session counts
    on the day of its last reply; tokens as on the Projects card: input, output and cache writes
    (cache reads are not new tokens); its cost is the agent's own API value."""
    sessions = list(sessions or [])
    if not sessions:
        return None
    rows = []
    for session in sessions:
        when = session.last or session.first
        for number, (model, tokens) in enumerate((session.tokens or {"": [0, 0, 0, 0]}).items()):
            t = (list(tokens) + [0, 0, 0, 0])[:4]
            rows.append((when, model, t[0], t[1], t[3], session.cost if number == 0 else 0))
    today = today or dt.date.today()
    usage = _from_rows(rows, today, source)
    usage.by_model = {m: n for m, n in usage.by_model.items() if any(n)}      # no empty "unknown" row
    usage.api_value = True
    # Sessions, not (session, model) rows.
    week_start = today - dt.timedelta(days=DAYS - 1)
    stamps = [s.last or s.first for s in sessions]
    usage.sessions_total = len(stamps)
    usage.sessions_week = sum(1 for x in stamps if dt.datetime.fromtimestamp(x).date() >= week_start)
    usage.sessions_today = sum(1 for x in stamps if _is_day(x, today))
    return usage


def model_key(name) -> str:
    """A model's name as agents and local LLMs write it, made comparable: no provider prefix
    ("llama-local/…"), no tag (":latest"), no file ending, lower case."""
    text = str(name or "").strip().rsplit("/", 1)[-1].split(":", 1)[0].lower()
    for ending in (".gguf", ".kcpps"):
        text = text[:-len(ending)] if text.endswith(ending) else text
    return text


def runtime_usage(models, agent_sessions: dict, connect_key: str = "", today: dt.date | None = None) -> Usage | None:
    """Derived for a local LLM: the tokens agents sent through it, from their sessions (numbers
    only), where a session's model is one of the LLM's models; a model written with a provider
    prefix ("llama-local/…") counts only for the LLM with that connect key. agent_sessions: {agent
    name: [Session]}. by_model is per "agent · model"; sessions count once per agent session."""
    wanted = {model_key(m) for m in models or []}
    wanted.discard("")
    if not wanted:
        return None
    rows, used = [], []
    for agent, sessions in agent_sessions.items():
        for session in sessions or []:
            hit = False
            when = session.last or session.first
            for model, tokens in (session.tokens or {}).items():
                prefix = str(model).rsplit("/", 1)[0] if "/" in str(model) else ""
                if prefix and connect_key and prefix != connect_key:
                    continue
                if model_key(model) in wanted:
                    t = (list(tokens) + [0, 0, 0, 0])[:4]
                    rows.append((when, f"{agent} · {model}", t[0], t[1], t[3], 0))
                    hit = True
            if hit:
                used.append(when)
    if not rows:
        return None
    today = today or dt.date.today()
    usage = _from_rows(rows, today, "agents")
    week_start = today - dt.timedelta(days=DAYS - 1)
    usage.sessions_total = len(used)
    usage.sessions_week = sum(1 for x in used if dt.datetime.fromtimestamp(x).date() >= week_start)
    usage.sessions_today = sum(1 for x in used if _is_day(x, today))
    usage.cost = None
    return usage


def claude(today: dt.date | None = None, sessions=None) -> Usage | None:
    """Claude Code's numbers from its session files (kept: the 0.7.3 agent add-ons name it; the app
    itself now derives usage from any agent's sessions, see usage_for)."""
    if sessions is None:
        import claude_sessions
        sessions = claude_sessions.collect()
    return from_sessions(sessions, "Claude Code", today)


def opencode(today: dt.date | None = None, root: Path = HOME) -> Usage | None:
    db = _connect(root / ".local/share/opencode/opencode.db")
    if db is None:
        return None
    try:
        rows = db.execute(
            "SELECT time_created / 1000.0, model, tokens_input, tokens_output,"
            " COALESCE(tokens_cache_read, 0) + COALESCE(tokens_cache_write, 0), cost"
            " FROM session_v2 WHERE parent_id IS NULL ORDER BY time_created DESC LIMIT ?", (MAX_ROWS,)).fetchall()
    except sqlite3.Error:
        return None
    finally:
        db.close()
    return _from_rows(rows, today or dt.date.today(), "OpenCode")


def pi(today: dt.date | None = None, root: Path = HOME) -> Usage | None:
    """Pi keeps one file per session (named by its start time): counted, never opened."""
    folder = root / ".pi/agent/sessions"
    if not folder.is_dir():
        return None
    rows = []
    try:
        for project in sorted(folder.iterdir())[:500]:
            if not project.is_dir() or project.is_symlink():
                continue
            for item in sorted(project.glob("*.jsonl"))[-MAX_FILES:]:
                try:
                    rows.append((item.stat().st_mtime, item.name))
                except OSError:
                    continue
                if len(rows) >= MAX_FILES:
                    break
    except OSError:
        return None
    today = today or dt.date.today()
    started = []
    for mtime, name in rows:
        try:      # 2026-09-27T22-16-27-758Z_<id>.jsonl
            stamp = dt.datetime.strptime(name[:19], "%Y-%m-%dT%H-%M-%S").replace(tzinfo=dt.timezone.utc)
            started.append((stamp.astimezone().timestamp(), mtime))
        except ValueError:
            started.append((mtime, mtime))
    usage = _from_rows([(s, None, 0, 0, 0, 0) for s, _m in started], today, "Pi")
    usage.has_tokens = False
    if started:
        usage.last = dt.datetime.fromtimestamp(max(m for _s, m in started))
    return usage


def _rebuild():
    """Agents whose own session store can be read, and agents that tell their sign-in state
    themselves (argv, text meaning "no", text meaning "yes"); run again when add-ons change agents."""
    global READERS, STATUS_COMMANDS
    READERS = {h.command: h.usage for h in harnesses.all_harnesses() if h.usage is not None}
    STATUS_COMMANDS = {h.command: h.login_status for h in harnesses.all_harnesses() if h.login_status}


_rebuild()
harnesses.on_change(_rebuild)

def usage_for(command: str, record: dict, sessions=None) -> Usage | None:
    """The best numbers there are for an agent: the desktop's record, else its adapter's own reader,
    else derived from its sessions (sessions: {id: Session} collected already, or None to collect)."""
    found = from_record(record)
    if found is not None:
        return found
    reader = READERS.get(command)
    try:
        found = reader() if reader else None
    except Exception:          # a store in a format we do not know: no numbers, no crash
        found = None
    if found is not None:
        return found
    harness = harnesses.get(command)
    if harness is None or harness.sessions is None:
        return None
    try:
        if sessions is None:
            sessions = harness.sessions.collect()
        return from_sessions(sessions.values() if isinstance(sessions, dict) else sessions,
                             harness.full_name or harness.name)
    except Exception:
        return None


# --- access: subscription, sign-in or API key ---------------------------------------------------

@dataclass
class Access:
    how: str                 # what it needs
    login: str = ""          # the command that signs in
    files: tuple = ()        # credential files (relative to ~): only whether one exists is checked
    env: tuple = ()          # API-key variables: only whether one is set is checked
    local_ok: bool = False   # works with a local LLM, without any account
    per_token: bool = False  # every sign-in is an API key: paid per token, no plan with limits
    logins: tuple = ()       # signin.Login: the agent's own sign-ins the app can start in a terminal
    key: object = None       # signin.KeyStep: how an API key gets where the agent reads it


def access_table() -> dict:
    """Per agent: what it needs to work (from the agent adapters; built on each call, so it follows
    the interface language)."""
    return {h.command: h.access() for h in harnesses.all_harnesses() if h.access is not None}


def login_state(command: str) -> bool | None:
    """The agent's own answer (a bounded run of its status command), None when unknown."""
    import ai_core
    import shutil
    entry = STATUS_COMMANDS.get(command)
    if entry is None or not shutil.which(entry[0][0]):
        return None
    argv, negative, positive = entry
    try:
        out, _ok = ai_core.read_bounded_output(list(argv), max_bytes=16384, timeout=8, merge_stderr=True)
    except Exception:
        return None
    text = out.decode("utf-8", "replace").lower()
    return False if negative in text else True if positive in text else None


def pays_per_token(command: str, details, environ=None) -> bool:
    """Whether the agent is paid per token (an API key or API billing) rather than with a plan whose
    limits the battery shows: no limits known, and one of its API-key variables is set (only whether
    it is), or it signs in with API keys only (access.per_token) and is signed in (its credential file
    is there: never opened), or the desktop's usage record for it is there and names no plan. Without
    any of these it is not known: no."""
    if details is None or details.limits:
        return False
    harness = harnesses.get(command)
    access = harness.access() if harness is not None and harness.access is not None else None
    environ = os.environ if environ is None else environ
    if access is not None and any(environ.get(name) for name in access.env):
        return True
    if access is not None and access.per_token and credentials_present(access, HOME, environ):
        return True              # signed in, and it has only API keys (DeepSeek): only whether a file is there
    return bool(details.has_usage) and not details.plan


def cost_today(command: str, today: dt.date | None = None) -> float | None:
    """What the agent's sessions active today cost at API prices (costs()); None when unknown."""
    found = costs(command, today)
    return found.today if found is not None else None


@dataclass
class Costs:
    """What an agent's tokens cost at API prices (plugin_costs): sessions active today, in the last 7 and
    30 days, and per model over the 30 days its tokens and their cost by kind (input, output, cache read,
    cache write). Models without a price (local LLMs) are in `unpriced`."""
    today: float = 0.0
    week: float = 0.0
    month: float = 0.0
    by_model: dict = field(default_factory=dict)   # model -> (provider, [4 token counts], [4 costs in USD])
    unpriced: dict = field(default_factory=dict)   # model -> its tokens (all four kinds together)
    reported: float = 0.0    # of month: what the agent itself reports for sessions without token counts (Crush)


def costs(command: str, today: dt.date | None = None, sessions=None) -> Costs | None:
    """The agent's Costs from its own session files (numbers only; sessions: {id: Session} collected
    already, or None to collect); None when it has no session reader or none of its models has a price."""
    harness = harnesses.get(command)
    if harness is None or harness.sessions is None:
        return None
    today = today or dt.date.today()
    try:
        found = harness.sessions.collect() if sessions is None else sessions
        sessions = list(found.values() if isinstance(found, dict) else found)
    except Exception:          # a store in a format we do not know: no costs, no crash
        return None
    result, any_price = Costs(), False
    for session in sessions:
        when = session.last or session.first
        try:
            day = dt.datetime.fromtimestamp(float(when)).date()
        except (TypeError, ValueError, OverflowError, OSError):
            continue
        age = (today - day).days
        if not 0 <= age < 30:
            continue
        split = plugin_costs.parts(session.tokens)
        for model, (provider, money) in split.items():
            counts = [_int(n) for n in (list(session.tokens[model]) + [0, 0, 0, 0])[:4]]
            _provider, kept_counts, kept_money = result.by_model.setdefault(model, (provider, [0] * 4, [0.0] * 4))
            for i in range(4):
                kept_counts[i] += counts[i]
                kept_money[i] += money[i]
            spent = sum(money)
            result.month += spent
            result.week += spent if age < 7 else 0.0
            result.today += spent if age == 0 else 0.0
            any_price = True
        for model, counts in (session.tokens or {}).items():
            if model not in split and model:
                result.unpriced[model] = result.unpriced.get(model, 0) + sum(_int(n) for n in counts)
        own = getattr(session, "cost", 0) or 0
        if not session.tokens and isinstance(own, (int, float)) and own > 0:
            # No token counts, only the agent's own cost for the session (Crush): that counts.
            result.reported += own
            result.month += own
            result.week += own if age < 7 else 0.0
            result.today += own if age == 0 else 0.0
            any_price = True
    return result if any_price else None


def credentials_present(access: Access, root: Path = HOME, environ=None) -> bool:
    environ = os.environ if environ is None else environ
    if any(environ.get(name) for name in access.env):
        return True
    for name in access.files:
        try:
            if (root / name).is_file():       # a stat, never a read
                return True
        except OSError:
            continue
    return False


# --- setup checks ------------------------------------------------------------------------------

def agent_checks(agent, info, record: dict, inventory, hints: dict, logged_in: bool | None = None,
                 root: Path = HOME, environ=None, issue: dict | None = None) -> list:
    """Is the agent ready to use? Each check with what to do next when it is not."""
    import graph
    checks = []
    if not agent.installed:
        checks.append(Check("missing", _("Not installed"),
                            step=desktop.text("install_agent")))
        return checks
    checks.append(Check("ok", _("Installed"), " · ".join(x for x in (agent.version,) if x)))
    if issue and issue.get("kind") in ISSUE_KINDS:
        what, step, command = issue_text(issue["kind"])
        checks.append(Check("warn", _("Start ran into a problem"), what, step, command))

    model, provider = graph.split_model(info.model)
    local_names = {p.name for p in info.providers if p.local}
    uses_local = bool(provider) and provider in local_names
    access = access_table().get(agent.command)
    if uses_local:
        checks.append(Check("ok", _("No account needed"), _("It uses a local LLM on this computer.")))
    elif record and record.get("ready") and record.get("tierLabel"):
        checks.append(Check("ok", _("Signed in"), _("Plan: {0}").format(str(record["tierLabel"])[:40])))
    elif record and record.get("authHelpText") and not record.get("ready"):
        checks.append(Check("missing", _("Not signed in"), str(record.get("usageStatusText") or "")[:200],
                            str(record["authHelpText"])[:300]))
    elif access is not None and access.local_ok and not access.files and not access.env and not access.login:
        checks.append(Check("ok", _("No account needed"), access.how))
    elif access is not None:
        signed_in = credentials_present(access, root, environ) if logged_in is None else logged_in
        if signed_in:
            checks.append(Check("ok", _("Signed in"), _("Its sign-in or API key is set up.")))
        elif access.local_ok and local_names:
            checks.append(Check("info", _("No account set up"),
                                _("It can use the local LLMs it is connected to."),
                                access.how + (" · " + _("Sign in: {0}").format(access.login) if access.login else "")))
        else:
            checks.append(Check("missing", _("Not signed in"), access.how,
                                _("Sign in: {0}").format(access.login) if access.login else access.how))
    elif not model and not info.providers and agent.command not in graph.CLOUD:
        # No service of its own (OpenClaw routes to the providers in its settings), and none set up:
        # it cannot answer anything yet.
        checks.append(Check("missing", _("No model set up"),
                            _("It has no model service of its own, and none is set up in its settings."),
                            _("Draw a line on the map from it to a local LLM, or set up a provider in the agent "
                              "itself (OpenClaw: openclaw onboard).")))
    else:
        checks.append(Check("info", _("Sign-in unknown"),
                            _("AI Agents Map does not know how this agent signs in.")))

    if model:
        checks.append(Check("ok", _("Model"), model + (f" · {provider}" if provider else "")))
    elif any(c.title == _("No model set up") for c in checks):
        pass                                  # said above, with what to do
    elif local_names:
        checks.append(Check("warn", _("No model chosen"), "",
                            _("Select the agent on the map and click a model of a local LLM it is connected to.")))
    else:
        checks.append(Check("info", _("Model"), _("the agent's own default")))

    by_name = {rt.name: rt for rt in inventory.runtimes}
    for p in info.providers:
        if not p.local:
            continue
        rt = by_name.get(p.runtime)
        # Only the local LLM the agent uses now can stand in its way; one it is merely set up for
        # (a line it could be switched to on the map) gets a note instead.
        problem = "missing" if p.active else "info"
        unused = "" if p.active else _("Not in use: it only matters when the agent is switched to it.")
        if rt is None or not rt.installed:
            checks.append(Check(problem, _("{0} is not installed").format(p.runtime or p.name), unused,
                                _("Install it: its card on Setup → Install.")))
        elif not rt.models:
            import link_check
            step = link_check.add_model_step(rt)
            checks.append(Check(problem, _("{0} has no models").format(rt.name), unused, step[:1].upper() + step[1:]))
        elif p.active and getattr(rt, "server_off", False):
            checks.append(Check("missing", _("{0}'s server is off").format(rt.name),
                                _("The app is open, but the agent reaches it only through its server."),
                                _("Start server in its menu on the map (lms server start).")))
        elif p.active and not rt.running:
            checks.append(Check("ok", rt.name, _("starts with the agent") if runtime_ctl.can_control(rt)
                                else _("installed, not running")))
        else:
            checks.append(Check("ok", rt.name, _("running") if rt.running else _("set up")))

    for link_key, entry in sorted(hints.items()):
        if not link_key.startswith(f"agent:{agent.command}|"):
            continue
        first = entry["hints"][0]["text"] if entry.get("hints") else ""
        checks.append(Check("missing" if entry.get("kind") and any(h["level"] == "error" for h in entry["hints"])
                            else "warn", _("A connection does not work: {0}").format(link_key.partition("|")[2]
                                                                                    .partition(":")[2]),
                            first, _("Click its dotted line on the map for all hints and Test again.")))

    servers = inventory.agent_mcp.get(agent.command, [])
    if servers:
        checks.append(Check("ok", _("MCP servers"), ", ".join(servers)))
    else:
        checks.append(Check("info", _("No MCP servers yet"), "",
                            _("Add one: Add MCP server on Setup, then draw a line from the agent to it on the map.")))
    sets = inventory.agent_skills.get(agent.command, [])
    if sets:
        checks.append(Check("ok", _("Skills"), ", ".join(sets)))
    else:
        checks.append(Check("info", _("No skills yet"), "", _("Add some: Add skills on Setup.")))
    return checks


def runtime_checks(rt, hints: dict, agent_names: dict, login: bool | None) -> list:
    checks = []
    if not rt.installed:
        return [Check("missing", _("Not installed"), "",
                      _("Install it: its card on Setup → Install."))]
    checks.append(Check("ok", _("Installed"), rt.address))
    if rt.running:
        checks.append(Check("ok", _("Running"), rt.address))
    elif runtime_ctl.can_control(rt):
        checks.append(Check("info", _("Not running"), _("starts with an agent that uses it")
                            if rt.active_agents else _("nothing uses it now"),
                            "" if rt.active_agents else _("Right-click → Start to run it now.")))
    else:
        checks.append(Check("warn", _("Not running"), _("no user service starts it"),
                            _("Start it yourself, or reinstall it from the map so a service starts it.")))
    if login is not None:
        checks.append(Check("ok" if login else "info", _("Start at login"), _("on") if login else _("off")))
    if rt.models:
        checks.append(Check("ok", _("Models"), str(len(rt.models)) + (f" · {rt.models_dir}" if rt.models_dir else "")))
    else:
        checks.append(Check("missing", _("No models"), "", _("Download one: Add model on Setup, or Models… in its menu on the map.")))
    users = [agent_names.get(a, a) for a in rt.agents]
    if users:
        now = [agent_names.get(a, a) for a in rt.active_agents]
        checks.append(Check("ok", _("Connected agents"), ", ".join(users)
                            + (" · " + _("in use by {0}").format(", ".join(now)) if now else "")))
    else:
        checks.append(Check("missing", _("No agent connected"), "",
                            _("Draw a line from an agent to this box on the map.")))
    for link_key, entry in sorted(hints.items()):
        if link_key.endswith(f"|runtime:{rt.key}"):
            first = entry["hints"][0]["text"] if entry.get("hints") else ""
            source = link_key.partition("|")[0].partition(":")[2]
            checks.append(Check("missing", _("The connection from {0} does not work").format(
                agent_names.get(source, source)), first,
                _("Click its dotted line on the map for all hints and Test again.")))
    return checks


def summary_line(checks: list) -> tuple[str, str]:
    """("ok" | "warn" | "missing", one sentence) for the top of the page."""
    missing = [c for c in checks if c.state == "missing"]
    warn = [c for c in checks if c.state == "warn"]
    if missing:
        return "missing", _("Not ready yet: {0}").format(missing[0].title) + (
            " " + _("(+{0} more to do)").format(len(missing) - 1) if len(missing) > 1 else "")
    if warn:
        return "warn", _("Works, one thing to look at: {0}").format(warn[0].title)
    return "ok", _("Set up and ready")
