"""The usage statistics box on the map's flowing agent → provider line, while an agent works: how
fast its model answers (output tokens per second of its latest reply), its context size and how
much of it is in use (Settings › Usage statistics box says which of these show).

Read from the agent's own session files, which its adapter names ([replies]: folder, depth,
format, contexts), numbers, times and model names only, never message text. The formats:
- "claude-jsonl": "assistant" lines (message id, model, usage: output_tokens, and input, cache read
  and cache write tokens: the context in use), "user" lines are input;
- "codex-jsonl": "token_count" events (last_token_usage: output_tokens, total_tokens; and
  model_context_window), tool output and user messages are input;
- "pi-jsonl": assistant messages (model, usage: output, totalTokens), every other message is input.

A reply's speed is its output tokens over the time from the input before it (the prompt or a tool's
result) to its last line, so it includes the wait for the first token. The context size comes from
the session file when it says it (Codex), else from the adapter's contexts (model name prefix ->
tokens), else from the local LLM the agent uses (the app fills it in). Only the newest session file
written in the last RECENT seconds is read, and of it only the last TAIL bytes (own regular files,
no links); the file list is looked at again every RESCAN seconds.
"""

import json
import os
import stat
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

RECENT = 60                     # seconds: a reply older than this shows nothing
RESCAN = 10                     # seconds between looks for the newest session file
TAIL = 256 * 1024               # bytes read from the end of that file
MAX_LINE = 64 * 1024            # longer lines are skipped (a reply with big content)
MAX_ENTRIES = 20000             # folder entries looked at per scan
MAX_RATE = 100000.0
ITEMS = ("rate", "context", "used")    # what the box can show, in this order (settings: usage_box_items)
SIZES = (7, 8, 9, 10, 12)              # its text size in pt (settings: usage_box_size), 7 the default


@dataclass
class Reading:
    rate: float | None = None   # output tokens per second of the latest reply
    used: int = 0               # tokens in its context after the latest reply
    window: int = 0             # its context size (0: not known)
    model: str = ""

    @property
    def share(self) -> float | None:
        """The part of the context in use, 0..1, or None when the size is not known."""
        return min(1.0, self.used / self.window) if self.used and self.window else None


def roots(command: str) -> tuple:
    """(folder, depth of the session files below it, format, contexts) from the agent's adapter, or ()."""
    import harnesses
    harness = harnesses.get(command)
    replies = harness.replies if harness is not None else ()
    if not replies or replies[2] not in FORMATS:
        return ()
    return Path(os.path.expanduser(replies[0])), replies[1], replies[2], dict(replies[3] if len(replies) > 3 else ())


def newest_file(root: Path, depth: int, since: float) -> Path | None:
    """The newest *.jsonl at that depth below root written after `since`, or None."""
    best, best_time, seen = None, since, 0
    stack = [(root, 0)]
    while stack and seen < MAX_ENTRIES:
        folder, level = stack.pop()
        try:
            entries = os.scandir(folder)
        except OSError:
            continue
        with entries:
            for entry in entries:
                seen += 1
                if seen >= MAX_ENTRIES:
                    break
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if level < depth and stat.S_ISDIR(info.st_mode):
                    stack.append((Path(entry.path), level + 1))
                elif level == depth and stat.S_ISREG(info.st_mode) and entry.name.endswith(".jsonl") \
                        and info.st_mtime > best_time:
                    best, best_time = Path(entry.path), info.st_mtime
    return best


