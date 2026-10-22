# Vendored from ~/Projects/omarchy-plugin-ai (vendor.py): do not edit here.
"""plugin_ai: let an installed online agent (Claude Code, Codex) do small jobs for a plugin, and keep
one log of those jobs and their tokens for all of Uwe Bruckhaus's plugins.

Shared, vendored copy: the source is ~/Projects/omarchy-plugin-ai/plugin_ai.py; `vendor.py <plugin
folder>` copies it into a plugin. Python standard library only, no GTK: a plugin's helper script,
its QML backend or its GTK popup can all use it. API version in API; bump it when the API changes.

What a plugin can do with it:
- available() / choose(preferred): the installed agents that can do look-ups;
- look_up(agent, prompt, schema, plugin=, task=): one web look-up (the web and nothing else: no
  shell, no files), the answer as text, with its tokens recorded;
- task_argv(agent, prompt): the agent started with a first prompt, for a terminal the user watches
  (it asks before each step itself); record_task() notes that it was started;
- runs() / totals() / per_day(): what was done, its results and tokens (Smart Check in AI Agents
  Map shows all plugins' runs).

Nothing an agent answers is ever run by this module: a plugin checks the answer (first_json(), its
own rules) before it shows or uses it.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

API = 1
MAX_RUNS = 500
MAX_ANSWER = 512 * 1024
MAX_LOG = 2 * 1024 * 1024        # the run log (500 short runs are far below this)
TIMEOUT = 600
OUTPUTS = ("text", "claude-json", "codex-jsonl")


@dataclass(frozen=True)
class AgentFacts:
    name: str
    research: tuple          # argv: "{prompt}" and "{schema}" (a JSON schema file) where they go
    output: str              # how the answer and its tokens come back (OUTPUTS)
    task: tuple = ()         # argv: started with "{prompt}" as its first message


# The agents every plugin knows; AI Agents Map passes its own adapters' facts instead (facts=).
AGENTS = {
    "claude": AgentFacts(
        "Claude Code",
        ("claude", "-p", "{prompt}", "--tools", "WebSearch", "WebFetch", "--allowedTools", "WebSearch", "WebFetch",
         "--strict-mcp-config", "--no-session-persistence", "--disable-slash-commands", "--output-format", "json"),
        "claude-json", ("claude", "{prompt}")),
    "codex": AgentFacts(
        "Codex",
        ("codex", "--search", "exec", "--sandbox", "read-only", "--skip-git-repo-check", "--json", "--output-schema",
         "{schema}", "{prompt}"),
        "codex-jsonl", ("codex", "{prompt}")),
}

# Variables an agent session sets for what runs inside it: a look-up started from inside one must
# not look like part of it (Claude Code stops saving transcripts; mise prints a line per update).
SESSION_VARS = ("CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "AI_AGENT", "CODEX_THREAD_ID", "CODEX_SANDBOX",
                "CODEX_CI", "OPENCODE", "OPENCODE_PID", "PI_CODING_AGENT", "GEMINI_CLI")


def log_file() -> Path:
    state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local/state")
    return Path(state) / "omarchy-plugin-ai/runs.json"


# --- which agent ---------------------------------------------------------------------------------

def available(facts: dict | None = None) -> list[str]:
    """The agents (commands) on PATH that can do look-ups, in the facts' order."""
    facts = AGENTS if facts is None else facts
    return [c for c, f in facts.items() if f.research and shutil.which(c)]


def choose(preferred: str = "", facts: dict | None = None) -> str | None:
    """The preferred agent when it is there, else the first one that is."""
    found = available(facts)
    return preferred if preferred in found else (found[0] if found else None)


def name_of(agent: str, facts: dict | None = None) -> str:
    f = (AGENTS if facts is None else facts).get(agent)
    return f.name if f else agent


# --- one look-up ---------------------------------------------------------------------------------

@dataclass
class Usage:
    input: int = 0           # tokens read, cache writes included
    cached: int = 0          # tokens read from the cache (cheaper)
    output: int = 0
    cost: float | None = None    # USD at API prices, when the agent says

    @property
    def total(self) -> int:
        return self.input + self.cached + self.output


