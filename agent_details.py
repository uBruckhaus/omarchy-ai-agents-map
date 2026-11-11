"""What the expanded agent card shows: plan and limits, model, providers
(local LLMs among them), usage and when the agent was last used.

Sources, all read-only and size-capped:

- The desktop's usage records (desktop.usage_dir(); on Omarchy
  ~/.local/state/omarchy/agents/usage/<agent>.json, written by `omarchy agent
  usage update`): plan, rate limits, today's usage. Only the desktop talks to
  the providers; refresh_usage() asks it to (desktop.refresh_usage). Where
  no collector of an agent is, one whose adapter names a protocol for its
  plan limits ([plan_limits]) is asked itself (native_usage): its own
  program answers for its own sign-in, the answer kept in
  ~/.local/state/ai-agents-map/usage/<agent>.json.
- Each agent's own settings files for the model and its providers. Never
  their credential files: secrets are not read, and nothing here is shown
  that could contain one (only names, model ids and local addresses).
- File times for "last used".
"""

import datetime as dt
import json
import os
import re
import stat
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast
from urllib.parse import urlparse

import ai_core
import desktop
from i18n import _

HOME = Path.home()
USAGE_DIR = desktop.usage_dir() or HOME / ".local/state/ai-agents-map/no-usage"   # the desktop's usage records
MAX_FILE_BYTES = 512 * 1024
MAX_SCAN = 3000              # directory entries looked at for "last used"
DATE_DEPTH = 2               # levels below those followed into the last-named subfolder (YYYY/MM/DD trees)
USAGE_MAX_AGE = 10 * 60      # seconds before an expanded card asks Omarchy for fresh usage
USAGE_TIMEOUT = 90
# Limits measured longer ago than this no longer look current: three times USAGE_MAX_AGE (after which
# the details ask for fresh usage), so two refreshes in a row may fail or be skipped before a battery
# says so. One without a valid reset is left out then (nothing says when its numbers ran out).
LIMITS_MAX_AGE = 3 * USAGE_MAX_AGE

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}

# Plans the usage records name by their service's code (Codex's planType), as people know them.
# A label not listed here (Claude's "Max 5x") is shown as it is.
PLAN_NAMES = {"free": "Free", "go": "Go", "plus": "Plus", "pro": "Pro", "prolite": "Pro Lite",
              "team": "Team", "business": "Business", "enterprise": "Enterprise", "edu": "Edu"}

@dataclass
class Provider:
    name: str
    local: bool = False
    runtime: str = ""        # llama.cpp, Ollama, … for local ones
    models: int = 0
    active: bool = True      # the agent uses it now; False: set up, ready to switch to
    port: int | None = None  # a local one's configured port: joined to the runtime detected there


@dataclass
class Limit:
    label: str
    percent: float           # 0..1
    resets_at: dt.datetime | None = None
    measured_at: dt.datetime | None = None   # when its numbers were measured (limitsFetchedAt, else updatedAt)
    stale: bool = False      # the collector kept its numbers past a failed probe (limitsStale)


@dataclass
class Details:
    plan: str = ""
    model: str = ""
    providers: list = field(default_factory=list)
    limits: list = field(default_factory=list)
    today_prompts: int | None = None
    today_sessions: int | None = None
    today_tokens: int | None = None
    total_sessions: int | None = None
    active_days: int | None = None
    usage_note: str = ""     # Omarchy's status or sign-in hint
    usage_updated: dt.datetime | None = None
    has_usage: bool = False
    last_used: dt.datetime | None = None
    config_dir: str = ""


# --- reading ----------------------------------------------------------------

def _read(path: Path, limit: int = MAX_FILE_BYTES) -> str | None:
    if path.name in ('auth.json', '.credentials.json', 'credentials.json'):
        return None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)   # a FIFO in its place cannot hang it
    except OSError:
        return None
    try:
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                return None
            data = handle.read(limit + 1)
    except OSError:
        return None
    return None if len(data) > limit else data.decode("utf-8", "replace")