def tail_lines(path: Path) -> list:
    """The complete lines in the last TAIL bytes of this account's own regular file."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return []
    with os.fdopen(fd, "rb") as handle:
        st = os.fstat(handle.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
            return []
        start = max(0, st.st_size - TAIL)
        handle.seek(start)
        data = handle.read(TAIL)
    lines = data.split(b"\n")
    if start:
        lines = lines[1:]                        # cut in the middle
    return [line for line in lines if line and len(line) <= MAX_LINE]


def _time(value) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value / 1000 if value > 1e11 else float(value)
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _tokens(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 10 ** 9 else 0


def _model(value) -> str:
    return value[:100] if isinstance(value, str) and value.isprintable() else ""


# Each reader turns a line into an Event or None.

@dataclass
class Event:
    when: float
    kind: str                   # "in" (input the next reply answers) or "out" (a reply's line)
    key: str = ""               # the reply's id (a reply may span several lines)
    tokens: int = 0             # its output tokens
    used: int = 0               # tokens in the context after it
    window: int = 0             # the context size, when the line says it
    model: str = ""


def claude_event(entry: dict) -> Event | None:
    kind = entry.get("type")
    when = _time(entry.get("timestamp"))
    if when is None:
        return None
    if kind == "assistant":
        message = entry.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("usage"), dict):
            return None
        usage = message["usage"]
        key = message.get("id") if isinstance(message.get("id"), str) else entry.get("uuid")
        used = sum(_tokens(usage.get(k)) for k in ("input_tokens", "cache_read_input_tokens",
                                                     "cache_creation_input_tokens", "output_tokens"))
        return Event(when, "out", str(key)[:100], _tokens(usage.get("output_tokens")), used,
                     model=_model(message.get("model")))
    if kind == "user":
        return Event(when, "in")
    return None


_CODEX_INPUT = ("function_call_output", "custom_tool_call_output", "user_message", "task_started")


def codex_event(entry: dict) -> Event | None:
    when = _time(entry.get("timestamp"))
    payload = entry.get("payload")
    if when is None or not isinstance(payload, dict):
        return None
    kind = payload.get("type")
    if kind == "token_count":
        info = payload.get("info")
        last = info.get("last_token_usage") if isinstance(info, dict) else None
        if not isinstance(last, dict):
            return None
        return Event(when, "out", f"codex:{when}", _tokens(last.get("output_tokens")),
                     _tokens(last.get("total_tokens")), _tokens(info.get("model_context_window")))
    if kind == "turn_context":
        return Event(when, "model", model=_model(payload.get("model")))
    if kind in _CODEX_INPUT or kind == "message" and payload.get("role") == "user":
        return Event(when, "in")
    return None


def pi_event(entry: dict) -> Event | None:
    when = _time(entry.get("timestamp"))
    if when is None:
        return None
    if entry.get("type") == "model_change":
        return Event(when, "model", model=_model(entry.get("modelId")))
    message = entry.get("message")
    if entry.get("type") != "message" or not isinstance(message, dict):
        return None
    if message.get("role") == "assistant":
        usage = message.get("usage")
        if not isinstance(usage, dict):
            return None
        return Event(when, "out", str(entry.get("id", when))[:100], _tokens(usage.get("output")),
                     _tokens(usage.get("totalTokens")), model=_model(message.get("model")))
    return Event(when, "in")


FORMATS = {"claude-jsonl": claude_event, "codex-jsonl": codex_event, "pi-jsonl": pi_event}


def last_reply(events: list) -> tuple | None:
    """(tokens per second, end time) of the newest reply among the events in file order; None when
    there is none with a measurable time."""
    start = None                 # time of the input the coming reply answers
    replies = {}                 # key -> [start, end, tokens]
    order = []
    for event in events:
        if event.kind == "in":
            start = event.when
            continue
        if event.kind != "out":
            continue
        reply = replies.get(event.key)
        if reply is None:
            reply = replies[event.key] = [start, event.when, 0]
            order.append(event.key)
        reply[1] = max(reply[1], event.when)
        reply[2] = max(reply[2], event.tokens)  # Claude repeats the reply's usage on each of its lines
        start = event.when                       # the next reply (several in a row) starts here
    for key in reversed(order):
        begun, end, tokens = replies[key]
        if begun is not None and tokens > 0 and 0.3 <= end - begun <= 600:
            return min(MAX_RATE, tokens / (end - begun)), end
    return None


def window_for(model: str, contexts: dict) -> int:
    """The adapter's context size for this model: the longest matching name prefix ("" for any)."""
    best = max((p for p in contexts if model.startswith(p)), key=len, default=None)
    return _tokens(contexts.get(best)) if best is not None else 0


def read(form: str, path: Path, contexts: dict | None = None) -> tuple | None:
    """(Reading, time of its latest reply) from one session file, or None."""
    reader = FORMATS.get(form)
    if reader is None:
        return None
    events = []
    for line in tail_lines(path):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and (event := reader(entry)) is not None:
            events.append(event)
    outs = [e for e in events if e.kind == "out"]
    if not outs:
        return None
    last = outs[-1]
    model = last.model or next((e.model for e in reversed(events) if e.model), "")
    window = last.window or window_for(model, contexts or {})
    rate = last_reply(events)
    return Reading(rate[0] if rate else None, last.used, window, model), last.when


class Rates:
    """Remembers each agent's newest session file between the 2-second checks."""

    def __init__(self):
        self.files = {}              # command -> (path or None, monotonic time of the look)

    def readings(self, commands, now: float | None = None, wall: float | None = None) -> dict:
        """{command: Reading of its latest reply} for these (working) agents."""
        now = time.monotonic() if now is None else now
        wall = time.time() if wall is None else wall
        found = {}
        for command in commands:
            where = roots(command)
            if not where:
                continue
            folder, depth, form, contexts = where
            path, looked = self.files.get(command, (None, -RESCAN))
            if path is None or now - looked >= RESCAN:
                path = newest_file(folder, depth, wall - RECENT)
                self.files[command] = (path, now)
            if path is None:
                continue
            result = read(form, path, contexts)
            if result is not None and wall - result[1] <= RECENT:
                found[command] = result[0]
        return found


def rate_text(rate: float) -> str:
    return f"{rate:.1f} t/s" if rate < 10 else f"{rate:.0f} t/s"


def size_text(tokens: int) -> str:
    """1M, 272k, 32k."""
    if tokens >= 1_000_000:
        return f"{tokens / 1_000_000:.1f}".rstrip("0").rstrip(".") + "M"
    return f"{round(tokens / 1000)}k" if tokens >= 1000 else str(tokens)


def share_color(share: float) -> str:
    """As the batteries' thresholds: yellow when a quarter or less is left, red under a tenth;
    below that the line's own colour ("line")."""
    return "red" if share > 0.9 else "yellow" if share >= 0.75 else "line"


def parts(reading: Reading, items) -> list:
    """[(text, colour name)] the box shows, in ITEMS order, for the chosen items ("line": the
    colour of the line it is drawn on)."""
    out = []
    if "rate" in items and reading.rate is not None:
        out.append((rate_text(reading.rate), "line"))
    if "context" in items and reading.window:
        out.append((size_text(reading.window), "line"))
    if "used" in items and (share := reading.share) is not None:
        out.append((f"{share * 100:.0f}%", share_color(share)))
    return out