def _int(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def parse_output(kind: str, text: str) -> tuple[str, Usage | None]:
    """(answer text, token use or None) from what the agent printed."""
    if kind == "claude-json":
        data = None
        for chunk in [text.strip()] + text.strip().splitlines()[::-1]:
            try:
                value = json.loads(chunk)
            except ValueError:
                continue
            if isinstance(value, dict) and value.get("type") == "result":
                data = value
                break
        if data is None:
            raise ValueError("no answer")
        if data.get("is_error"):
            raise RuntimeError(str(data.get("result") or data.get("subtype") or "failed")[:200])
        used = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        cost = data.get("total_cost_usd")
        return str(data.get("result") or ""), Usage(
            input=_int(used.get("input_tokens")) + _int(used.get("cache_creation_input_tokens")),
            cached=_int(used.get("cache_read_input_tokens")), output=_int(used.get("output_tokens")),
            cost=float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None)
    if kind == "codex-jsonl":
        answer, usage, seen = "", Usage(), False
        for line in text.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            item = event.get("item") if isinstance(event.get("item"), dict) else {}
            if event.get("type") == "item.completed" and item.get("type") == "agent_message":
                answer = str(item.get("text") or "")
            elif event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
                used, seen = event["usage"], True
                cached = _int(used.get("cached_input_tokens"))
                usage.cached += cached
                usage.input += max(0, _int(used.get("input_tokens")) - cached)    # input counts the cached too
                usage.output += _int(used.get("output_tokens"))
            elif event.get("type") in ("error", "turn.failed"):
                error = event.get("error") if isinstance(event.get("error"), dict) else event
                raise RuntimeError(str(error.get("message") or "failed")[:200])
        return answer, usage if seen else None
    return text, None


def clean_env(environ=None) -> dict:
    env = dict(os.environ if environ is None else environ)
    for name in list(env):
        if name in SESSION_VARS or name.startswith("CLAUDE_CODE_"):
            del env[name]
    return env


def run_bounded(argv: list[str], max_bytes: int = MAX_ANSWER, timeout: float = TIMEOUT, cwd: str | None = None,
                env: dict | None = None) -> tuple[bytes, bool]:
    """argv (no shell), its output up to max_bytes, within one deadline; (output, ended with 0)."""
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            cwd=cwd, env=env if env is not None else clean_env(), start_new_session=True)
    out, deadline = bytearray(), time.monotonic() + timeout
    os.set_blocking(proc.stdout.fileno(), False)
    try:
        while True:
            try:
                chunk = proc.stdout.read(65536)
            except BlockingIOError:
                chunk = None
            if chunk:
                out += chunk[:max(0, max_bytes - len(out))]
            elif chunk == b"" and proc.poll() is not None:
                break
            if time.monotonic() > deadline:
                raise subprocess.TimeoutExpired(argv, timeout)
            if not chunk:
                time.sleep(0.05)
        return bytes(out), proc.returncode == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        proc.stdout.close()


def look_up_argv(argv, output: str, *, plugin: str, task: str, agent: str, run=None, cwd: str | None = None,
                 result_of=None) -> str:
    """Run a look-up command (argv as the agent's facts give it, filled in) and return its answer;
    the run, its tokens and its result (result_of(answer) -> short text) are recorded, ok or not."""
    usage, ok, result = None, False, ""
    try:
        out, done = (run or run_bounded)(list(argv), max_bytes=MAX_ANSWER, timeout=TIMEOUT, cwd=cwd)
        text = out.decode("utf-8", "replace")
        answer, usage = parse_output(output, text)
        if not done:
            raise RuntimeError((answer or text).strip()[-200:] or "no answer")
        ok = True
        if result_of is not None:
            try:
                result = str(result_of(answer) or "")
            except Exception as error:      # the answer did not hold up: the run counts as failed
                ok, result = False, str(error)[:120]
                raise
        return answer
    except Exception as error:
        result = result or str(error)[:120]
        raise
    finally:
        record(plugin, task, agent, usage, ok, result)


def look_up(agent: str, prompt: str, schema: dict, *, plugin: str, task: str, facts: dict | None = None,
            run=None, result_of=None) -> str:
    """One web look-up by `agent` for `plugin` (its `task`, a short id such as "updates")."""
    f = (AGENTS if facts is None else facts)[agent]
    with tempfile.TemporaryDirectory(prefix="plugin-ai-") as work:
        schema_file = Path(work) / "schema.json"
        schema_file.write_text(json.dumps(schema))
        binary = shutil.which(agent) or agent
        argv = [binary] + [{"{prompt}": prompt, "{schema}": str(schema_file)}.get(a, a) for a in f.research[1:]]
        return look_up_argv(argv, f.output, plugin=plugin, task=task, agent=agent, run=run, cwd=work,
                            result_of=result_of)


def first_json(text: str, key: str) -> dict:
    """The first JSON object in text that has `key` (agents wrap JSON in prose or code fences)."""
    decoder = json.JSONDecoder()
    start = text.find("{")
    while start != -1:
        try:
            value, _end = decoder.raw_decode(text, start)
            if isinstance(value, dict) and key in value:
                return value
        except ValueError:
            pass
        start = text.find("{", start + 1)
    raise ValueError("no JSON answer")


# --- a task in a terminal ------------------------------------------------------------------------