def _strip_jsonc(text: str) -> str:
    """Drop // and /* */ comments outside strings, and trailing commas."""
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
            out.append(c)
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
            continue
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        else:
            out.append(c)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def read_json(path: Path) -> dict:
    text = _read(path)
    if text is None:
        return {}
    try:
        data = json.loads(_strip_jsonc(text) if path.suffix == ".jsonc" or "//" in text else text)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def extra_usage(access) -> dict:
    """Paid usage beyond the plan (access.extra_*): on, off, or None when this PC cannot tell; only
    that one value of the file is used."""
    if access is None:
        return {"on": None, "url": ""}
    value = None
    if access.extra_file and access.extra_key:
        value = read_json(Path(access.extra_file.replace("~", str(HOME), 1)))
        for part in access.extra_key.split("."):
            value = value.get(part) if isinstance(value, dict) else None
    return {"on": value if isinstance(value, bool) else None, "url": access.extra_url}


def read_toml(path: Path) -> dict:
    text = _read(path)
    try:
        return tomllib.loads(text) if text else {}
    except tomllib.TOMLDecodeError:
        return {}


def _text(value, limit: int = 80) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _expand(path: str) -> Path:
    return Path(os.path.expanduser(path))


# --- providers --------------------------------------------------------------

def local_ports() -> dict:
    """{port: name} of the local LLMs by their usual ports (their engines'), the first one wins."""
    import llm_engine
    import providers
    found = {}
    for p in providers.local():
        for port in llm_engine.of(p.key).ports:
            found.setdefault(port, p.name)
    return found


def local_names() -> dict:
    """{connect key: name} of the local LLMs ("strata-local": "Strata"): a provider the app wrote
    names its local LLM, also when another one has that usual port (Strata and llama.cpp: 8080)."""
    import providers
    return {p.connect_key: p.name for p in providers.local() if p.connect_key}


def provider_for_url(name: str, url, models: int = 0, active: bool = True) -> Provider:
    """A provider entry; local when its address is this machine."""
    try:
        parsed = urlparse(url) if isinstance(url, str) else None
        host, port = (parsed.hostname, parsed.port) if parsed else (None, None)
    except ValueError:
        host, port = None, None
    local = host in LOCAL_HOSTS
    runtime = (local_names().get(name) or local_ports().get(port, "")) if local else ""
    return Provider(name=_text(name, 40), local=local, runtime=runtime, models=models, active=active,
                    port=port if local else None)


def _model_count(value) -> int:
    if isinstance(value, (list, dict)):
        return min(len(value), 9999)
    return 0


def activity(command: str) -> tuple:
    """Where the agent's last use shows (its adapter's activity files and folders)."""
    import harnesses
    harness = harnesses.get(command)
    return harness.activity if harness is not None else ()


# --- usage (Omarchy's records) -------------------------------------------------

def _time(value) -> dt.datetime | None:
    """An ISO time, always with a time zone (local when it has none)."""
    try:
        moment = dt.datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None
    return moment.astimezone() if moment is not None and moment.tzinfo is None else moment


def _usage_time(value) -> dt.datetime | None:
    """A usage record's time: an ISO text, or seconds or milliseconds since 1970 (collectors write both)."""
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        seconds = value / 1000 if value > 1e11 else value
        try:
            return dt.datetime.fromtimestamp(seconds, dt.timezone.utc) if seconds <= 8e12 else None
        except (OverflowError, OSError, ValueError):
            return None
    return _time(value) if isinstance(value, str) and value else None


