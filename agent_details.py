"""What the expanded agent card shows: plan and limits, model, providers
(local LLMs among them), usage and when the agent was last used.

Sources, all read-only and size-capped:

- The desktop's usage records (desktop.usage_dir(); on Omarchy
  ~/.local/state/omarchy/agents/usage/<agent>.json, written by `omarchy agent
  usage update`): plan, rate limits, today's usage. Only the desktop talks to
  the providers; refresh_usage() asks it to (desktop.refresh_usage).
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

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}

@dataclass
class Provider:
    name: str
    local: bool = False
    runtime: str = ""        # llama.cpp, Ollama, … for local ones
    models: int = 0
    active: bool = True      # the agent uses it now; False: set up, ready to switch to


@dataclass
class Limit:
    label: str
    percent: float           # 0..1
    resets_at: dt.datetime | None = None


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

def _read(path: Path) -> str | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)   # a FIFO in its place cannot hang it
    except OSError:
        return None
    try:
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                return None
            data = handle.read(MAX_FILE_BYTES + 1)
    except OSError:
        return None
    return None if len(data) > MAX_FILE_BYTES else data.decode("utf-8", "replace")


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


def provider_for_url(name: str, url, models: int = 0, active: bool = True) -> Provider:
    """A provider entry; local when its address is this machine."""
    try:
        parsed = urlparse(url) if isinstance(url, str) else None
        host, port = (parsed.hostname, parsed.port) if parsed else (None, None)
    except ValueError:
        host, port = None, None
    local = host in LOCAL_HOSTS
    runtime = local_ports().get(port, "") if local else ""
    return Provider(name=_text(name, 40), local=local, runtime=runtime, models=models, active=active)


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


def apply_usage(details: Details, record: dict):
    if not record:
        return
    details.has_usage = bool(record.get("ready")) or bool(record.get("hasLocalStats"))
    details.plan = _text(record.get("tierLabel"), 40) or details.plan
    details.today_prompts = _count(record.get("todayPrompts"))
    details.today_sessions = _count(record.get("todaySessions"))
    details.today_tokens = _count(record.get("todayTotalTokens"))
    details.total_sessions = _count(record.get("totalSessions"))
    details.active_days = _count(record.get("activeDays"))
    details.usage_updated = _time(record.get("updatedAt"))
    details.usage_note = _text(record.get("usageStatusText"), 120)
    limits = record.get("limits")
    if isinstance(limits, list):
        for item in limits[:8]:
            if not isinstance(item, dict):
                continue
            percent = item.get("percent")
            if isinstance(percent, (int, float)) and not isinstance(percent, bool):
                details.limits.append(Limit(label=_text(item.get("title") or item.get("label"), 40),
                                            percent=max(0.0, min(1.0, float(percent))),
                                            resets_at=_time(item.get("resetsAt"))))


def usage_is_stale(command: str, usage_dir: Path = USAGE_DIR) -> bool:
    """True when Omarchy collects usage for this agent and its record is old (or missing)."""
    if not has_usage_collector(command):
        return False
    try:
        age = dt.datetime.now().timestamp() - (usage_dir / f"{command}.json").stat().st_mtime
    except OSError:
        return True
    return age > USAGE_MAX_AGE


def has_usage_collector(command: str) -> bool:
    return desktop.has_usage_collector(command)


def refresh_usage(command: str) -> bool:
    """Ask the desktop for this agent's usage (blocking; run it on a thread)."""
    return desktop.has_usage_collector(command) and desktop.refresh_usage(command)


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


def current_limits(limits, now: dt.datetime | None = None) -> list:
    """The limits whose numbers still count: one whose reset time has passed is left out (its
    percentage is from before the reset, so it would show too little left)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    return [limit for limit in limits or ()
            if limit.resets_at is None or (limit.resets_at if limit.resets_at.tzinfo else limit.resets_at.astimezone()) > now]


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
    apply_usage(details, usage_record(command, usage_dir))
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