def task_argv(agent: str, prompt: str, facts: dict | None = None) -> list[str] | None:
    """The agent started with `prompt` as its first message (one argument), or None."""
    f = (AGENTS if facts is None else facts).get(agent)
    binary = shutil.which(agent)
    if f is None or not f.task or binary is None:
        return None
    return [binary] + [prompt if a == "{prompt}" else a for a in f.task[1:]]


def record_task(plugin: str, task: str, agent: str, result: str = "started") -> None:
    """A task given to an agent in a terminal: its tokens are its own session's (not known here)."""
    record(plugin, task, agent, None, True, result, kind="task")


# --- the log -------------------------------------------------------------------------------------

def read_json(path, max_bytes: int = MAX_LOG):
    """A JSON file of this account's own: never through a link, only a regular file, at most
    max_bytes (larger: None). None when it is missing or not JSON."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > max_bytes:
            return None
        data = os.read(fd, max_bytes + 1)
        return None if len(data) > max_bytes else json.loads(data.decode("utf-8", "replace"))
    except (OSError, ValueError):
        return None
    finally:
        os.close(fd)


def runs(path: Path | None = None) -> list[dict]:
    data = read_json(path or log_file())
    found = data.get("runs") if isinstance(data, dict) else None
    return [r for r in found if isinstance(r, dict)] if isinstance(found, list) else []


def record(plugin: str, task: str, agent: str, usage: Usage | None, ok: bool, result: str = "",
           kind: str = "look-up", path: Path | None = None, now: dt.datetime | None = None) -> None:
    """One run in the shared log (owner-only; plugins may write at the same time: a lock)."""
    path = path or log_file()
    entry = {"when": (now or dt.datetime.now()).isoformat(timespec="seconds"), "plugin": plugin, "task": task,
             "agent": agent, "kind": kind, "ok": bool(ok)}
    if result:
        entry["result"] = " ".join(str(result).split())[:160]
    if usage is not None:
        entry.update(input=usage.input, cached=usage.cached, output=usage.output)
        if usage.cost is not None:
            entry["cost"] = round(usage.cost, 4)
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = os.open(path.with_suffix(".lock"), os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            kept = (runs(path) + [entry])[-MAX_RUNS:]
            fd, tmp = tempfile.mkstemp(prefix=".runs-", dir=str(path.parent))
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), 0o600)
                json.dump({"api": API, "runs": kept}, handle, indent=1)
            os.replace(tmp, path)
        finally:
            os.close(lock)
    except OSError:
        pass


@dataclass
class Totals:
    runs: int = 0
    ok: int = 0
    tokens_today: int = 0
    tokens: int = 0              # in the period
    cost: float = 0.0
    unknown: int = 0             # runs whose tokens are not known (tasks, text-only agents)
    last: dict | None = None
    by_agent: dict = field(default_factory=dict)     # agent -> tokens
    by_plugin: dict = field(default_factory=dict)    # plugin -> tokens
    by_task: dict = field(default_factory=dict)      # (plugin, task) -> tokens


def tokens_of(run: dict) -> int | None:
    return _int(run.get("input")) + _int(run.get("cached")) + _int(run.get("output")) if "input" in run else None


def _when(run: dict) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(str(run.get("when")))
    except ValueError:
        return None


def totals(all_runs: list[dict], days: int = 30, now: dt.datetime | None = None, **match) -> Totals:
    """The runs of the last `days` that match (agent=, plugin=, task=, kind=)."""
    now = now or dt.datetime.now()
    result = Totals()
    for run in all_runs:
        when = _when(run)
        if when is None or now - when > dt.timedelta(days=days) or any(run.get(k) != v for k, v in match.items()):
            continue
        result.runs += 1
        result.ok += bool(run.get("ok"))
        result.last = run
        tokens = tokens_of(run)
        if tokens is None:
            result.unknown += 1
            continue
        result.tokens += tokens
        result.tokens_today += tokens if when.date() == now.date() else 0
        if isinstance(run.get("cost"), (int, float)):
            result.cost += float(run["cost"])
        for table, key in ((result.by_agent, run.get("agent")), (result.by_plugin, run.get("plugin")),
                           (result.by_task, (run.get("plugin"), run.get("task")))):
            table[key] = table.get(key, 0) + tokens
    return result


def per_day(all_runs: list[dict], days: int = 14, now: dt.datetime | None = None, **match) -> list[tuple]:
    """[(date, tokens)] for the last `days` days, oldest first."""
    now = now or dt.datetime.now()
    sums = {now.date() - dt.timedelta(days=i): 0 for i in range(days)}
    for run in all_runs:
        when, tokens = _when(run), tokens_of(run)
        if when is not None and tokens and when.date() in sums and all(run.get(k) == v for k, v in match.items()):
            sums[when.date()] += tokens
    return sorted(sums.items())