def _count(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def usage_stamps(usage_dir: Path = USAGE_DIR) -> dict:
    """{agent: the time its usage record was last written} (only file times: cheap enough for the
    map's 2-second poll, which reads a record again when its time changes)."""
    stamps = {}
    try:
        entries = list(os.scandir(usage_dir))[:200]
    except OSError:
        return stamps
    for entry in entries:
        name = entry.name
        if name.endswith(".json") and re.fullmatch(r"[a-z0-9-]{1,64}", name[:-5]):
            try:
                stamps[name[:-5]] = entry.stat(follow_symlinks=False).st_mtime_ns
            except OSError:
                continue
    return stamps


def usage_record(command: str, usage_dir: Path = USAGE_DIR) -> dict:
    if not re.fullmatch(r"[a-z0-9-]{1,64}", command):
        return {}
    return read_json(usage_dir / f"{command}.json")


def plan_label(raw) -> str:
    """A usage record's plan, readable: "prolite" -> "Pro Lite"."""
    text = _text(raw, 40)
    return PLAN_NAMES.get(text.strip().lower(), text)


def record_written(path: Path) -> dt.datetime | None:
    """When a usage record was written (its mtime): when its limits were measured if it says nothing
    itself, so limits of unknown age still grow old (current_limits, limits_stale)."""
    try:
        return dt.datetime.fromtimestamp(int(path.stat().st_mtime), dt.timezone.utc)   # whole seconds
    except (OSError, OverflowError, ValueError):
        return None


def apply_usage(details: Details, record: dict, written: dt.datetime | None = None):
    if not record:
        return
    details.has_usage = bool(record.get("ready")) or bool(record.get("hasLocalStats"))
    details.plan = plan_label(record.get("tierLabel")) or details.plan
    details.today_prompts = _count(record.get("todayPrompts"))
    details.today_sessions = _count(record.get("todaySessions"))
    details.today_tokens = _count(record.get("todayTotalTokens"))
    details.total_sessions = _count(record.get("totalSessions"))
    details.active_days = _count(record.get("activeDays"))
    details.usage_updated = _time(record.get("updatedAt"))
    details.usage_note = _text(record.get("usageStatusText"), 120)
    limits = record.get("limits")
    measured = (_usage_time(record.get("limitsFetchedAt")) or _usage_time(record.get("updatedAt"))
                or written)      # no time of its own: the file's, never an age that is unknown for ever
    if isinstance(limits, list):
        for item in limits[:8]:
            if not isinstance(item, dict):
                continue
            percent = item.get("percent")
            if isinstance(percent, (int, float)) and not isinstance(percent, bool):
                details.limits.append(Limit(label=_text(item.get("title") or item.get("label"), 40),
                                            percent=max(0.0, min(1.0, float(percent))),
                                            resets_at=_time(item.get("resetsAt")), measured_at=measured,
                                            stale=record.get("limitsStale") is True))


def usage_is_stale(command: str, usage_dir: Path = USAGE_DIR, root: Path = HOME) -> bool:
    """True when Omarchy collects usage for this agent and its record is old (or missing), or older
    than the agent's sign-in (a new sign-in, a changed plan: the record still tells the old one)."""
    native = not has_usage_collector(command)
    if native:
        if plan_limits_rpc(command, root) is None:
            return False
        usage_dir = native_usage_dir()       # asked of the agent itself (native_usage)
    try:
        written = (usage_dir / f"{command}.json").stat().st_mtime
    except OSError:
        return True
    if native and usage_record(command, usage_dir).get("home") != usage_home(command, root):
        return True                          # another home's: not this one's limits
    if dt.datetime.now().timestamp() - written > USAGE_MAX_AGE:
        return True
    return signed_in_since(command, written, root)


def signed_in_since(command: str, when: float, root: Path = HOME) -> bool:
    """The agent's credential file changed after `when` (only its time is looked at, never its text;
    where its home variable moved it: access_paths)."""
    for path in access_paths(command, root):
        try:
            if path.stat().st_mtime > when:
                return True
        except OSError:
            continue
    return False


def has_usage_collector(command: str) -> bool:
    return desktop.has_usage_collector(command)


def native_usage_dir() -> Path:
    """Where AI Agents Map keeps the usage records it asked an agent's own program for."""
    state = os.environ.get("XDG_STATE_HOME", "")
    return (Path(state) if state.startswith("/") else HOME / ".local/state") / "ai-agents-map/usage"


def access_paths(command: str, root: Path | None = None, environ=None) -> list:
    """The agent's access files (relative to ~) where it keeps them: in the folder its
    plan_limits.home_env names when that is set (CODEX_HOME), each by its name."""
    import harnesses
    import agent_status
    environ = os.environ if environ is None else environ
    root = HOME if root is None else root
    harness = harnesses.get(command)
    access = agent_status.access_table().get(command)
    names = access.files if access is not None else ()
    var = harness.plan_limits[2] if harness is not None and len(harness.plan_limits) > 2 else ""
    moved = environ.get(var, "") if var else ""
    return [Path(moved) / Path(name).name if moved.startswith("/") else root / name for name in names]


def usage_home(command: str, root: Path | None = None, environ=None) -> str:
    """The agent's effective home (plan_limits.home_env, CODEX_HOME, or the home folder) as a digest:
    AI Agents Map's own usage record keeps the one it was asked for, so another home's limits never show."""
    import hashlib
    import harnesses
    environ = os.environ if environ is None else environ
    root = HOME if root is None else root
    harness = harnesses.get(command)
    var = harness.plan_limits[2] if harness is not None and len(harness.plan_limits) > 2 else ""
    moved = environ.get(var, "") if var else ""
    return hashlib.sha256(f"{root}\n{moved if moved.startswith('/') else ''}".encode()).hexdigest()[:16]


def program_answer(command: str, root: Path | None = None, environ=None) -> bool | None:
    """What the agent's own program last said for this home (AI Agents Map's own record): True (its limits
    came), False (it named no sign-in) or None (not asked yet, no answer, or Omarchy's collector asks)."""
    if has_usage_collector(command):
        return None
    record = usage_record(command, native_usage_dir())
    said = record.get("signedIn")
    return said if record.get("home") == usage_home(command, root, environ) and isinstance(said, bool) else None


def plan_limits_probe(command: str, root: Path | None = None, environ=None) -> bool:
    """Whether the agent's own program may be asked for its plan limits: one of its access files is
    there, or its store keeps the sign-in elsewhere (also after it said nobody is signed in: a new
    sign-in in a keyring is found so)."""
    environ = os.environ if environ is None else environ
    root = HOME if root is None else root
    return any(path.is_file() for path in access_paths(command, root, environ)) \
        or _store_elsewhere(command, root, environ)


def plan_limits_signed_in(command: str, root: Path | None = None, environ=None) -> bool:
    """Whether the agent is signed in as far as this PC can tell ([plan_limits]): one of its access
    files is there (only that, never what is in it; access_paths), or its store setting (plain
    configuration, plan_limits.store: Codex's cli_auth_credentials_store) keeps the sign-in outside
    the files, a keyring, and its own program did not say last time (program_answer) that nobody is
    signed in. Not asked yet or no answer: signed in, as before. The map's line, the battery and the
    record's validity go by this."""
    environ = os.environ if environ is None else environ
    root = HOME if root is None else root
    said = program_answer(command, root, environ)
    files = [path for path in access_paths(command, root, environ) if path.is_file()]
    if files:
        # A sign-in file the program said is no sign-in (expired, revoked) is none, until the file
        # changes after that answer (a new sign-in).
        if said is not False:
            return True
        record = native_usage_dir() / f"{command}.json"
        try:
            written = record.stat().st_mtime
        except OSError:
            return True
        return any(path.stat().st_mtime > written for path in files)
    return _store_elsewhere(command, root, environ) and said is not False


def _store_elsewhere(command: str, root: Path, environ) -> bool:
    import harnesses
    harness = harnesses.get(command)
    facts = harness.plan_limits if harness is not None else ()
    if len(facts) < 4 or not facts[3]:
        return False
    file, fmt, key, files = facts[3]
    var = facts[2]
    moved = environ.get(var, "") if var else ""
    path = Path(moved) / Path(file).name if moved.startswith("/") else root / file.removeprefix("~/")
    try:
        text = path.read_text(encoding="utf-8") if path.stat().st_size <= 512 * 1024 else ""
        value = (tomllib.loads(text) if fmt == "toml" else json.loads(text)) if text else {}
    except (OSError, ValueError, UnicodeDecodeError):
        value = {}
    for part in key.split("."):
        value = value.get(part) if isinstance(value, dict) else None
    return isinstance(value, str) and value != "" and value != (files or "file")


def plan_limits_rpc(command: str, root: Path = HOME, environ=None) -> tuple | None:
    """(rpc, argv, program) of an agent whose plan limits may be asked of its own program: its
    adapter names how ([plan_limits]), no collector of it is found, it is signed in
    (plan_limits_signed_in) and its installed program is found offline (agents.real_program: never a
    launcher that would install or update it). Else None."""
    import agents
    import harnesses
    harness = harnesses.get(command)
    if harness is None or not harness.plan_limits or has_usage_collector(command):
        return None
    if not plan_limits_probe(command, root, environ):
        return None
    specs = [s for s in (agents.wrapper_spec(command), harness.spec,
                         *(w.spec for w in harness.install if w.kind == "mise")) if s]
    program = agents.real_program(command, specs, os.environ if environ is None else environ)
    return (harness.plan_limits[0], harness.plan_limits[1], program) if program else None


def refresh_usage(command: str) -> bool:
    """Ask the desktop for this agent's usage (blocking; run it on a thread); without a collector,
    the agent's own program when its adapter says how (native_usage)."""
    if desktop.has_usage_collector(command):
        return desktop.refresh_usage(command)
    native = plan_limits_rpc(command)
    return native is not None and native_usage(command, *native)


def native_usage(command: str, rpc: str, argv, program: str, environ=None) -> bool:
    """Ask the agent's own program for its plan limits and keep the answer as its usage record."""
    environ = os.environ if environ is None else environ
    record = ask_plan_limits(rpc, list(argv), dict(environ), program=program)
    home = usage_home(command, HOME, environ)   # the home it was asked for: its record is that home's only
    folder = native_usage_dir()
    if record is None:
        # No answer: signed in or not is unknown now; an earlier "signed out" of this home goes.
        old = usage_record(command, folder)
        if old.get("home") != home or old.get("signedIn") is not False:
            return False
        record = {k: v for k, v in old.items() if k != "signedIn"}
        answered = False
    else:
        record["home"] = home
        answered = True
    try:
        folder.mkdir(parents=True, exist_ok=True)
        temporary = folder / f".{command}.json.tmp"
        temporary.write_text(json.dumps(record) + "\n", encoding="utf-8")
        temporary.replace(folder / f"{command}.json")
    except OSError:
        return False
    return answered


LIMIT_RPCS = ("codex-app-server",)


def ask_plan_limits(rpc: str, argv: list, environ: dict, answer: float = 8, total: float = 20,
                    now=None, program: str | None = None) -> dict | None:
    """The record Omarchy's collector would write (limits with label, percent and resetsAt; tierLabel),
    asked of the agent's own program over `rpc` (omarchy-agent-usage-codex's fetch_codex_rpc:
    initialize, initialized, account/rateLimits/read, account/read only when no plan is named); None
    when it could not be asked. No prompt goes to a model and nothing here reads a credential. Each
    answer within `answer` seconds, all within `total`; the program (its whole session) is killed
    afterwards. `program` is the agent's installed program (plan_limits_rpc: agents.real_program);
    without one nothing is asked, never the first `argv[0]` on PATH."""
    import select
    import signal
    import time
    if rpc not in LIMIT_RPCS or not argv or not program or os.path.basename(program) != argv[0]:
        return None
    binary = program
    try:
        proc = subprocess.Popen([binary, *argv[1:]], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, env=environ, cwd=environ.get("HOME") or None,
                                start_new_session=True)
    except OSError:
        return None
    end = time.monotonic() + total
    pending = [b""]
    read = [0]

    def send(message):
        assert proc.stdin is not None
        proc.stdin.write((json.dumps(message) + "\n").encode())
        proc.stdin.flush()

    def request(ident, method, wait):
        send({"id": ident, "method": method, "params": {"clientInfo": {"name": "ai-agents-map", "version": "1"}}
              if method == "initialize" else {}})
        deadline = min(end, time.monotonic() + wait)
        assert proc.stdout is not None
        while True:
            while b"\n" in pending[0]:
                line, pending[0] = pending[0].split(b"\n", 1)
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if isinstance(message, dict) and message.get("id") == ident:
                    return message
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(method)
            ready, _w, _x = select.select([proc.stdout], [], [], min(0.25, remaining))
            if not ready:
                continue
            chunk = os.read(proc.stdout.fileno(), 65536)
            read[0] += len(chunk)
            if not chunk or read[0] > 1024 * 1024:
                raise RuntimeError(method)
            pending[0] += chunk

    try:
        request(1, "initialize", answer)
        send({"method": "initialized", "params": {}})
        reply = request(2, "account/rateLimits/read", answer)
        at = (now or dt.datetime.now(dt.timezone.utc))
        record = {"schemaVersion": 1, "updatedAt": at.isoformat(), "limitsStale": False,
                  "limitsFetchedAt": int(at.timestamp() * 1000), "limits": [], "tierLabel": "",
                  "usageStatusText": ""}
        error = reply.get("error")
        if isinstance(error, dict):
            if "auth" not in str(error.get("message") or "").lower():
                return None
            record["usageStatusText"] = "Waiting for auth"
            record["signedIn"] = False          # its own answer: nobody signed in here
            return record
        result = reply.get("result") if isinstance(reply.get("result"), dict) else {}
        limits = result.get("rateLimits") if isinstance(result.get("rateLimits"), dict) else {}
        plan = limits.get("planType") if isinstance(limits.get("planType"), str) else ""
        if not plan:
            try:
                found = (request(3, "account/read", 2).get("result") or {}).get("account") or {}
                plan = str(found.get("planType") or found.get("type") or "") if isinstance(found, dict) else ""
            except (TimeoutError, RuntimeError, OSError, AttributeError):
                pass
        record["tierLabel"] = plan
        record["ready"] = True
        record["signedIn"] = True               # its limits came: signed in
        for window in (limits.get("primary"), limits.get("secondary")):
            entry = _rate_window(window)
            if entry:
                record["limits"].append(entry)
        entry = _spend_limit(limits.get("individualLimit"))
        if entry:
            record["limits"].append(entry)
        return record
    except (TimeoutError, RuntimeError, OSError, ValueError):
        return None
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
        for pipe in (proc.stdin, proc.stdout):
            try:
                if pipe is not None:
                    pipe.close()
            except OSError:
                pass


def _rpc_number(value) -> float:
    try:
        return float(value) if not isinstance(value, bool) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _rpc_reset(value) -> str:
    seconds = _rpc_number(value)
    if seconds <= 0:
        return ""
    moment = dt.datetime.fromtimestamp(seconds, dt.timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _rate_window(window) -> dict | None:
    """limit_window: a rate window's share used and when it resets."""
    used = window.get("usedPercent") if isinstance(window, dict) else None
    if not isinstance(used, (int, float)) or isinstance(used, bool):
        return None
    mins = int(_rpc_number(window.get("windowDurationMins")))
    label = ("Weekly (7-day)" if mins == 10080 else f"{mins // 60}h window" if mins > 0 and mins % 60 == 0
             else f"{mins}m window" if mins > 0 else "Limit")
    return {"label": label, "percent": used / 100, "resetsAt": _rpc_reset(window.get("resetsAt"))}


def _spend_limit(snapshot) -> dict | None:
    """spend_limit: a spend cap's share used."""
    if not isinstance(snapshot, dict):
        return None
    cap = _rpc_number(snapshot.get("limit"))
    if cap > 0 and snapshot.get("used") is not None:
        used = _rpc_number(snapshot.get("used")) / cap
    elif isinstance(snapshot.get("remainingPercent"), (int, float)):
        used = 1 - snapshot["remainingPercent"] / 100
    else:
        return None
    return {"label": "Spend limit", "percent": max(0.0, min(1.0, used)),
            "resetsAt": _rpc_reset(snapshot.get("resetsAt"))}


# --- last used ---------------------------------------------------------------

def newest_mtime(paths: list[str]) -> dt.datetime | None:
    """Newest modification time of these files, or of the entries (two levels
    deep) of these folders, looking at no more than MAX_SCAN entries. Below that
    only the last-named subfolder is followed (DATE_DEPTH more levels): a tree by
    date such as Codex's sessions/2026/10/23/rollout-….jsonl is read down to today's files."""
    newest, seen = 0.0, 0
    for raw in paths:
        path = _expand(raw)
        try:
            info = path.lstat()
        except OSError:
            continue
        newest = max(newest, info.st_mtime)
        if not path.is_dir() or path.is_symlink():
            continue
        stack = [(path, 0)]
        while stack and seen < MAX_SCAN:
            folder, depth = stack.pop()
            try:
                with os.scandir(folder) as entries:
                    for entry in entries:
                        seen += 1
                        if seen >= MAX_SCAN:
                            break
                        try:
                            mtime = entry.stat(follow_symlinks=False).st_mtime
                        except OSError:
                            continue
                        newest = max(newest, mtime)
                        if depth < 1 and entry.is_dir(follow_symlinks=False):
                            stack.append((Path(entry.path), depth + 1))
            except OSError:
                continue
        folder = path                         # the tree by date: on along its last-named subfolders
        for level in range(1 + DATE_DEPTH):
            last = _last_dirs(folder)
            if not last:
                break
            folder = Path(last[0])
            if level == 0:
                continue                      # its entries were read above
            try:
                with os.scandir(folder) as entries:
                    for _i, entry in zip(range(MAX_SCAN), entries):
                        try:
                            newest = max(newest, entry.stat(follow_symlinks=False).st_mtime)
                        except OSError:
                            continue
            except OSError:
                break
    return dt.datetime.fromtimestamp(newest) if newest else None


def _last_dirs(folder: Path, limit: int = 400) -> list[str]:
    """[the last-named subfolder of folder] (by name: 2026 > 2025, 23 > 09), [] when it has none."""
    try:
        with os.scandir(folder) as entries:
            names = [e.path for _i, e in zip(range(limit), entries) if e.is_dir(follow_symlinks=False)]
    except OSError:
        return []
    return sorted(names)[-1:]


# --- the battery: what is left of an online agent's limits ----------------------------------------

BATTERY_LOW = 0.25           # this much left or less: yellow (the default; Settings › Costs)
BATTERY_EMPTY = 0.10         # this much left or less: red (the default; Settings › Costs)


def warn_levels() -> tuple[float, float]:
    """(yellow, red) as shares used, from Settings › Costs: for an online agent's limits and for an
    agent paid per token against a day's limit alike. Red is always above yellow."""
    yellow, red = cast(int, ai_core.setting("warn_yellow")) / 100, cast(int, ai_core.setting("warn_red")) / 100
    return (yellow, red) if red > yellow else (1 - BATTERY_LOW, 1 - BATTERY_EMPTY)


def _too_old(limit, now: dt.datetime) -> bool:
    measured = getattr(limit, "measured_at", None)
    return measured is not None and (now - measured).total_seconds() > LIMITS_MAX_AGE


def current_limits(limits, now: dt.datetime | None = None) -> list:
    """The limits whose numbers still count: one whose reset time has passed is left out (its
    percentage is from before the reset, so it would show too little left), and so is one without a
    valid reset whose numbers are older than LIMITS_MAX_AGE."""
    now = now or dt.datetime.now(dt.timezone.utc)
    return [limit for limit in limits or ()
            if ((limit.resets_at if limit.resets_at.tzinfo else limit.resets_at.astimezone()) > now
                if limit.resets_at is not None else not _too_old(limit, now))]


def limits_stale(limits, now: dt.datetime | None = None) -> tuple | None:
    """None while the limits still counting are current; else (when the oldest of them was measured,
    None when not known): the collector kept them past a failed probe, or they are older than
    LIMITS_MAX_AGE, so they may be out of date."""
    now = now or dt.datetime.now(dt.timezone.utc)
    stale = [limit for limit in current_limits(limits, now) if limit.stale or _too_old(limit, now)]
    if not stale:
        return None
    return (min((limit.measured_at for limit in stale if limit.measured_at is not None), default=None),)


def next_reset(limits, now: dt.datetime | None = None) -> dt.datetime | None:
    """The soonest reset still to come among these limits (then the battery is due again even
    though the usage record stays the same), None without one."""
    now = now or dt.datetime.now(dt.timezone.utc)
    times = [limit.resets_at if limit.resets_at.tzinfo else limit.resets_at.astimezone()
             for limit in limits or () if limit.resets_at is not None]
    return min((t for t in times if t > now), default=None)


def renewed_records(known: dict | None, records: dict, commands) -> set:
    """The agents (of commands) whose usage record was written again, came, or went since the
    last poll; nothing on the first poll (known None: everything is read then anyway)."""
    if known is None:
        return set()
    return {c for c in set(known) | set(records) if c in commands and known.get(c) != records.get(c)}


def battery(limits, now: dt.datetime | None = None, levels: tuple | None = None) -> tuple | None:
    """(left 0..1, "green" | "yellow" | "red") of the tightest limit (a 5-hour session, a week…);
    None without limits. Limit.percent is the share used; limits past their reset don't count.
    levels: (yellow, red) shares used, else the defaults (75 %, 90 %)."""
    lefts = [1.0 - max(0.0, min(1.0, limit.percent)) for limit in current_limits(limits, now)]
    if not lefts:
        return None
    return _charge(min(lefts), levels)


def red_limits(limits, now: dt.datetime | None = None, levels: tuple | None = None) -> list:
    """The limits still counting that are used up to the red level (for the notification)."""
    red = (levels or (1 - BATTERY_LOW, 1 - BATTERY_EMPTY))[1]
    return [limit for limit in current_limits(limits, now) if round(limit.percent, 4) >= red]


def _charge(left: float, levels: tuple | None = None) -> tuple:
    yellow, red = levels or (1 - BATTERY_LOW, 1 - BATTERY_EMPTY)
    left = round(left, 4)                 # 1 - 0.90 is 0.0999…: exactly a tenth left is red
    return left, "red" if left <= round(1 - red, 4) else "yellow" if left <= round(1 - yellow, 4) else "green"


def gpu_battery(free: int, total: int, own: int = 0) -> tuple | None:
    """A running local LLM's battery: (left, colour, tooltip) of the GPU memory still free, with
    the same colours as an online agent's limits; None without a GPU."""
    if total <= 0:
        return None
    gib = 1024 ** 3
    tip = _("GPU memory: {0} of {1} GB free").format(f"{max(0, free) / gib:.1f}", f"{total / gib:.1f}")
    if own >= 64 * 1024 ** 2:
        tip += "\n" + _("This local LLM uses {0} GB").format(f"{own / gib:.1f}")
    return (*_charge(max(0.0, min(1.0, free / total))), tip)


def battery_tip(limits, now: dt.datetime | None = None) -> str:
    """One line per limit still counting: "Weekly (7-day): 55 % left, resets Thu 16:00"."""
    lines = []
    for limit in current_limits(limits, now):
        left = round(100 * (1.0 - max(0.0, min(1.0, limit.percent))))
        line = _("{0}: {1} % left").format(limit.label or _("Limit"), left)
        if limit.resets_at is not None:
            moment = limit.resets_at.astimezone() if limit.resets_at.tzinfo else limit.resets_at
            line += ", " + _("resets {0}").format(moment.strftime("%a %H:%M"))
        lines.append(line)
    stale = limits_stale(limits, now)
    if stale is not None:
        lines.append(_("These numbers may be out of date.") if stale[0] is None else
                     _("Measured {0}: these numbers may be out of date.").format(
                         stale[0].astimezone().strftime("%a %H:%M")))
    return "\n".join(lines)


# --- all together --------------------------------------------------------------

def setup_stamp(commands) -> tuple:
    """Each agent's model and providers as its own settings name them (~4 ms for ten agents): a
    different stamp means one was changed outside the app (its config file, its own /model)."""
    import harnesses
    stamp = []
    for command in commands:
        harness = harnesses.get(command)
        if harness is None or harness.details is None:
            continue
        details = Details()
        try:
            harness.details(details)
        except Exception:          # an add-on's reader: never stops the poll
            continue
        stamp.append((command, details.model, repr(details.providers)))
    return tuple(stamp)


def details_for(command: str, usage_dir: Path = USAGE_DIR) -> Details:
    import harnesses
    details = Details()
    harness = harnesses.get(command)
    if harness is not None and harness.details is not None:
        try:
            harness.details(details)
        except (OSError, ValueError, TypeError, AttributeError):
            pass
    folder = usage_dir
    record = usage_record(command, folder)
    if not record and usage_dir == USAGE_DIR and not has_usage_collector(command):
        folder = native_usage_dir()
        record = usage_record(command, folder)   # asked of the agent itself (native_usage)
        if record and (record.get("home") != usage_home(command) or not plan_limits_signed_in(command)):
            record = {}        # signed out since, or another home's: not this PC's account now
    apply_usage(details, record, record_written(folder / f"{command}.json"))
    if not details.limits:
        # The agent's own store (an adapter's or an extension's limits() fact): plan and limits.
        if harness is not None and harness.limits is not None:
            try:
                plan, limits = harness.limits()
                details.plan = details.plan or str(plan or "")[:40]
                details.limits = [x for x in limits if isinstance(x, Limit)][:8]
            except Exception:      # a store in a format we do not know: no limits, no crash
                pass
    details.last_used = newest_mtime(list(activity(command)))
    config = harness.home if harness is not None else ""
    details.config_dir = config if config and _expand(config).exists() else ""
    return details
